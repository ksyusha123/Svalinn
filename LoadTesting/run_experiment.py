"""Run paired OFF/ON load experiments on Windows, including from Git Bash."""

import argparse
import csv
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path

import psutil
from runner_common import (
    DLL,
    LOAD_TESTING,
    PRIORITIES,
    ROOT,
    Child,
    build,
    check_masks,
    command,
    directory_lock,
    ensure_port_free,
    executable,
    get_json,
    main_guard,
    read_json,
    sha256,
    stamp,
    utc,
    wait_ready,
    write_json,
)


def bounded(kind, low, high):
    def parse(value):
        number = kind(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"Ожидалось число от {low} до {high}.")
        return number

    return parse


def rates(value):
    try:
        result = [int(part) for part in value.split(",")]
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Ожидался список целых чисел через запятую."
        ) from error
    if not result or len(set(result)) != len(result) or min(result) < 0:
        raise argparse.ArgumentTypeError(
            "Уровни должны быть непустыми, неотрицательными и без повторов."
        )
    return result


def strategy_options(parser):
    parser.add_argument("--strategy", choices=["Inflight", "Cpu"], default="Inflight")
    parser.add_argument(
        "--cpu-sample-interval-ms", type=bounded(int, 50, 60000), default=250
    )
    parser.add_argument(
        "--cpu-smoothing-factor", type=bounded(float, 0.000001, 1), default=0.3
    )
    parser.add_argument(
        "--cpu-overload-percent", type=bounded(float, 0.000001, 100), default=90
    )
    parser.add_argument(
        "--cpu-recovery-percent", type=bounded(float, 0, 99.999999), default=75
    )


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    strategy_options(p)
    p.add_argument(
        "--scenario", choices=["LowFlood", "SuccessDisaster"], default="LowFlood"
    )
    p.add_argument("--workload", choices=["Cpu", "Io", "Mixed", "Delay"], default="Cpu")
    p.add_argument("--rates", type=rates)
    p.add_argument("--low-rates", type=rates)
    p.add_argument("--critical-rate", type=bounded(int, 1, 1000))
    specs = {
        "duration-seconds": (5, 600, 30),
        "repetitions": (1, 20, 3),
        "warmup-requests": (1, 1000, 20),
        "iterations": (1000, 10000000, 100000),
        "min-worker-threads": (1, 256, 16),
        "max-concurrent-requests": (1, 10000, 4),
        "io-slots": (1, 256, 4),
        "io-service-ms": (1, 10000, 100),
        "timeout-seconds": (1, 60, 5),
        "port": (1, 65535, 5026),
        "slo-ms": (1, 60000, None),
        "high-slo-ms": (1, 60000, None),
    }
    for priority, delay in zip(PRIORITIES, (800, 1600, 800, 3000)):
        specs[priority + "-delay-ms"] = (0, 30000, delay)
        specs[priority + "-cpu-percent"] = (0, 100, 50)
    for name, (low, high, default) in specs.items():
        p.add_argument("--" + name, type=bounded(int, low, high), default=default)
    p.add_argument("--server-affinity-mask", type=int, default=1)
    p.add_argument("--generator-affinity-mask", type=int, default=4)
    p.add_argument("--output-directory", type=Path)
    p.add_argument("--python-executable", default=sys.executable)
    for name in ("resume", "skip-build", "analyze"):
        p.add_argument("--" + name, action="store_true")
    return p


def validate(a):
    if a.cpu_recovery_percent >= a.cpu_overload_percent:
        raise ValueError("CpuRecoveryPercent должен быть ниже CpuOverloadPercent.")
    if a.scenario == "SuccessDisaster":
        if a.low_rates is not None or a.critical_rate is not None:
            raise ValueError(
                "Для SuccessDisaster задайте --rates; --low-rates и --critical-rate не применяются."
            )
        a.rates = (
            a.rates if a.rates is not None else [20, 40, 60, 80, 100, 120, 140, 160]
        )
        if min(a.rates) <= 0:
            raise ValueError("--rates должны быть положительными.")
    elif a.rates is not None:
        raise ValueError("Для LowFlood используйте --low-rates и --critical-rate.")
    a.low_rates = (
        a.low_rates if a.low_rates is not None else [5, 15, 25, 35, 45, 55, 70, 85]
    )
    a.critical_rate = a.critical_rate if a.critical_rate is not None else 5
    if a.slo_ms is None:
        a.slo_ms = 1200 if a.workload == "Delay" else 250
    if a.high_slo_ms is None:
        a.high_slo_ms = 2500 if a.workload == "Delay" else a.slo_ms
    if a.resume and a.output_directory is None:
        raise ValueError("Для --resume укажите --output-directory.")
    if a.output_directory is None:
        a.output_directory = (
            LOAD_TESTING
            / "results"
            / f"{a.scenario}-{a.workload}-{a.strategy}-{stamp()}"
        )
    a.output_directory = a.output_directory.resolve()
    check_masks(a.server_affinity_mask, a.generator_affinity_mask)
    return a


