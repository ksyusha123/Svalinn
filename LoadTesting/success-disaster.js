import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const baseUrl = (__ENV.BASE_URL || 'http://127.0.0.1:5025').replace(/\/$/, '');
const thinkSeconds = Number(__ENV.THINK_SECONDS || 0);

export const options = {
  scenarios: {
    success_disaster: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RATE || 900),
      timeUnit: '1s',
      duration: __ENV.DURATION || '60s',
      preAllocatedVUs: Number(__ENV.PREALLOCATED_VUS || 500),
      maxVUs: Number(__ENV.MAX_VUS || 2500),
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<5000'],
    'slo_availability{priority:Critical}': ['rate>=0.999'],
    'slo_availability{priority:High}': ['rate>=0.99'],
    'slo_shed_rate{priority:Critical}': ['rate<0.001'],
    'slo_accepted_latency_ms{priority:Critical}': ['p(95)<1200'],
    'slo_accepted_latency_ms{priority:High}': ['p(95)<2500'],
  },
};

const statuses = new Counter('svalinn_statuses');
const acceptedRequests = new Counter('svalinn_accepted_requests');
const shedRequests = new Counter('svalinn_shed_requests');
const criticalRejected = new Counter('svalinn_critical_rejected_requests');
const availability = new Rate('slo_availability');
const shedRate = new Rate('slo_shed_rate');
const acceptedLatency = new Trend('slo_accepted_latency_ms', true);

const traffic = [
  {
    weight: 15,
    method: 'GET',
    path: '/hospital/patients/42/allergies',
    priority: 'Critical',
    name: 'allergy_lookup',
  },
  {
    weight: 10,
    method: 'POST',
    path: '/hospital/emergency/admissions',
    priority: 'Critical',
    name: 'emergency_admission',
    body: { patientName: 'Load Test Patient', severity: 5 },
  },
  {
    weight: 30,
    method: 'POST',
    path: '/hospital/appointments',
    priority: 'High',
    name: 'appointment_booking',
    body: { patientId: 42, department: 'cardiology' },
  },
  {
    weight: 25,
    method: 'GET',
    path: '/hospital/patients/42',
    priority: 'Normal',
    name: 'patient_profile',
  },
  {
    weight: 20,
    method: 'GET',
    path: '/hospital/reports/daily',
    priority: 'Low',
    name: 'daily_report',
  },
];

const totalWeight = traffic.reduce((sum, item) => sum + item.weight, 0);

function pickRequest() {
  let ticket = Math.random() * totalWeight;

  for (const item of traffic) {
    ticket -= item.weight;
    if (ticket <= 0) {
      return item;
    }
  }

  return traffic[traffic.length - 1];
}

export default function () {
  const item = pickRequest();
  const params = {
    headers: item.body ? { 'Content-Type': 'application/json' } : {},
    tags: {
      scenario_type: 'success_disaster',
      priority: item.priority,
      endpoint: item.name,
    },
  };

  const response = item.method === 'POST'
    ? http.post(`${baseUrl}${item.path}`, JSON.stringify(item.body), params)
    : http.get(`${baseUrl}${item.path}`, params);

  statuses.add(1, {
    status: String(response.status),
    endpoint: item.name,
    priority: item.priority,
  });

  const wasShed = response.status === 503;
  const wasAccepted = response.status === 200 || response.status === 202;

  availability.add(wasAccepted, { endpoint: item.name, priority: item.priority });
  shedRate.add(wasShed, { endpoint: item.name, priority: item.priority });

  if (wasShed) {
    shedRequests.add(1, { endpoint: item.name, priority: item.priority });
    if (item.priority === 'Critical') {
      criticalRejected.add(1, { endpoint: item.name });
    }
  }

  if (wasAccepted) {
    acceptedRequests.add(1, { endpoint: item.name, priority: item.priority });
    acceptedLatency.add(response.timings.duration, { endpoint: item.name, priority: item.priority });
  }

  check(response, {
    'server returned handled status': (r) => r.status === 200 || r.status === 202 || r.status === 503,
    'critical requests are not shed': (r) => item.priority !== 'Critical' || r.status !== 503,
  });

  if (thinkSeconds > 0) {
    sleep(thinkSeconds);
  }
}
