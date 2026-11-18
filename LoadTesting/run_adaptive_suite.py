"""Predeclared adaptive-shedding matrix, scaled by a separate CPU calibration."""

import argparse
import csv
from pathlib import Path

import run_experiment as runner
from runner_common import build, directory_lock, main_guard, read_json, write_json


def case_name(index, case):
    return f"{index + 1:02}-{case['workload']}-{case['profile']}-{case['mode']}-{case['strategy']}"


def plan(capacity, smoke=False, repetitions=3):
    cases = []
    for workload, cap in (("Cpu", capacity), ("Io", 4000 / 150)):
        profiles = ["Plateau", "Spike", "OverloadRecovery"]
        if workload == "Io":
            profiles += ["DependencySlowdown", "CompositionShift"]
        for profile in profiles:
            if profile == "Plateau":
                levels = sorted(
                    {max(1, round(cap * fraction)) for fraction in (0.6, 1, 2)}
                )
            elif profile == "DependencySlowdown":
                levels = [19]
            elif profile == "CompositionShift":
                levels = [max(1, round(cap * 1.1))]
            else:
                levels = [max(2, round(cap * 1.5))]
            if smoke and profile == "Plateau":
                levels = levels[:1]
            control = case_name(
                len(cases),
                {
                    "workload": workload,
                    "profile": profile,
                    "mode": "off",
                    "strategy": "Inflight",
                },
            )
            for mode, strategy in (
                ("off", "Inflight"),
                ("on", "Inflight"),
                ("on", "Adaptive"),
                ("on", "Cinnamon"),
            ):
                cases.append(
                    {
                        "workload": workload,
                        "strategy": strategy,
                        "profile": profile,
                        "scenario": "SuccessDisaster",
                        "levels": levels,
                        "baseline": max(1, round(cap * 0.5)),
                        "mode": mode,
                        "controlCase": control,
                        "ablation": None,
                    }
                )
    matrix = {
        "schemaVersion": 5,
        "purpose": "Reduced adaptive shedding study with shared OFF controls",
        "cpuCapacityRps": capacity,
        "repetitions": 1 if smoke else repetitions,
        "durationSeconds": 5 if smoke else 120,
        "plateauSeconds": 5 if smoke else 60,
        "baselineSeconds": 5 if smoke else 30,
        "recoverySeconds": 15 if smoke else 90,
        "spikeSeconds": 2 if smoke else 10,
        "cases": cases,
        "smoke": smoke,
        "evaluation": {
            "sloDefinition": "SuccessfulLatency",
            "latencyPercentile": 90,
            "criticalLatencyTargetMs": 1000,
            "otherLatencyReportingMs": 1000,
            "criticalSuccessTargetPercent": None,
        },
        "cinnamonStart": {
            "InitialLimit": 4,
            "QueueCapacity": 16,
            "QueueTimeoutMs": 500,
        },
    }
    matrix["runCount"] = sum(len(c["levels"]) * matrix["repetitions"] for c in cases)
    matrix["loadSeconds"] = sum(
        len(c["levels"])
        * matrix["repetitions"]
        * (
            matrix["plateauSeconds"]
            if c["profile"] == "Plateau"
            else matrix["baselineSeconds"]
            + matrix["recoverySeconds"]
            + (
                matrix["spikeSeconds"]
                if c["profile"] == "Spike"
                else matrix["durationSeconds"]
            )
        )
        for c in cases
    )
    return matrix


def case_arguments(case, matrix, folder):
    args = [
        "--scenario",
        case["scenario"],
        "--workload",
        case["workload"],
        "--strategy",
        case["strategy"],
        "--profile",
        case["profile"],
        "--modes",
        case["mode"],
        "--baseline-rate",
        str(case["baseline"]),
        "--rates" if case["scenario"] == "SuccessDisaster" else "--low-rates",
        ",".join(map(str, case["levels"])),
        "--duration-seconds",
        str(
            matrix["plateauSeconds"]
            if case["profile"] == "Plateau"
            else matrix["durationSeconds"]
        ),
        "--baseline-seconds",
        str(matrix["baselineSeconds"]),
        "--recovery-seconds",
        str(matrix["recoverySeconds"]),
        "--spike-seconds",
        str(matrix["spikeSeconds"]),
        "--repetitions",
        str(matrix["repetitions"]),
        "--warmup-requests",
        "2" if matrix["smoke"] else "20",
        "--skip-build",
        "--analyze",
        "--output-directory",
        str(folder),
    ]
    evaluation = matrix["evaluation"]
    args += [
        "--slo-ms",
        str(evaluation["criticalLatencyTargetMs"]),
        "--high-slo-ms",
        str(evaluation["otherLatencyReportingMs"]),
        "--slo-definition",
        evaluation["sloDefinition"],
        "--cinnamon-initial-limit",
        str(matrix["cinnamonStart"]["InitialLimit"]),
        "--cinnamon-queue-capacity",
        str(matrix["cinnamonStart"]["QueueCapacity"]),
        "--cinnamon-queue-timeout-ms",
        str(matrix["cinnamonStart"]["QueueTimeoutMs"]),
    ]
    if evaluation["criticalSuccessTargetPercent"] is not None:
        args += [
            "--critical-success-target-percent",
            str(evaluation["criticalSuccessTargetPercent"]),
        ]
    if case["scenario"] == "LowFlood":
        args += [
            "--critical-rate",
            str(
                max(
                    1,
                    round(
                        (matrix["cpuCapacityRps"] if case["workload"] == "Cpu" else 4000 / 150)
                        * 0.1
                    ),
                )
            ),
        ]
    if case["ablation"]:
        args.append("--cinnamon-disable-" + case["ablation"])
    return args