def settings(a):
    values = {}
    names = (
        "strategy cpu_sample_interval_ms cpu_smoothing_factor cpu_overload_percent cpu_recovery_percent "
        "workload io_slots io_service_ms warmup_requests scenario duration_seconds repetitions iterations "
        "min_worker_threads max_concurrent_requests critical_delay_ms high_delay_ms normal_delay_ms low_delay_ms "
        "high_slo_ms slo_ms timeout_seconds server_affinity_mask generator_affinity_mask"
    )
    for name in names.split():
        parts = name.split("_")
        values[parts[0] + "".join(s.title() for s in parts[1:])] = getattr(a, name)
    values.update(
        cpuProcessorCapacity=1,
        createdUtc=utc(),
        runner="python-v1",
        cpuPercent={
            p: getattr(a, p + "_cpu_percent")
            if a.workload == "Mixed"
            else (100 if a.workload == "Cpu" else 0)
            for p in PRIORITIES
        },
        loadLevels=a.rates if a.scenario == "SuccessDisaster" else a.low_rates,
        k6Script="k6/success-disaster.js"
        if a.scenario == "SuccessDisaster"
        else "k6/low-flood.js",
        logicalProcessors=os.cpu_count(),
        os=sys.platform,
        dotnetVersion=command([executable("dotnet"), "--version"], True).strip(),
        k6Version=command([executable("k6"), "version"], True).splitlines()[0],
        binarySha256=sha256(DLL),
        processorDescription=os.environ.get("PROCESSOR_IDENTIFIER", ""),
        baseCommit=command(
            [
                "git",
                "-c",
                f"safe.directory={ROOT.as_posix()}",
                "-C",
                ROOT,
                "rev-parse",
                "HEAD",
            ],
            True,
        ).strip(),
    )
    if a.scenario == "SuccessDisaster":
        values.update(
            rates=a.rates, priorityShares=dict(zip(PRIORITIES, (25, 30, 25, 20)))
        )
    else:
        values.update(criticalRate=a.critical_rate, lowRates=a.low_rates)
    return values


def source_files():
    paths = [
        "TestApp/Program.cs",
        "TestApp/CpuBenchmark.cs",
        "TestApp/IoBenchmark.cs",
        "TestApp/DelayBenchmark.cs",
        "TestApp/TestApp.csproj",
        "TestApp/appsettings.json",
        "LoadTesting/k6/low-flood.js",
        "LoadTesting/k6/success-disaster.js",
        "LoadTesting/k6/workload-model.js",
        "LoadTesting/plotting/analyze-results.py",
        "LoadTesting/run_experiment.py",
        "LoadTesting/runner_common.py",
        "LoadTesting/run-experiment.sh",
        "LoadTesting/run-python.sh",
        "LoadTesting/plotting/pyproject.toml",
        "LoadTesting/plotting/uv.lock",
    ]
    paths += [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "Svalinn").rglob("*")
        if p.suffix in (".cs", ".csproj")
        and not {"bin", "obj"} & set(p.relative_to(ROOT).parts)
    ]
    return sorted(paths)


def check_resume(output, current):
    previous = read_json(output / "manifest.json")
    if previous.get("runner") != "python-v1":
        raise ValueError(
            "Каталог создан PowerShell-runner. Для Python начните новую серию в новой папке; старые результаты сохранены."
        )
    ignored = {
        "createdUtc",
        "baseCommit",
        "os",
        "logicalProcessors",
        "processorDescription",
    }
    for key in current.keys() - ignored:
        if current[key] != previous.get(key):
            raise ValueError(f"Нельзя продолжить: изменён параметр {key}.")
    hashes = output / "source-hashes.json"
    if not hashes.exists():
        if (output / "calibration.json").exists() or list(output.glob("r*.run.json")):
            raise ValueError(
                "Нет source-hashes.json для уже начатых измерений. Нужна новая серия."
            )
        return
    for entry in read_json(hashes):
        if entry["Path"] in (
            "LoadTesting/run_experiment.py",
            "LoadTesting/runner_common.py",
            "LoadTesting/run-experiment.sh",
            "LoadTesting/run-python.sh",
            "LoadTesting/plotting/analyze-results.py",
        ):
            continue
        if sha256(ROOT / entry["Path"]) != entry["Sha256"]:
            raise ValueError(f"Нельзя продолжить: изменён исходник {entry['Path']}.")


