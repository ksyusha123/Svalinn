import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const baseUrl = (__ENV.BASE_URL || 'http://127.0.0.1:5025').replace(/\/$/, '');

export const options = {
  scenarios: {
    dos_attack: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RATE || 800),
      timeUnit: '1s',
      duration: __ENV.DURATION || '30s',
      preAllocatedVUs: Number(__ENV.PREALLOCATED_VUS || 300),
      maxVUs: Number(__ENV.MAX_VUS || 1500),
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<5000'],
  },
};

const statuses = new Counter('svalinn_statuses');

export default function () {
  const response = http.get(`${baseUrl}/hospital/reports/daily`, {
    tags: {
      scenario_type: 'dos_attack',
      priority: 'Low',
      endpoint: 'daily_report',
    },
  });

  statuses.add(1, { status: String(response.status), endpoint: 'daily_report' });

  check(response, {
    'server returned handled status': (r) => r.status === 200 || r.status === 503,
  });
}
