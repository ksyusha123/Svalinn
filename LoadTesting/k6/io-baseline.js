import http from 'k6/http';
import { Counter, Trend } from 'k6/metrics';

const rate = Number(__ENV.RATE);
const warmup = Number(__ENV.WARMUP_SECONDS || 30);
const duration = Number(__ENV.MEASURE_SECONDS || 180);
const requests = new Counter('measured_requests');
const successes = new Counter('measured_successes');
const timeouts = new Counter('measured_timeouts');
const shed = new Counter('measured_shed');
const timelySuccesses = new Counter('measured_timely_successes');
const errors = new Counter('measured_other_errors');
const latency = new Trend('measured_latency_ms', true);
const successLatency = new Trend('measured_success_latency_ms', true);
const spike = __ENV.PROFILE === 'Spike';
const scenario = (seconds, startTime, exec, offeredRate = rate) => ({
    executor: 'constant-arrival-rate', rate: offeredRate, timeUnit: '1s',
    duration: `${seconds}s`, startTime, exec,
    preAllocatedVUs: 256, maxVUs: 256, gracefulStop: '6s',
});
export const options = {
    scenarios: spike ? {
        warmup: scenario(warmup, '0s', 'warmupRequest', 15),
        baseline: scenario(45, `${warmup}s`, 'baselineRequest', 15),
        spike: scenario(30, `${warmup + 45}s`, 'spikeRequest', rate),
        recovery: scenario(60, `${warmup + 75}s`, 'recoveryRequest', 15),
    } : {
        warmup: scenario(warmup, '0s', 'warmupRequest'),
        measure: scenario(duration, `${warmup}s`, 'measureRequest'),
    },
    summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
    thresholds: spike ? {
        'dropped_iterations{scenario:baseline}': ['count>=0'],
        'dropped_iterations{scenario:spike}': ['count>=0'],
        'dropped_iterations{scenario:recovery}': ['count>=0'],
    } : { 'dropped_iterations{scenario:measure}': ['count>=0'] },
};
function send(measured, phase = 'plateau') {
    const started = Date.now();
    const response = http.get(`${__ENV.BASE_URL}/benchmark/io/normal`, { timeout: '5s', tags: { phase } });
    if (!measured) return;
    const ms = Date.now() - started;
    const tags = { phase };
    requests.add(1, tags);
    latency.add(ms, tags);
    successes.add(response.status === 200 ? 1 : 0, tags);
    shed.add(response.status === 503 ? 1 : 0, tags);
    timelySuccesses.add(response.status === 200 && ms < 500 ? 1 : 0, tags);
    timeouts.add(response.error_code === 1050 ? 1 : 0, tags);
    errors.add(response.status !== 200 && response.status !== 503 && response.error_code !== 1050 ? 1 : 0, tags);
    if (response.status === 200) successLatency.add(ms, tags);
}
export function warmupRequest() { send(false); }
export function measureRequest() { send(true); }
export function baselineRequest() { send(true, 'baseline'); }
export function spikeRequest() { send(true, 'spike'); }
export function recoveryRequest() { send(true, 'recovery'); }
export function handleSummary(data) {
    return { [__ENV.SUMMARY_PATH]: JSON.stringify(data, null, 2) };
}