def run(a):
    output = a.output_directory.resolve()
    if output.exists() and not a.resume:
        raise ValueError("Use a new output directory or --resume.")
    if a.resume and not (output / "matrix.json").exists():
        raise ValueError("Cannot resume without matrix.json.")
    output.mkdir(parents=True, exist_ok=True)
    with directory_lock(output):
        if a.resume:
            matrix = read_json(output / "matrix.json")
            if matrix["smoke"] != a.smoke or matrix["repetitions"] != (
                1 if a.smoke else a.repetitions
            ):
                raise ValueError(
                    "Repeat the original --smoke / --repetitions settings."
                )
        else:
            build()
            calibration_dir = output / "calibration"
            calibration_dir.mkdir()
            config = runner.validate(
                runner.parser().parse_args(["--output-directory", str(calibration_dir)])
            )
            runner.calibrate(config)
            calibration = read_json(calibration_dir / "calibration.json")
            matrix = plan(
                calibration["estimatedSingleCpuCapacityRps"], a.smoke, a.repetitions
            )
            write_json(output / "matrix.json", matrix)
        if matrix.get("schemaVersion") != 5:
            raise ValueError("Start a fresh randomized I/O matrix; schemaVersion 5 required.")
        print(
            f"Planned runs: {matrix['runCount']}; load time: {matrix['loadSeconds'] / 3600:.2f} hours",
            flush=True,
        )
        statuses = []
        for index, case in enumerate(matrix["cases"]):
            name = case_name(index, case)
            if case["ablation"]:
                name += "-without-" + case["ablation"]
            folder = output / name
            args = case_arguments(case, matrix, folder)
            if a.resume and (folder / "manifest.json").exists():
                args.append("--resume")
            write_json(
                output / "status.json",
                {"state": "running", "current": name, "completed": statuses},
            )
            try:
                runner.run(runner.parser().parse_args(args))
                statuses.append(name)
                summarize(output, statuses)
            except BaseException as error:
                write_json(
                    output / "status.json",
                    {
                        "state": "failed",
                        "current": name,
                        "completed": statuses,
                        "error": str(error),
                    },
                )
                raise
        summarize(output, statuses)
        write_json(output / "status.json", {"state": "complete", "completed": statuses})
        (output / "report.txt").write_text(
            f"Completed cases: {len(statuses)}. Smoke protocol: {matrix['smoke']}.\n"
            "Shared OFF per workload/profile/level/repetition; all phases within a run share a process.\n"
            "Inspect per-case comparison.csv, runs.csv and timelines. Violated SLO is a result, not invalid input.\n"
            "CPU capacity is an estimate; I/O 26.7 RPS is theoretical (150 +/- 50 ms). Do not interpret sampled levels as exact capacity.\n",
            encoding="utf-8",
        )


def summarize(output, statuses):
    matrix = (
        read_json(output / "matrix.json") if (output / "matrix.json").exists() else None
    )
    controls = (
        {case_name(i, c): c["controlCase"] for i, c in enumerate(matrix["cases"])}
        if matrix
        else {}
    )
    for filename in ("comparison.csv", "runs.csv"):
        rows = []
        for name in statuses:
            with (output / name / filename).open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                rows += [
                    dict(case=name, control_case=controls.get(name, ""), **row)
                    for row in csv.DictReader(handle)
                ]
        if not rows:
            raise ValueError("Missing matrix results: " + filename)
        with (output / ("suite-" + filename)).open(
            "w", encoding="utf-8-sig", newline=""
        ) as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-directory", type=Path, required=True)
    p.add_argument("--resume", action="store_true")
    p.add_argument(
        "--smoke",
        action="store_true",
        help="Short infrastructure verification, not scientific evidence",
    )
    p.add_argument("--repetitions", type=runner.bounded(int, 1, 20), default=3)
    main_guard(lambda: run(p.parse_args()))
