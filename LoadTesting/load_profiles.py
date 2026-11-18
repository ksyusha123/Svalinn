"""Deterministic offered-load schedules; all phases share one server process."""

PROFILES = (
    "Plateau",
    "Spike",
    "OverloadRecovery",
    "DependencySlowdown",
    "CompositionShift",
)
SHARES = dict(zip(("critical", "high", "normal", "low"), (25, 30, 25, 20)))
SHIFTED_SHARES = dict(zip(SHARES, (60, 25, 10, 5)))


def phases(a, load):
    def phase(name, duration, rate, service=None, shares=None):
        rates = (
            {p: rate * share / 100 for p, share in (shares or SHARES).items()}
            if a.scenario == "SuccessDisaster"
            else {"critical": a.critical_rate, "low": rate}
        )
        return {
            "name": name,
            "durationSeconds": duration,
            "rates": rates,
            "ioServiceMs": service or a.io_service_ms,
        }

    if a.profile == "Plateau":
        result = [phase("plateau", a.duration_seconds, load)]
    elif a.profile in ("Spike", "OverloadRecovery"):
        result = [
            phase("baseline", a.baseline_seconds, a.baseline_rate),
            phase(
                "spike" if a.profile == "Spike" else "overload",
                a.spike_seconds if a.profile == "Spike" else a.duration_seconds,
                load,
            ),
            phase("recovery", a.recovery_seconds, a.baseline_rate),
        ]
    elif a.profile == "DependencySlowdown":
        result = [
            phase("baseline", a.baseline_seconds, load),
            phase("slowdown", a.duration_seconds, load, a.degraded_io_service_ms),
            phase("recovery", a.recovery_seconds, load),
        ]
    else:
        result = [
            phase("baseline", a.baseline_seconds, load),
            phase("composition", a.duration_seconds, load, shares=SHIFTED_SHARES),
            phase("recovery", a.recovery_seconds, load),
        ]
    start = 0
    for i, item in enumerate(result):
        item.update(index=i, startSeconds=start)
        start += item["durationSeconds"]
    return result


def validate_profile(a):
    if a.profile in ("Spike", "OverloadRecovery"):
        levels = a.rates if a.scenario == "SuccessDisaster" else a.low_rates
        if min(levels) <= a.baseline_rate:
            raise ValueError("Peak rates must exceed --baseline-rate.")
    if a.profile == "DependencySlowdown":
        if a.workload != "Io":
            raise ValueError(
                "DependencySlowdown requires --workload Io to isolate dependency capacity."
            )
        if a.degraded_io_service_ms <= a.io_service_ms:
            raise ValueError(
                "Degraded service time must exceed normal I/O service time."
            )
        if a.degraded_io_service_ms >= min(a.slo_ms, a.high_slo_ms):
            raise ValueError(
                "Degraded service time must remain below protected SLOs; otherwise shedding cannot meet them."
            )
    if a.profile == "CompositionShift" and a.scenario != "SuccessDisaster":
        raise ValueError("CompositionShift requires SuccessDisaster.")
