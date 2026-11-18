"""Phase and arrival-window analysis. Percentiles are never pooled across runs."""

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path


def metric(metrics, name, stat, default=math.nan):
    return metrics.get(name, {}).get("values", {}).get(stat, default)


def outcome(metrics, prefix, duration):
    count = metric(metrics, prefix + "_requests", "count", 0)
    good = metric(metrics, prefix + "_good", "count", 0)
    return {
        "requests": count,
        "good": good,
        "slo_misses": count - good,
        "errors": metric(metrics, prefix + "_errors", "count", 0),
        "goodput_rps": good / duration,
        "success_pct": 100 * metric(metrics, prefix + "_success", "rate"),
        "slo_pct": 100 * metric(metrics, prefix + "_slo", "rate"),
        "shed_pct": 100 * metric(metrics, prefix + "_shed", "rate"),
        "timeout_pct": 100 * metric(metrics, prefix + "_timeout", "rate"),
        "p90_ms": metric(metrics, prefix + "_success_latency_ms", "p(90)"),
        "p95_ms": metric(metrics, prefix + "_success_latency_ms", "p(95)"),
        "p99_ms": metric(metrics, prefix + "_success_latency_ms", "p(99)"),
        "io_wait_p95_ms": metric(metrics, prefix + "_io_wait_ms", "p(95)"),
        "io_service_p95_ms": metric(metrics, prefix + "_io_service_ms", "p(95)"),
    }


def validate_run(exp, metrics):
    reasons = []
    if metric(metrics, "dropped_iterations", "count", 0):
        reasons.append("generator dropped iterations")
    for stream in exp["streams"]:
        actual = metric(metrics, stream["name"] + "_requests", "count", 0)
        if abs(actual - stream["rate"] * stream["durationSeconds"]) > 1 + 1e-9:
            reasons.append("arrival count: " + stream["name"])
    for phase in exp["phases"]:
        for priority in phase["rates"]:
            count = metric(
                metrics, f"phase_{phase['index']}_{priority}_requests", "count", 0
            )
            streams = sum(
                metric(metrics, s["name"] + "_requests", "count", 0)
                for s in exp["streams"]
                if s["phase"] == phase["index"] and s["priority"] == priority
            )
            windows = sum(
                metric(metrics, f"window_{w['index']}_{priority}_requests", "count", 0)
                for w in exp["windows"]
                if w["phase"] == phase["index"]
            )
            if count != streams or count != windows:
                reasons.append(
                    f"incomplete attribution: phase {phase['index']} {priority}"
                )
    for priority in exp["rates"]:
        total = metric(metrics, priority + "_requests", "count", 0)
        if total != sum(
            metric(metrics, f"phase_{p['index']}_{priority}_requests", "count", 0)
            for p in exp["phases"]
        ):
            reasons.append("priority total: " + priority)
    return reasons


def recovery(rows, priority, required_windows=3, policy=None):
    """Confirmation time after three consecutive full 5s windows meeting observed targets.

    This is an empirical response measure, not a confidence bound on availability.
    """
    candidates = sorted(
        (r for r in rows if r["phase"] == "recovery" and r["priority"] == priority),
        key=lambda r: r["start_seconds"],
    )
    streak = 0
    for row in candidates:
        passed = (
            row["duration_seconds"] == 5
            and row["requests"] > 0
            and row["slo_pct"] >= 95
            and row["success_pct"] >= (99.9 if priority == "critical" else 99)
        )
        if policy and policy.get("latencyPercentile"):
            target_ms = policy["highSloMs"] if priority == "high" else policy["sloMs"]
            latency = row.get(f"p{policy['latencyPercentile']}_ms", math.nan)
            target_success = (
                policy.get("criticalSuccessTargetPercent")
                if priority == "critical"
                else None
            )
            passed = (
                row["duration_seconds"] == 5
                and row["requests"] > 0
                and (
                    row["slo_pct"] >= 90
                    if policy["sloDefinition"] == "TimelySuccessRatio"
                    else math.isfinite(latency) and latency < target_ms
                )
                and (target_success is None or row["success_pct"] >= target_success)
            )
        streak = streak + 1 if passed else 0
        if streak >= required_windows:
            return (
                row["start_seconds"]
                + row["duration_seconds"]
                - candidates[0]["start_seconds"]
            )
    return None


