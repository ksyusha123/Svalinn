import http from 'k6/http';
import { check } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const baseUrl = (__ENV.BASE_URL || 'http://127.0.0.1:5025').replace(/\/$/, '');

export const options = {
  scenarios: {
    low_priority_attack: {
      executor: 'constant-arrival-rate',
      exec: 'lowPriorityAttack',
      rate: Number(__ENV.ATTACK_RATE || 1200),
      timeUnit: '1s',
      duration: __ENV.DURATION || '45s',
      preAllocatedVUs: Number(__ENV.ATTACK_PREALLOCATED_VUS || 500),
      maxVUs: Number(__ENV.ATTACK_MAX_VUS || 2500),
    },
    critical_probe: {
      executor: 'constant-arrival-rate',
      exec: 'criticalProbe',
      rate: Number(__ENV.CRITICAL_RATE || 30),
      timeUnit: '1s',
      duration: __ENV.DURATION || '45s',
      preAllocatedVUs: Number(__ENV.PROBE_PREALLOCATED_VUS || 50),
      maxVUs: Number(__ENV.PROBE_MAX_VUS || 300),
    },
    high_priority_probe: {
      executor: 'constant-arrival-rate',
      exec: 'highPriorityProbe',
      rate: Number(__ENV.HIGH_RATE || 30),
      timeUnit: '1s',
      duration: __ENV.DURATION || '45s',
      preAllocatedVUs: Number(__ENV.PROBE_PREALLOCATED_VUS || 50),
      maxVUs: Number(__ENV.PROBE_MAX_VUS || 300),
    },
  },
  thresholds: {
    http_req_duration: ['p(95)<5000'],
    'slo_availability{priority:Critical}': ['rate>=0.999'],
    'slo_availability{priority:High}': ['rate>=0.99'],
    'slo_shed_rate{priority:Critical}': ['rate<0.001'],
    'slo_shed_rate{priority:High}': ['rate<0.01'],
    'slo_accepted_latency_ms{priority:Critical}': ['p(95)<1200'],
    'slo_accepted_latency_ms{priority:High}': ['p(95)<2500'],
    'slo_shed_rate{priority:Low}': ['rate>0.05'],
  },
};

const statuses = new Counter('svalinn_statuses');
const acceptedRequests = new Counter('svalinn_accepted_requests');
const shedRequests = new Counter('svalinn_shed_requests');
const availability = new Rate('slo_availability');
const shedRate = new Rate('slo_shed_rate');
const acceptedLatency = new Trend('slo_accepted_latency_ms', true);
const criticalRejected = new Counter('svalinn_critical_rejected_requests');

function recordResult(response, endpoint, priority, acceptedStatuses) {
  statuses.add(1, { status: String(response.status), endpoint, priority });

  const wasShed = response.status === 503;
  const wasAccepted = acceptedStatuses.includes(response.status);

  availability.add(wasAccepted, { endpoint, priority });
  shedRate.add(wasShed, { endpoint, priority });

  if (wasShed) {
    shedRequests.add(1, { endpoint, priority });
    if (priority === 'Critical') {
      criticalRejected.add(1, { endpoint });
    }
  }

  if (wasAccepted) {
    acceptedRequests.add(1, { endpoint, priority });
    acceptedLatency.add(response.timings.duration, { endpoint, priority });
  }
}

export function lowPriorityAttack() {
  const endpoint = 'daily_report';
  const priority = 'Low';
  const response = http.get(`${baseUrl}/hospital/reports/daily`, {
    tags: {
      scenario_type: 'dos_attack',
      traffic_role: 'attack',
      priority,
      endpoint,
    },
  });

  recordResult(response, endpoint, priority, [200]);

  check(response, {
    'server returned handled status': (r) => r.status === 200 || r.status === 503,
  });
}

export function criticalProbe() {
  const endpoint = 'allergy_lookup';
  const priority = 'Critical';
  const response = http.get(`${baseUrl}/hospital/patients/42/allergies`, {
    tags: {
      scenario_type: 'dos_attack',
      traffic_role: 'slo_probe',
      priority,
      endpoint,
    },
  });

  recordResult(response, endpoint, priority, [200]);

  check(response, {
    'critical probe served': (r) => r.status === 200,
  });
}

export function highPriorityProbe() {
  const endpoint = 'appointment_booking';
  const priority = 'High';
  const response = http.post(
    `${baseUrl}/hospital/appointments`,
    JSON.stringify({ patientId: 42, department: 'cardiology' }),
    {
      headers: { 'Content-Type': 'application/json' },
      tags: {
        scenario_type: 'dos_attack',
        traffic_role: 'slo_probe',
        priority,
        endpoint,
      },
    },
  );

  recordResult(response, endpoint, priority, [200]);

  check(response, {
    'high priority probe served': (r) => r.status === 200,
  });
}
