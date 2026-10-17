import http from 'k6/http';
import execution from 'k6/execution';
import { Counter, Rate, Trend } from 'k6/metrics';

export function createExperiment(scenario) {
  const baseUrl = (__ENV.BASE_URL || 'http://127.0.0.1:5026').replace(/\/$/, '');
  const workload = (__ENV.WORKLOAD || 'Cpu').toLowerCase();
  const durationSeconds = Number(__ENV.DURATION_SECONDS || 30);
  const timeoutSeconds = Number(__ENV.TIMEOUT_SECONDS || 5);
  const sloMs = Number(__ENV.SLO_MS || (workload === 'delay' ? 1200 : 250));
  const highSloMs = Number(__ENV.HIGH_SLO_MS || (workload === 'delay' ? 2500 : sloMs));
  const totalRate = Number(__ENV.RATE || 80);
  const criticalRate = Number(__ENV.CRITICAL_RATE || 5);
  const lowRate = Number(__ENV.LOW_RATE || 30);
  const shares = scenario === 'SuccessDisaster' ? { critical: 25, high: 30, normal: 25, low: 20 } : null;
  const rates = shares ? Object.fromEntries(Object.entries(shares).map(([p, share]) => [p, totalRate * share / 100]))
    : { critical: criticalRate, low: lowRate };
  if (!['cpu', 'io', 'mixed', 'delay'].includes(workload) || !Number.isInteger(durationSeconds) || durationSeconds < 1 ||
      !Number.isFinite(timeoutSeconds) || timeoutSeconds <= 0 || !Number.isFinite(sloMs) || sloMs <= 0 || !Number.isFinite(highSloMs) || highSloMs <= 0 ||
      (shares && (!Number.isInteger(totalRate) || totalRate < 1)) || !Number.isInteger(criticalRate) || criticalRate < 1 ||
      !Number.isInteger(lowRate) || lowRate < 0) throw new Error('РќРµРєРѕСЂСЂРµРєС‚РЅС‹Рµ РїР°СЂР°РјРµС‚СЂС‹ РЅР°РіСЂСѓР·РєРё.');

  const cpuPercent = {};
  const scenarios = {}, streamRates = {}, metrics = {};
  const ioWait = new Trend('io_wait_ms', true), ioService = new Trend('io_service_ms', true);
  function makeMetrics(name) {
    return {
      requests: new Counter(`${name}_requests`), latency: new Trend(`${name}_latency_ms`, true),
      successLatency: new Trend(`${name}_success_latency_ms`, true), success: new Rate(`${name}_success`),
      slo: new Rate(`${name}_slo`), shed: new Rate(`${name}_shed`), timeout: new Rate(`${name}_timeout`),
      errors: new Counter(`${name}_errors`), good: new Counter(`${name}_good`),
    };
  }
  function gcd(a, b) { while (b) { const r = a % b; a = b; b = r; } return a; }
  for (const [priority, rate] of Object.entries(rates)) {
    const percent = Number(__ENV[`${priority.toUpperCase()}_CPU_PERCENT`] || '50');
    if (!Number.isInteger(percent) || percent < 0 || percent > 100) throw new Error('Р”РѕР»СЏ CPU РґРѕР»Р¶РЅР° Р±С‹С‚СЊ С†РµР»РѕР№ РѕС‚ 0 РґРѕ 100.');
    cpuPercent[priority] = workload === 'cpu' ? 100 : workload === 'io' ? 0 : percent;
    metrics[priority] = makeMetrics(priority);
    for (const kind of (workload === 'delay' ? ['delay'] : ['cpu', 'io'])) {
      const part = kind === 'delay' ? 100 : kind === 'cpu' ? cpuPercent[priority] : 100 - cpuPercent[priority];
      // РўРѕС‡РЅР°СЏ СЂР°С†РёРѕРЅР°Р»СЊРЅР°СЏ РёРЅС‚РµРЅСЃРёРІРЅРѕСЃС‚СЊ. РЎРѕРєСЂР°С‰Р°РµРј РґСЂРѕР±СЊ, С‡С‚РѕР±С‹ РёР·Р±РµР¶Р°С‚СЊ РѕРєСЂСѓРіР»РµРЅРёСЏ РґРѕР»РµР№.
      const numerator = Math.round(rate * 100) * part;
      if (!numerator) continue;
      const divisor = gcd(numerator, 10000);
      const name = `${priority}_${kind}`;
      const rps = numerator / 10000;
      const vus = Math.ceil(rps * (timeoutSeconds + 1)) + 5;
      streamRates[name] = rps;
      metrics[name] = makeMetrics(name);
      scenarios[name] = { executor: 'constant-arrival-rate', rate: numerator / divisor,
        timeUnit: `${10000 / divisor}s`, duration: `${durationSeconds}s`,
        gracefulStop: `${timeoutSeconds + 2}s`, preAllocatedVUs: vus, maxVUs: vus, exec: 'runRequest' };
    }
  }
  const thresholds = { dropped_iterations: ['count==0'], critical_success: ['rate>=0.999'],
    critical_slo: ['rate>=0.95'], critical_success_latency_ms: [`p(95)<${sloMs}`] };
  if (rates.high) Object.assign(thresholds, { high_success: ['rate>=0.99'], high_slo: ['rate>=0.95'],
    high_success_latency_ms: [`p(95)<${highSloMs}`] });

  return {
    options: { discardResponseBodies: true, scenarios, thresholds,
      summaryTrendStats: ['avg', 'min', 'med', 'max', 'p(95)', 'p(99)', 'count'] },
    runRequest() {
      const name = execution.scenario.name;
      const [priority, kind] = name.split('_');
      const targetMs = priority === 'high' ? highSloMs : sloMs;
      const started = Date.now();
      const response = http.get(`${baseUrl}/benchmark/${kind}/${priority}`, {
        timeout: `${timeoutSeconds}s`, tags: { priority, workload: kind, scenario_type: scenario },
      });
      const elapsed = Date.now() - started, ok = response.status === 200;
      for (const m of [metrics[priority], metrics[name]]) {
        m.requests.add(1); m.latency.add(elapsed); m.success.add(ok); m.slo.add(ok && elapsed <= targetMs);
        m.shed.add(response.status === 503); m.timeout.add(response.error_code === 1050);
        m.errors.add(ok ? 0 : 1); m.good.add(ok && elapsed <= targetMs ? 1 : 0);
        if (ok) m.successLatency.add(elapsed);
      }
      if (ok && kind === 'io') {
        ioWait.add(Number(response.headers['X-Benchmark-Io-Wait-Ms']));
        ioService.add(Number(response.headers['X-Benchmark-Io-Service-Ms']));
      }
    },
    handleSummary(data) {
      data.experiment = { strategy: __ENV.STRATEGY || 'Inflight', scenario, workload, rates, streamRates, cpuPercent, shares,
        totalRate: Object.values(rates).reduce((a, b) => a + b, 0),
        mode: __ENV.MODE || 'manual', repetition: Number(__ENV.REPETITION || 1),
        durationSeconds, sloMs, highSloMs, timeoutSeconds };
      return { [__ENV.SUMMARY_PATH || 'workload-summary.json']: JSON.stringify(data, null, 2) };
    },
  };
}