def write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def plot_run(directory, name, exp, rows):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(4, 1, figsize=(12, 12), sharex=True, layout="constrained")
    colors = {
        "critical": "tab:blue",
        "high": "tab:orange",
        "normal": "tab:green",
        "low": "tab:red",
    }
    for priority in (p for p in colors if p in exp["rates"]):
        values = [r for r in rows if r["priority"] == priority]
        x = [r["start_seconds"] + r["duration_seconds"] / 2 for r in values]
        edges = [r["start_seconds"] for r in values] + [exp["durationSeconds"]]
        offered = [r["offered_rps"] for r in values]
        axes[0].step(
            edges,
            offered + offered[-1:],
            where="post",
            label=priority + " offered",
            alpha=0.5,
            color=colors[priority],
            linestyle="--",
        )
        axes[0].plot(
            x,
            [r["goodput_rps"] for r in values],
            marker=".",
            label=priority + " goodput",
            color=colors[priority],
        )
        axes[1].plot(
            x,
            [r[f"p{exp.get('latencyPercentile', 95)}_ms"] for r in values],
            marker=".",
            label=priority,
            color=colors[priority],
        )
        axes[2].plot(
            x,
            [r["slo_pct"] for r in values],
            marker=".",
            label=priority,
            color=colors[priority],
        )
        axes[3].plot(
            x,
            [r["shed_pct"] for r in values],
            marker=".",
            label=priority,
            color=colors[priority],
        )
    axes[1].axhline(exp["sloMs"], color="black", linestyle="--", label="Critical SLO")
    if exp["highSloMs"] != exp["sloMs"]:
        axes[1].axhline(exp["highSloMs"], color="gray", linestyle=":", label="High SLO")
    for ax, label in zip(
        axes,
        (
            "Offered / goodput, RPS",
            f"Successful p{exp.get('latencyPercentile', 95)}, ms",
            "HTTP 200 within budget, %",
            "Shed, %",
        ),
    ):
        ax.set_ylabel(label)
        ax.set_xlim(0, exp["durationSeconds"])
        ax.grid(alpha=0.25)
        ax.legend(fontsize=7, ncol=4)
        for phase in exp["phases"][1:]:
            ax.axvline(phase["startSeconds"], color="gray", linestyle=":")
    axes[-1].set_xlabel("Seconds from k6 test start; requests grouped by start time")
    axes[2].set_ylim(-2, 102)
    axes[3].set_ylim(-2, 102)
    for phase in exp["phases"]:
        axes[0].text(
            phase["startSeconds"],
            1.01,
            phase["name"],
            transform=axes[0].get_xaxis_transform(),
            fontsize=8,
        )
    fig.suptitle(
        f"{name}: {exp['scenario']} / {exp['profile']} / {exp['workload']} / {exp['strategy']}",
        fontsize=11,
    )
    fig.savefig(directory / (name + ".timeline.png"), dpi=140)
    plt.close(fig)