def snapshot(output, current, resume):
    prefix = "resume-" + stamp() + "-" if resume else ""
    write_json(
        output / (prefix + "settings.json" if resume else "manifest.json"), current
    )
    files = source_files()
    hashes = [{"Path": p, "Sha256": sha256(ROOT / p)} for p in files]
    archive_path = output / (prefix + "experiment-source.zip")
    temporary = archive_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(ROOT / path, path)
    os.replace(temporary, archive_path)
    write_json(output / (prefix + "source-hashes.json"), hashes)
    # Preparation may have stopped before the initial hashes/archive were committed.
    if resume and not (output / "source-hashes.json").exists():
        shutil.copyfile(archive_path, output / "experiment-source.zip")
        write_json(output / "source-hashes.json", hashes)
    return prefix


def start_server(a, enabled, name):
    ensure_port_free(a.port)
    config = {
        "CpuBenchmark:Enabled": a.workload != "Delay",
        "CpuBenchmark:Iterations": a.iterations,
        "CpuBenchmark:MinWorkerThreads": a.min_worker_threads,
        "CpuBenchmark:AffinityMask": a.server_affinity_mask,
        "DelayBenchmark:Slo:CriticalP95LatencyMs": a.slo_ms,
        "DelayBenchmark:Slo:HighP95LatencyMs": a.high_slo_ms,
        "IoBenchmark:Slots": a.io_slots,
        "IoBenchmark:ServiceMs": a.io_service_ms,
        "Svalinn:Enabled": enabled,
        "Svalinn:Strategy": a.strategy,
        "Svalinn:Cpu:SampleIntervalMs": a.cpu_sample_interval_ms,
        "Svalinn:Cpu:SmoothingFactor": a.cpu_smoothing_factor,
        "Svalinn:Cpu:OverloadThresholdPercent": a.cpu_overload_percent,
        "Svalinn:Cpu:RecoveryThresholdPercent": a.cpu_recovery_percent,
        "Svalinn:Cpu:ProcessorCapacity": 1,
        "Svalinn:MaxConcurrentRequests": a.max_concurrent_requests,
        "Svalinn:MinimumPriorityWhenOverloaded": "High",
        "Svalinn:AlwaysAllowCriticalRequests": True,
        "Logging:LogLevel:Default": "Error",
        "Logging:LogLevel:Microsoft.AspNetCore": "Error",
    }
    config.update(
        {
            "DelayBenchmark:" + p.title() + "DelayMs": getattr(a, p + "_delay_ms")
            for p in PRIORITIES
        }
    )
    url = f"http://127.0.0.1:{a.port}"
    args = [executable("dotnet"), DLL, "--urls", url] + [
        f"--{k}={str(v).lower() if isinstance(v, bool) else v}"
        for k, v in config.items()
    ]
    child = Child(
        args,
        a.output_directory,
        name + ".server",
        ROOT / "TestApp",
        a.server_affinity_mask if a.workload == "Delay" else None,
    )

    def probe():
        info = get_json(
            url
            + (
                "/benchmark/delay/info"
                if a.workload == "Delay"
                else "/benchmark/cpu/info"
            ),
            2,
        )
        if info["processId"] != child.process.pid:
            raise RuntimeError("Порт занят другим процессом.")
        if a.workload == "Delay":
            if [p["delayMs"] for p in info["endpoints"]] != [
                getattr(a, p + "_delay_ms") for p in PRIORITIES
            ]:
                raise RuntimeError("Настройки задержек не совпадают.")
        else:
            if (
                info["affinityMask"] != a.server_affinity_mask
                or info["iterations"] != a.iterations
            ):
                raise RuntimeError("Настройки CPU не совпадают.")
            state = get_json(url + "/benchmark/telemetry", 2)
            if state["capacity"] != a.io_slots or state["serviceMs"] != a.io_service_ms:
                raise RuntimeError("Настройки I/O не совпадают.")

    try:
        wait_ready(child, probe)
        return child
    except BaseException:
        child.close()
        raise


