import http from 'k6/http';
import execution from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

export function createExperiment(scenario) {
  const baseUrl = (__ENV.BASE_URL || 'http://127.0.0.1:5026').replace(/\/$/, '');
  const workload = (__ENV.WORKLOAD || 'Cpu').toLowerCase();
  const timeoutSeconds = Number(__ENV.TIMEOUT_SECONDS || 5);
  const sloMs = Number(__ENV.SLO_MS || (workload === 'delay' ? 1200 : 1000));
  const highSloMs = Number(__ENV.HIGH_SLO_MS || (workload === 'delay' ? 2500 : sloMs));
  const sloDefinition = __ENV.SLO_DEFINITION || 'SuccessfulLatency';
  const criticalSuccessTargetPercent = __ENV.CRITICAL_SUCCESS_TARGET_PERCENT ? Number(__ENV.CRITICAL_SUCCESS_TARGET_PERCENT) : null;
  if (!['SuccessfulLatency', 'TimelySuccessRatio'].includes(sloDefinition) ||
      (criticalSuccessTargetPercent !== null && (!Number.isFinite(criticalSuccessTargetPercent) || criticalSuccessTargetPercent <= 0 || criticalSuccessTargetPercent > 100)))
    throw new Error('Invalid evaluation policy.');
  const phases = JSON.parse(__ENV.PHASES_JSON || 'null');
  if (!['cpu', 'io', 'mixed', 'delay'].includes(workload) || !Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0 ||
      !Number.isFinite(sloMs) || sloMs <= 0 || !Number.isFinite(highSloMs) || highSloMs <= 0 || !phases?.length)
    throw new Error('Invalid workload parameters; use run_experiment.py.');
  let durationSeconds = 0;
  for (const [i, phase] of phases.entries()) {
    if (phase.index !== i || phase.startSeconds !== durationSeconds || !Number.isInteger(phase.durationSeconds) ||
        phase.durationSeconds <= 0 || !Number.isInteger(phase.ioServiceMs) || phase.ioServiceMs < 1 || phase.ioServiceMs > 10000 ||
        !Object.keys(phase.rates).length || Object.entries(phase.rates).some(([p, r]) =>
          !['critical', 'high', 'normal', 'low'].includes(p) || !Number.isFinite(r) || r < 0))
      throw new Error('Invalid phase schedule.');
    durationSeconds += phase.durationSeconds;
  }
  const priorities = [...new Set(phases.flatMap(p => Object.keys(p.rates)))];
  const clockEpoch = new Trend('test_clock_epoch_ms');
  const scenarios = {}, streams = [], metrics = {}, windows = [], cpuPercent = {}, rates = {};
  function makeMetrics(name) {
    if (!metrics[name]) metrics[name] = {
      requests: new Counter(`${name}_requests`), latency: new Trend(`${name}_latency_ms`, true),
      successLatency: new Trend(`${name}_success_latency_ms`, true), success: new Rate(`${name}_success`),
      slo: new Rate(`${name}_slo`), shed: new Rate(`${name}_shed`), timeout: new Rate(`${name}_timeout`),
      errors: new Counter(`${name}_errors`), good: new Counter(`${name}_good`),
      ioWait: new Trend(`${name}_io_wait_ms`, true), ioService: new Trend(`${name}_io_service_ms`, true),
    };
  }
  function gcd(a, b) { while (b) { const r = a % b; a = b; b = r; } return a; }
  for (const p of priorities) {
    const percent = Number(__ENV[`${p.toUpperCase()}_CPU_PERCENT`] || '50');
    if (!Number.isInteger(percent) || percent < 0 || percent > 100) throw new Error('Invalid CPU share.');
    cpuPercent[p] = workload === 'cpu' ? 100 : workload === 'io' ? 0 : percent;
    rates[p] = 0;
    makeMetrics(p);
  }
  for (const phase of phases) {
    // Windows never cross phase boundaries; attribution uses request start, not completion.
    for (let offset = 0; offset < phase.durationSeconds; offset += 5) {
      const window = { index: windows.length, phase: phase.index, startSeconds: phase.startSeconds + offset,
        durationSeconds: Math.min(5, phase.durationSeconds - offset) };
      windows.push(window);
      for (const p of Object.keys(phase.rates)) makeMetrics(`window_${window.index}_${p}`);
    }
    for (const [priority, rate] of Object.entries(phase.rates)) {
      rates[priority] += rate * phase.durationSeconds / durationSeconds;
      makeMetrics(`phase_${phase.index}_${priority}`);
      for (const kind of (workload === 'delay' ? ['delay'] : ['cpu', 'io'])) {
        const part = kind === 'delay' ? 100 : kind === 'cpu' ? cpuPercent[priority] : 100 - cpuPercent[priority];
        const numerator = Math.round(rate * 100) * part;
        if (!numerator) continue;
        const divisor = gcd(numerator, 10000), rps = numerator / 10000;
        const name = `${priority}_${kind}_p${phase.index}`;
        const vus = Math.ceil(rps * (timeoutSeconds + 1)) + 5;
        streams.push({ name, priority, kind, phase: phase.index, rate: rps, durationSeconds: phase.durationSeconds });
        makeMetrics(name);
        scenarios[name] = { executor: 'constant-arrival-rate', rate: numerator / divisor,
          timeUnit: `${10000 / divisor}s`, duration: `${phase.durationSeconds}s`, startTime: `${phase.startSeconds}s`,
          gracefulStop: `${timeoutSeconds + 2}s`, preAllocatedVUs: vus, maxVUs: vus, exec: 'runRequest' };
      }
    }
  }
  const byName = Object.fromEntries(streams.map(s => [s.name, s]));
  const thresholds = { dropped_iterations: ['count==0'], critical_good: ['count>0'] };
  if (sloDefinition === 'SuccessfulLatency') thresholds.critical_success_latency_ms = [`p(90)<${sloMs}`];
  else thresholds.critical_slo = ['rate>=0.90'];
  if (criticalSuccessTargetPercent !== null) thresholds.critical_success = [`rate>=${criticalSuccessTargetPercent / 100}`];
  return {
    options: { discardResponseBodies: true, scenarios, thresholds,
      summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(90)', 'p(95)', 'p(99)', 'count'] },
    runRequest() {
      const stream = byName[execution.scenario.name];
      const { priority, kind } = stream, phase = phases[stream.phase];
      const targetMs = priority === 'high' ? highSloMs : sloMs;
      // k6 may schedule one boundary iteration at exactly duration; retain it in its originating phase.
      const seconds = Math.max(phase.startSeconds, Math.min(phase.startSeconds + phase.durationSeconds - 1e-6,
        execution.instance.currentTestRunDuration / 1000));
      const window = windows.find(w => w.phase === phase.index && seconds >= w.startSeconds &&
        seconds < w.startSeconds + w.durationSeconds);
      const started = Date.now();
      clockEpoch.add(started - execution.instance.currentTestRunDuration);
      const response = http.get(`${baseUrl}/benchmark/${kind}/${priority}`, {
        timeout: `${timeoutSeconds}s`, tags: { priority, workload: kind, phase: phase.name },
        headers: kind === 'io' ? { 'X-Benchmark-Service-Ms': String(phase.ioServiceMs) } : {},
      });
      const elapsed = Date.now() - started, ok = response.status === 200;
      const names = [priority, stream.name, `phase_${phase.index}_${priority}`];
      if (window) names.push(`window_${window.index}_${priority}`);
      for (const name of names) {
        const m = metrics[name];
        m.requests.add(1); m.latency.add(elapsed); m.success.add(ok); m.slo.add(ok && elapsed < targetMs);
        m.shed.add(response.status === 503); m.timeout.add(response.error_code === 1050);
        m.errors.add(ok ? 0 : 1); m.good.add(ok && elapsed < targetMs ? 1 : 0);
        if (ok) m.successLatency.add(elapsed);
        if (ok && kind === 'io') {
          m.ioWait.add(Number(response.headers['X-Benchmark-Io-Wait-Ms']));
          m.ioService.add(Number(response.headers['X-Benchmark-Io-Service-Ms']));
        }
      }
    },
    handleSummary(data) {
      data.experiment = { schemaVersion: 2, strategy: __ENV.STRATEGY || 'Inflight', scenario, workload,
        profile: __ENV.PROFILE || 'Plateau', phases, windows, streams, rates, cpuPercent,
        totalRate: Object.values(rates).reduce((a, b) => a + b, 0),
        mode: __ENV.MODE || 'manual', repetition: Number(__ENV.REPETITION || 1),
        durationSeconds, sloMs, highSloMs, timeoutSeconds, latencyPercentile: 90, sloDefinition, criticalSuccessTargetPercent };
      return { [__ENV.SUMMARY_PATH || 'workload-summary.json']: JSON.stringify(data, null, 2) };
    },
  };
}