def plot_controllers(directory, name, clock_epoch_ms=math.nan):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with (directory / (name + ".cpu.csv")).open(
        encoding="utf-8-sig", newline=""
    ) as handle:
        samples = list(csv.DictReader(handle))
    if not samples:
        return
    panels = [
        ("Server CPU, % of one core", ["serverCpuPercentOfOneLogicalProcessor"]),
        ("I/O active / waiting", ["ioActive", "ioWaiting"]),
        (
            "Concurrency",
            [
                "cinnamonLimit",
                "cinnamonInflight",
                "cinnamonQueued",
                "adaptiveLimit",
                "adaptiveInflight",
            ],
        ),
        (
            "Controller latency, ms",
            [
                "cinnamonReferenceLatencyMs",
                "cinnamonSampleLatencyMs",
                "adaptiveReferenceLatencyMs",
                "adaptiveSampleLatencyMs",
            ],
        ),
        (
            "PI rejection / error / integral",
            ["cinnamonRejectionRatio", "cinnamonPiError", "cinnamonPiIntegral"],
        ),
    ]
    fig, axes = plt.subplots(
        len(panels), 1, figsize=(12, 12), sharex=True, layout="constrained"
    )
    aligned = math.isfinite(clock_epoch_ms) and all(
        s.get("sampleEpochMs") for s in samples
    )
    seconds = [
        (float(s["sampleEpochMs"]) - clock_epoch_ms) / 1000
        if aligned
        else float(s["elapsedSeconds"].replace(",", "."))
        for s in samples
    ]
    for ax, (title, fields) in zip(axes, panels):
        for field in fields:
            if not any(s.get(field) for s in samples):
                continue
            values = [
                float(s[field].replace(",", ".")) if s.get(field) else math.nan
                for s in samples
            ]
            ax.plot(seconds, values, label=field)
        ax.set_ylabel(title, fontsize=8)
        ax.grid(alpha=0.25)
        if ax.lines:
            ax.legend(fontsize=7)
    axes[-1].set_xlabel(
        "Seconds from k6 test start (wall-clock alignment)"
        if aligned
        else "Seconds from generator process start (includes k6 initialization)"
    )
    fig.suptitle(name)
    fig.savefig(directory / (name + ".controllers.png"), dpi=140)
    plt.close(fig)


