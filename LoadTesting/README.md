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
- latency percentiles
- `/metrics` counters when Svalinn is enabled

## DoS-Style Flood

This floods the low-priority expensive report endpoint.

```powershell
k6 run LoadTesting/dos-attack.js
```

Override defaults:

```powershell
$env:BASE_URL="http://127.0.0.1:5025"
$env:RATE="800"
$env:PREALLOCATED_VUS="300"
$env:MAX_VUS="1500"
$env:DURATION="30s"
k6 run LoadTesting/dos-attack.js
```

## Success Disaster

This simulates a legitimate traffic spike across critical, high, normal, and low-priority endpoints.

```powershell
k6 run LoadTesting/success-disaster.js
```

Override defaults:

```powershell
$env:BASE_URL="http://127.0.0.1:5025"
$env:RATE="600"
$env:PREALLOCATED_VUS="250"
$env:MAX_VUS="1200"
$env:DURATION="60s"
$env:THINK_SECONDS="0"
k6 run LoadTesting/success-disaster.js
```

The traffic mix is intentionally simple and visible in the script.

If every request still passes on your machine, increase `RATE` first. If k6 reports
`Insufficient VUs`, increase `PREALLOCATED_VUS` and `MAX_VUS`.