def warm_up(a, kind=None):
    kind = kind or a.workload
    for i in range(1, a.warmup_requests + 1):
        route = ("cpu" if i % 2 else "io") if kind == "Mixed" else kind.lower()
        get_json(
            f"http://127.0.0.1:{a.port}/benchmark/{route}/critical",
            max(15, a.critical_delay_ms / 1000 + 5),
        )


def calibrate(a):
    with start_server(a, False, "calibration") as server:
        warm_up(a, "Cpu")
        start_cpu = server.cpu_time()
        latencies = []
        url = f"http://127.0.0.1:{a.port}/benchmark/cpu/"
        for _ in range(40):
            start = time.perf_counter()
            get_json(url + "critical", 10)
            latencies.append((time.perf_counter() - start) * 1000)
        cpu_per_request = (server.cpu_time() - start_cpu) / 40
        if cpu_per_request <= 0:
            raise RuntimeError(
                "Вычислительная работа ниже точности учёта CPU; увеличьте --iterations."
            )
        checksums = {get_json(url + p)["checksum"] for p in PRIORITIES}
        if len(checksums) != 1:
            raise RuntimeError("Вычислительная работа различается между приоритетами.")
        write_json(
            a.output_directory / "calibration.json",
            {
                "cpuSecondsPerRequest": cpu_per_request,
                "estimatedSingleCpuCapacityRps": 1 / cpu_per_request,
                "meanSerialLatencyMs": sum(latencies) / len(latencies),
                "checksum": checksums.pop(),
                "samples": 40,
                "ioSlots": a.io_slots,
                "ioServiceMs": a.io_service_ms,
                "theoreticalIoCapacityRps": 1000 * a.io_slots / a.io_service_ms,
            },
        )
        print(
            f"Калибровка: {cpu_per_request * 1000:.1f} мс CPU на запрос; примерно {1 / cpu_per_request:.1f} запросов/с",
            flush=True,
        )


def telemetry(a):
    return get_json(f"http://127.0.0.1:{a.port}/benchmark/telemetry", 2)


def run_one(a, current, repetition, load, mode, name, prefix):
    print(f"Выполняется прогон {name}", flush=True)
    output = a.output_directory
    with start_server(a, mode == "on", name) as server:
        warm_up(a)
        before = telemetry(a) if a.workload != "Delay" else None
        env = os.environ.copy()
        values = {
            "BASE_URL": f"http://127.0.0.1:{a.port}",
            "CRITICAL_RATE": a.critical_rate,
            "LOW_RATE": load,
            "RATE": load,
            "DURATION_SECONDS": a.duration_seconds,
            "SLO_MS": a.slo_ms,
            "HIGH_SLO_MS": a.high_slo_ms,
            "TIMEOUT_SECONDS": a.timeout_seconds,
            "WORKLOAD": a.workload,
            "MODE": mode,
            "STRATEGY": a.strategy,
            "REPETITION": repetition,
            "SUMMARY_PATH": str(output / (name + ".json")),
        }
        values.update(
            {
                p.upper() + "_CPU_PERCENT": getattr(a, p + "_cpu_percent")
                for p in PRIORITIES
            }
        )
        env.update({k: str(v) for k, v in values.items()})
        samples = []
        with Child(
            [
                executable("k6"),
                "run",
                "--quiet",
                "--address",
                "127.0.0.1:0",
                LOAD_TESTING / current["k6Script"],
            ],
            output,
            name + ".k6",
            affinity=a.generator_affinity_mask,
            env=env,
        ) as generator:
            started = time.perf_counter()
            last_time, last_cpu, last_generator_cpu = (
                0,
                server.cpu_time(),
                generator.cpu_time(),
            )
            while generator.process.poll() is None:
                time.sleep(0.5)
                state = None
                if a.workload in ("Io", "Mixed"):
                    try:
                        state = get_json(
                            f"http://127.0.0.1:{a.port}/benchmark/telemetry", 1
                        )
                    except (OSError, ValueError, RuntimeError):
                        pass
                if server.process.poll() is not None:
                    raise RuntimeError("Сервер завершился во время нагрузочного теста.")
                now, cpu = time.perf_counter() - started, server.cpu_time()
                # A short run can exit between polling and reading its process counters.
                try:
                    generator_cpu = generator.cpu_time()
                except psutil.NoSuchProcess:
                    generator_cpu = last_generator_cpu
                samples.append(
                    {
                        "elapsedSeconds": now,
                        "serverCpuPercentOfOneLogicalProcessor": 100
                        * (cpu - last_cpu)
                        / (now - last_time),
                        "serverWorkingSetBytes": server.stats.memory_info().rss,
                        "serverThreads": server.stats.num_threads(),
                        "generatorCpuPercentOfOneLogicalProcessor": 100
                        * (generator_cpu - last_generator_cpu)
                        / (now - last_time),
                        "ioActive": state["active"] if state else None,
                        "ioWaiting": state["waiting"] if state else None,
                    }
                )
                last_time, last_cpu, last_generator_cpu = now, cpu, generator_cpu
                if now > a.duration_seconds + a.timeout_seconds + 60:
                    raise RuntimeError(
                        "Генератор нагрузки превысил допустимое время работы."
                    )
            exit_code = generator.process.wait()
        if not samples:
            raise RuntimeError(
                f"k6 завершился до сбора телеметрии; проверьте {name}.k6.err"
            )
        with (output / (name + ".cpu.csv")).open(
            "w", newline="", encoding="utf-8"
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=list(samples[0]))
            writer.writeheader()
            writer.writerows(samples)
        if exit_code not in (0, 99) or not (output / (name + ".json")).exists():
            raise RuntimeError(
                f"k6 завершился с кодом {exit_code}; проверьте {name}.k6.err"
            )
        after = None
        if a.workload != "Delay":
            for _ in range(3):
                try:
                    after = telemetry(a)
                    break
                except (OSError, ValueError, RuntimeError):
                    pass
            if after is None:
                print(f"Предупреждение: нет итоговой телеметрии {name}.", flush=True)
        write_json(
            output / (name + ".run.json"),
            {
                "exitCode": exit_code,
                "serverAffinityMask": sum(1 << i for i in server.stats.cpu_affinity()),
                "generatorAffinityMask": a.generator_affinity_mask,
                "ioBefore": before,
                "ioAfter": after,
                "ioTelemetryComplete": after is not None,
                "sourceSnapshotPrefix": prefix,
            },
        )