def analyze(directory):
    directory = Path(directory)
    phase_rows, window_rows, stream_rows, runs = [], [], [], []
    for path in sorted(directory.glob("r[0-9]*-*.json")):
        if path.name.endswith(".run.json"):
            continue
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        exp, metrics = data["experiment"], data["metrics"]
        if exp.get("schemaVersion") != 2:
            raise ValueError(
                "Use a fresh series: profile analysis requires schemaVersion 2."
            )
        reasons = validate_run(exp, metrics)
        if (
            not path.with_suffix(".cpu.csv").exists()
            or not path.with_suffix(".run.json").exists()
        ):
            reasons.append("missing run artifacts")
        valid = not reasons
        common = {
            "run": path.stem,
            "mode": exp["mode"],
            "strategy": exp["strategy"],
            "repetition": exp["repetition"],
            "profile": exp["profile"],
            "workload": exp["workload"],
            "scenario": exp["scenario"],
            "load_key": path.stem.split("-")[1],
            "valid": valid,
            "slo_definition": exp.get("sloDefinition", "LegacyTimelySuccess"),
            "latency_percentile": exp.get("latencyPercentile", 95),
        }
        local_windows = []
        for phase in exp["phases"]:
            for priority, rate in phase["rates"].items():
                phase_rows.append(
                    dict(
                        **common,
                        phase=phase["name"],
                        priority=priority,
                        start_seconds=phase["startSeconds"],
                        duration_seconds=phase["durationSeconds"],
                        offered_rps=rate,
                        io_service_ms=phase["ioServiceMs"],
                        **outcome(
                            metrics,
                            f"phase_{phase['index']}_{priority}",
                            phase["durationSeconds"],
                        ),
                    )
                )
        for window in exp["windows"]:
            phase = exp["phases"][window["phase"]]
            for priority, rate in phase["rates"].items():
                local_windows.append(
                    dict(
                        **common,
                        phase=phase["name"],
                        priority=priority,
                        start_seconds=window["startSeconds"],
                        duration_seconds=window["durationSeconds"],
                        offered_rps=rate,
                        **outcome(
                            metrics,
                            f"window_{window['index']}_{priority}",
                            window["durationSeconds"],
                        ),
                    )
                )
        window_rows.extend(local_windows)
        for stream in exp["streams"]:
            count = metric(metrics, stream["name"] + "_requests", "count", 0)
            stream_rows.append(
                dict(
                    **common,
                    stream=stream["name"],
                    phase=exp["phases"][stream["phase"]]["name"],
                    expected=stream["rate"] * stream["durationSeconds"],
                    actual=count,
                    **outcome(metrics, stream["name"], stream["durationSeconds"]),
                )
            )
        runs.append(
            dict(
                **common,
                invalid_reasons="; ".join(reasons),
                critical_recovery_confirmed_seconds=recovery(
                    local_windows, "critical", policy=exp
                )
                if valid
                else None,
                high_recovery_confirmed_seconds=recovery(
                    local_windows, "high", policy=exp
                )
                if valid
                else None,
            )
        )
        if valid:
            plot_run(directory, path.stem, exp, local_windows)
            plot_controllers(
                directory, path.stem, metric(metrics, "test_clock_epoch_ms", "med")
            )
    if not runs:
        raise ValueError("No completed runs found.")
    write_csv(directory / "phases.csv", phase_rows)
    write_csv(directory / "windows.csv", window_rows)
    write_csv(directory / "streams.csv", stream_rows)
    write_csv(directory / "runs.csv", runs)
    groups = defaultdict(list)
    for row in phase_rows:
        if row["valid"]:
            groups[
                (row["load_key"], row["mode"], row["phase"], row["priority"])
            ].append(row)
    comparisons = []
    for (load, mode, phase, priority), rows in groups.items():
        result = {
            "load": load,
            "mode": mode,
            "phase": phase,
            "priority": priority,
            "repetitions": len(rows),
        }
        for key in (
            "goodput_rps",
            "success_pct",
            "slo_pct",
            "shed_pct",
            "timeout_pct",
            "p90_ms",
            "p95_ms",
            "p99_ms",
        ):
            values = [r[key] for r in rows if math.isfinite(r[key])]
            result.update(
                {
                    key + "_median": statistics.median(values) if values else math.nan,
                    key + "_min": min(values) if values else math.nan,
                    key + "_max": max(values) if values else math.nan,
                }
            )
        comparisons.append(result)
    write_csv(directory / "comparison.csv", comparisons)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8-sig"))
    label = "rate" if manifest["scenario"] == "SuccessDisaster" else "low"
    expected = {
        f"r{r}-{label}{load}-{mode}"
        for r in range(1, manifest["repetitions"] + 1)
        for load in manifest["loadLevels"]
        for mode in manifest.get("modes", ("off", "on"))
    }
    actual = {r["run"] for r in runs}
    missing, unexpected = expected - actual, actual - expected
    invalid = sum(not r["valid"] for r in runs) + len(missing) + len(unexpected)
    report = [
        f"Completed runs: {len(runs)}; invalid: {invalid}.",
        "phases.csv: per-phase outcomes; windows.csv: arrival-time windows <=5 seconds.",
        "comparison.csv: per-run percentile medians and min/max; no pooled percentiles.",
        (
            "Recovery: third consecutive full 5s window meeting the selected latency/timely-success policy; Critical success target only when explicitly configured. High recovery is descriptive."
            if exp.get("latencyPercentile")
            else "Legacy recovery: third consecutive full 5s window with timely success >=95% and success >=99.9% Critical / >=99% High."
        ),
        "Empty recovery = not observed or not applicable; this is not a statistical availability guarantee.",
        "Goodput counts HTTP 200 within the reporting budget. Successful latency percentiles exclude errors/timeouts; evaluate success rate for every class separately.",
        "DependencySlowdown changes service cost for new arrivals, including their later resource wait; active work keeps its original cost.",
    ]
    report += [f"{r['run']}: {r['invalid_reasons']}" for r in runs if not r["valid"]]
    report += ["Missing run: " + name for name in sorted(missing)]
    report += ["Unexpected run: " + name for name in sorted(unexpected)]
    (directory / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(report[0])
    return invalid
