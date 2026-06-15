# Svalinn Hospital Load Testing

k6 load tests for the hospital benchmark API.

Default target:

```powershell
http://127.0.0.1:5025
```

## Run the App

With Svalinn enabled:

```powershell
$env:Svalinn__Enabled="true"
dotnet run --project TestApp --urls http://127.0.0.1:5025
```

Without Svalinn:

```powershell
$env:Svalinn__Enabled="false"
dotnet run --project TestApp --urls http://127.0.0.1:5025
```

Run each load test once with Svalinn disabled and once with Svalinn enabled, then compare:

- total requests
- successful responses
- rejected responses, especially `503`
- custom k6 counters: `svalinn_accepted_requests` and `svalinn_shed_requests`
- SLO availability by priority: `slo_availability{priority:*}`
- shed rate by priority: `slo_shed_rate{priority:*}`
- accepted-request latency by priority: `slo_accepted_latency_ms{priority:*}`
- `/metrics` counters when Svalinn is enabled

The default configuration is intentionally tuned to make load shedding visible:

- `Svalinn:MaxConcurrentRequests = 4`
- low-priority reports take `3000 ms`
- normal requests take `800 ms`
- appointment requests take about `1600 ms`
- k6 uses constant arrival rate instead of a fixed number of users

## DoS-Style Flood

This scenario now has two kinds of traffic:

- attack traffic floods the low-priority expensive report endpoint;
- SLO probe traffic continuously calls critical and high-priority operations.

The goal is not to measure availability of the attack endpoint. The goal is to
verify whether critical/high operations remain within SLO while low-priority
traffic is being shed.

The script reports SLO pass/fail directly through k6 thresholds:

- `Critical` availability must be at least `99.9%`
- `High` availability must be at least `99%`
- `Critical` shed rate must stay below `0.1%`
- `High` shed rate must stay below `1%`
- accepted `Critical` requests must have `p95 < 1200 ms`
- accepted `High` requests must have `p95 < 2500 ms`
- low-priority shed rate should be greater than `5%`, otherwise the scenario did not create visible shedding

```powershell
k6 run LoadTesting/dos-attack.js
```

Override defaults:

```powershell
$env:BASE_URL="http://127.0.0.1:5025"
$env:ATTACK_RATE="1200"
$env:CRITICAL_RATE="30"
$env:HIGH_RATE="30"
$env:ATTACK_PREALLOCATED_VUS="500"
$env:ATTACK_MAX_VUS="2500"
$env:PROBE_PREALLOCATED_VUS="50"
$env:PROBE_MAX_VUS="300"
$env:DURATION="45s"
k6 run LoadTesting/dos-attack.js
```

## Success Disaster

This simulates a legitimate traffic spike across critical, high, normal, and low-priority endpoints.
The script reports SLO pass/fail directly through k6 thresholds:

- `Critical` availability must be at least `99.9%`
- `High` availability must be at least `99%`
- `Critical` shed rate must stay below `0.1%`
- accepted `Critical` requests must have `p95 < 1200 ms`
- accepted `High` requests must have `p95 < 2500 ms`

```powershell
k6 run LoadTesting/success-disaster.js
```

Override defaults:

```powershell
$env:BASE_URL="http://127.0.0.1:5025"
$env:RATE="900"
$env:PREALLOCATED_VUS="500"
$env:MAX_VUS="2500"
$env:DURATION="60s"
$env:THINK_SECONDS="0"
k6 run LoadTesting/success-disaster.js
```

The traffic mix is intentionally simple and visible in the script.
The key expected result with Svalinn enabled is that normal and low-priority
requests receive `503` during overload, while critical requests continue to pass.

In the k6 summary, look at these lines first:

- `slo_availability`: whether accepted responses satisfy availability targets
- `slo_shed_rate`: which priorities were shed
- `slo_accepted_latency_ms`: latency only for requests that were accepted by the app
- `svalinn_critical_rejected_requests`: should remain `0`

If every request still passes on your machine, increase `RATE` first. If k6 reports
`Insufficient VUs`, increase `PREALLOCATED_VUS` and `MAX_VUS`.
