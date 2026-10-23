"""Run the fixed CPU/I/O/Mixed confirmation matrix."""

import argparse
import os
import shutil
import sys
from pathlib import Path

import run_experiment
from runner_common import (
    LOAD_TESTING,
    build,
    check_masks,
    command,
    directory_lock,
    executable,
    main_guard,
    read_json,
    utc,
    write_json,
)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    run_experiment.strategy_options(p)
    p.add_argument(
        "--duration-seconds", type=run_experiment.bounded(int, 5, 600), default=30
    )
    p.add_argument("--repetitions", type=run_experiment.bounded(int, 1, 20), default=5)
    p.add_argument(
        "--cpu-rates", type=run_experiment.rates, default=[60, 80, 100, 120, 130]
    )
    p.add_argument(
        "--io-rates", type=run_experiment.rates, default=[40, 50, 60, 70, 80]
    )
    p.add_argument(
        "--mixed-rates", type=run_experiment.rates, default=[100, 120, 140, 160]
    )
    p.add_argument("--output-directory", type=Path, required=True)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--python-executable", default=sys.executable)
    return p


def run(a):
    if a.cpu_recovery_percent >= a.cpu_overload_percent:
        raise ValueError("CpuRecoveryPercent должен быть ниже CpuOverloadPercent.")
    strategy = {
        k: v
        for k, v in vars(a).items()
        if k == "strategy" or k.startswith("cpu_") and k != "cpu_rates"
    }
    matrix = {
        "strategy": a.strategy,
        "cpuSampleIntervalMs": a.cpu_sample_interval_ms,
        "cpuSmoothingFactor": a.cpu_smoothing_factor,
        "cpuOverloadPercent": a.cpu_overload_percent,
        "cpuRecoveryPercent": a.cpu_recovery_percent,
        "scenario": "SuccessDisaster",
        "durationSeconds": a.duration_seconds,
        "repetitions": a.repetitions,
        "runner": "python-v1",
        "purpose": "Подтверждение границ фиксированной стратегии; параметры выбраны до измерений.",
        "series": [
            {
                "workload": kind,
                "directory": kind.lower(),
                "rates": getattr(a, kind.lower() + "_rates"),
            }
            for kind in ("Cpu", "Io", "Mixed")
        ],
    }
    if any(min(s["rates"]) <= 0 for s in matrix["series"]):
        raise ValueError("Все уровни матрицы должны быть положительными.")
    output = a.output_directory.resolve()
    if output.exists() and not a.resume:
        raise ValueError(
            "Для новой матрицы нужна новая папка; для продолжения используйте --resume."
        )
    if a.resume and not (output / "matrix.json").exists():
        if not output.is_dir() or any(
            p.name not in {"running.lock", "matrix.json.tmp"} for p in output.iterdir()
        ):
            raise ValueError(
                "Не найдена matrix.json для продолжения; каталог содержит неизвестные данные."
            )
        a.resume = False
    if a.resume:
        saved = read_json(output / "matrix.json")
        if saved.get("runner") != "python-v1":
            raise ValueError(
                "Матрица создана PowerShell-runner. Начните Python-матрицу в новой папке."
            )
        if saved != matrix:
            raise ValueError("Параметры отличаются от сохранённой матрицы.")
    # Check all prerequisites before creating a matrix or starting a build.
    check_masks(1, 4)
    executable("dotnet")
    executable("k6")
    command(
        [a.python_executable, LOAD_TESTING / "plotting/analyze-results.py", "--help"],
        True,
    )
    output.mkdir(parents=True, exist_ok=True)
    with directory_lock(output):
        if not a.resume and (output / "matrix.json").exists():
            raise ValueError(
                "Каталог уже подготовлен другим запуском. Используйте --resume."
            )
        if not a.resume:
            write_json(output / "matrix.json", matrix)
            for source, destination in [
                ("run_confirmatory_experiments.py", "runner-source.py"),
                ("summarize-confirmatory-results.py", "summary-source.py"),
            ]:
                shutil.copyfile(LOAD_TESTING / source, output / destination)
        # Recover an interruption during the initial source-copy stage.
        for source, destination in [
            ("run_confirmatory_experiments.py", "runner-source.py"),
            ("summarize-confirmatory-results.py", "summary-source.py"),
        ]:
            if not (output / destination).exists():
                shutil.copyfile(LOAD_TESTING / source, output / destination)
        total = sum(len(s["rates"]) for s in matrix["series"]) * a.repetitions * 2
        status = {
            "state": "running",
            "processId": os.getpid(),
            "totalRuns": total,
            "durationSeconds": a.duration_seconds,
            "repetitions": a.repetitions,
            "completedSeries": [],
        }

        def save():
            status["updatedUtc"] = utc()
            write_json(output / "status.json", status)

        print(
            f"Матрица: {total} прогонов по {a.duration_seconds} с; {a.repetitions} повторов OFF/ON.",
            flush=True,
        )
        try:
            save()
            build(no_restore=True)
            for series in matrix["series"]:
                status["currentWorkload"] = series["workload"]
                save()
                directory = output / series["directory"]
                resume = a.resume and directory.exists()
                if (
                    directory.exists()
                    and not resume
                    and any(p.name != "running.lock" for p in directory.iterdir())
                ):
                    raise ValueError(
                        f"Нет manifest.json, но каталог содержит данные: {directory}"
                    )
                options = run_experiment.parser().parse_args(
                    [
                        "--scenario",
                        "SuccessDisaster",
                        "--workload",
                        series["workload"],
                        "--rates",
                        ",".join(map(str, series["rates"])),
                        "--duration-seconds",
                        str(a.duration_seconds),
                        "--repetitions",
                        str(a.repetitions),
                        "--skip-build",
                        "--analyze",
                        "--output-directory",
                        str(directory),
                        "--python-executable",
                        a.python_executable,
                    ]
                    + (["--resume"] if resume else [])
                )
                for key, value in strategy.items():
                    setattr(options, key, value)
                run_experiment.run(options)
                status["completedSeries"].append(series["workload"])
                save()
            command([a.python_executable, output / "summary-source.py", output])
            status["state"] = "completed"
            save()
        except BaseException as error:
            status.update(
                state="interrupted"
                if isinstance(error, KeyboardInterrupt)
                else "failed",
                error=str(error) or type(error).__name__,
            )
            save()
            raise


if __name__ == "__main__":
    main_guard(lambda: run(parser().parse_args()))