def run(a):
    a = validate(a)
    output = a.output_directory
    executable("dotnet")
    executable("k6")
    if a.analyze:
        command(
            [
                a.python_executable,
                LOAD_TESTING / "plotting/analyze-results.py",
                "--help",
            ],
            True,
        )
    if output.exists() and not a.resume:
        raise ValueError(f"Для новой серии укажите новую папку: {output}")
    if a.resume and not (output / "manifest.json").exists():
        if not output.is_dir() or any(
            p.name not in {"running.lock", "manifest.json.tmp"}
            for p in output.iterdir()
        ):
            raise ValueError(
                "Не найден manifest.json прерванной серии; каталог содержит неизвестные данные."
            )
        a.resume = False
    if not a.skip_build:
        build()
    if not DLL.exists():
        raise ValueError("Нет Release-сборки TestApp.")
    output.mkdir(parents=True, exist_ok=True)
    with directory_lock(output):
        if not a.resume and (output / "manifest.json").exists():
            raise ValueError(
                "Каталог уже подготовлен другим запуском. Используйте --resume."
            )
        current = settings(a)
        if a.resume:
            check_resume(output, current)
        prefix = snapshot(output, current, a.resume)
        if a.workload != "Delay" and not (output / "calibration.json").exists():
            calibrate(a)
        for repetition in range(1, a.repetitions + 1):
            for load in current["loadLevels"]:
                for mode in ("off", "on") if repetition % 2 else ("on", "off"):
                    name = f"r{repetition}-{'rate' if a.scenario == 'SuccessDisaster' else 'low'}{load}-{mode}"
                    if a.resume:
                        if all(
                            (output / (name + ext)).exists()
                            for ext in (".json", ".cpu.csv", ".run.json")
                        ):
                            print(f"Пропущен завершённый прогон {name}", flush=True)
                            continue
                        partial = list(output.glob(name + ".*"))
                        if partial:
                            backup = output / (prefix + "incomplete-" + name)
                            backup.mkdir()
                            for path in partial:
                                path.rename(backup / path.name)
                    run_one(a, current, repetition, load, mode, name, prefix)
        if a.analyze:
            command(
                [
                    a.python_executable,
                    LOAD_TESTING / "plotting/analyze-results.py",
                    output,
                ]
            )
    print(f"Результаты: {output}", flush=True)


if __name__ == "__main__":
    main_guard(lambda: run(parser().parse_args()))
