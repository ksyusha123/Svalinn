import argparse
import csv
import io
import json
import math
import statistics
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

MODE_LABELS = {"off": "Svalinn выключен", "on": "Svalinn включён"}
CSV_LABELS = {
    "run": "Прогон",
    "mode": "Режим",
    "repetition": "Повтор",
    "offered_rps": "Заданная нагрузка, запросов/с",
    "duration_seconds": "Длительность, с",
    "slo_ms": "Целевой SLO, мс",
    "cpu_pct": "CPU сервера, % одного логического процессора",
    "generator_cpu_pct": "CPU генератора, % одного логического процессора",
    "dropped_iterations": "Пропущенные итерации k6",
    "valid_arrival_rate": "Заданная нагрузка достигнута",
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def number(text):
    # PowerShell Export-Csv может использовать десятичный разделитель системы.
    return float(text.replace(",", "."))


def value(metrics, metric, statistic, default=math.nan):
    return metrics.get(metric, {}).get("values", {}).get(statistic, default)


def io_delta(io_before, io_after, key):
    return (
        io_after[key] - io_before[key]
        if key in io_after and key in io_before
        else math.nan
    )


def measurement(
    name,
    priority,
    kind,
    rate,
    *,
    path,
    exp,
    metrics,
    cpu,
    generator_cpu,
    io_values,
    dropped,
    valid,
):
    return {
        "run": path.stem,
        "mode": exp["mode"],
        "repetition": exp["repetition"],
        "priority": priority,
        "workload": kind,
        "offered_rps": exp["totalRate"],
        "priority_rps": rate,
        "duration_seconds": exp["durationSeconds"],
        "slo_ms": exp["highSloMs"] if priority == "high" else exp["sloMs"],
        "requests": value(metrics, f"{name}_requests", "count", 0),
        "expected_requests": rate * exp["durationSeconds"],
        "p95_ms": value(metrics, f"{name}_success_latency_ms", "p(95)"),
        "p99_ms": value(metrics, f"{name}_success_latency_ms", "p(99)"),
        "all_outcomes_p95_ms": value(metrics, f"{name}_latency_ms", "p(95)"),
        "success_pct": 100 * value(metrics, f"{name}_success", "rate"),
        "slo_pct": 100 * value(metrics, f"{name}_slo", "rate"),
        "shed_pct": 100 * value(metrics, f"{name}_shed", "rate"),
        "timeout_pct": 100 * value(metrics, f"{name}_timeout", "rate"),
        "errors": value(metrics, f"{name}_errors", "count", 0),
        "goodput_rps": value(metrics, f"{name}_good", "count", 0)
        / exp["durationSeconds"],
        "cpu_pct": cpu,
        "generator_cpu_pct": generator_cpu,
        **io_values,
        "dropped_iterations": dropped,
        "valid_arrival_rate": valid,
    }


def median(valid_rows, load, mode, priority, key):
    values = [
        r[key]
        for r in valid_rows
        if r["offered_rps"] == load
        and r["mode"] == mode
        and r["priority"] == priority
        and math.isfinite(r[key])
    ]
    return statistics.median(values) if values else math.nan


def plot_cinnamon_timeseries(directory, run_names):
    for run in sorted(run_names):
        path = directory / (run + ".cpu.csv")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            samples = list(csv.DictReader(stream))
        if not samples or "cinnamonLimit" not in samples[0]:
            continue
        seconds = [number(s["elapsedSeconds"]) for s in samples]
        panels = [
            ("Конкурентность", [("cinnamonLimit", "Лимит"), ("cinnamonInflight", "Выполняются")], 1),
            ("Ожидают в очереди Cinnamon", [("cinnamonQueued", "Запросы")], 1),
            ("Целевая доля сброса, %", [("cinnamonRejectionRatio", "PI")], 100),
            ("Выполнение без очереди, мс", [("cinnamonReferenceLatencyMs", "Опорная"), ("cinnamonSampleLatencyMs", "Сглаженная p90")], 1),
            ("PI-регулятор", [("cinnamonPiError", "Ошибка"), ("cinnamonPiIntegral", "Интеграл")], 1),
            ("Ковариация / сбросы опорной задержки", [("cinnamonCovariance", "Ковариация"), ("cinnamonReferenceResets", "Сбросы")], 1),
        ]
        fig, axes = plt.subplots(3, 2, figsize=(12, 10), layout="constrained")
        for ax, (title, fields, scale) in zip(axes.flat, panels):
            for field, label in fields:
                values = [number(s[field]) * scale if s.get(field) else math.nan for s in samples]
                ax.plot(seconds, values, label=label)
            ax.set_title(title, fontsize=10)
            ax.set_xlabel("Время от старта k6, с")
            ax.grid(alpha=.25)
            ax.legend(fontsize=8)
        fig.suptitle(f"Cinnamon без когорт: {run}; группа default", fontsize=11)
        fig.savefig(directory / (run + ".cinnamon.png"), dpi=160)
        plt.close(fig)


def analyze(directory):
    """Каждый поток проверяется отдельно: недостигнутая нагрузка исключает весь прогон."""
    manifest = read_json(directory / "manifest.json")
    priorities = ("critical", "high", "normal", "low")
    rows, runs, stream_rows = [], [], []
    for path in sorted(directory.glob("r[0-9]*-*.json")):
        if path.name.endswith(".run.json"):
            continue
        data = read_json(path)
        exp, metrics = data["experiment"], data["metrics"]

        with path.with_suffix(".cpu.csv").open(
            encoding="utf-8-sig", newline=""
        ) as handle:
            samples = [
                s
                for s in csv.DictReader(handle)
                if 2 <= number(s["elapsedSeconds"]) <= exp["durationSeconds"]
            ]
        cpu = (
            statistics.mean(
                number(s["serverCpuPercentOfOneLogicalProcessor"]) for s in samples
            )
            if samples
            else math.nan
        )
        generator_cpu = (
            statistics.mean(
                number(s["generatorCpuPercentOfOneLogicalProcessor"]) for s in samples
            )
            if samples
            else math.nan
        )
        dropped = value(metrics, "dropped_iterations", "count", 0)
        priorities = tuple(
            p for p in ("critical", "high", "normal", "low") if p in exp["rates"]
        )
        scheduled_streams = exp["streamRates"]
        valid = dropped == 0 and all(
            abs(
                value(metrics, f"{p}_requests", "count", 0)
                - rate * exp["durationSeconds"]
            )
            <= 1 + 1e-9
            for p, rate in scheduled_streams.items()
        )
        valid = valid and all(
            value(metrics, f"{p}_requests", "count", 0)
            == sum(
                value(metrics, f"{name}_requests", "count", 0)
                for name in exp["streamRates"]
                if name.startswith(p + "_")
            )
            for p in priorities
        )
        io_samples = [sample for sample in samples if sample["ioActive"] != ""]
        run_metadata = read_json(path.with_name(path.stem + ".run.json"))
        io_before, io_after = (
            run_metadata["ioBefore"] or {},
            run_metadata["ioAfter"] or {},
        )

        io_values = {
            "io_active_mean": statistics.mean(number(x["ioActive"]) for x in io_samples)
            if io_samples
            else math.nan,
            "io_waiting_mean": statistics.mean(
                number(x["ioWaiting"]) for x in io_samples
            )
            if io_samples
            else math.nan,
            "io_waiting_peak": max(
                (number(x["ioWaiting"]) for x in io_samples), default=math.nan
            ),
            "io_wait_p95_ms": value(metrics, "io_wait_ms", "p(95)"),
            "io_service_p95_ms": value(metrics, "io_service_ms", "p(95)"),
            "io_completed": io_delta(io_before, io_after, "completed"),
            "io_sample_count": len(io_samples),
        }
        runs.append(
            {
                "run": path.stem,
                "mode": exp["mode"],
                "offered_rps": exp["totalRate"],
                "valid": valid,
                "cpu_pct": cpu,
                "generator_cpu_pct": generator_cpu,
            }
        )

        for priority in priorities:
            rows.append(
                measurement(
                    priority,
                    priority,
                    exp["workload"],
                    exp["rates"][priority],
                    path=path,
                    exp=exp,
                    metrics=metrics,
                    cpu=cpu,
                    generator_cpu=generator_cpu,
                    io_values=io_values,
                    dropped=dropped,
                    valid=valid,
                )
            )
        for name, rate in exp["streamRates"].items():
            priority, kind = name.split("_")
            stream_rows.append(
                measurement(
                    name,
                    priority,
                    kind,
                    rate,
                    path=path,
                    exp=exp,
                    metrics=metrics,
                    cpu=cpu,
                    generator_cpu=generator_cpu,
                    io_values=io_values,
                    dropped=dropped,
                    valid=valid,
                )
            )
    if not rows:
        raise SystemExit("Завершённые прогоны не найдены.")
    labels = {
        **CSV_LABELS,
        "priority": "Приоритет",
        "priority_rps": "Заданная интенсивность строки, запросов/с",
        "workload": "Характер работы",
        "io_active_mean": "Среднее число занятых мест I/O",
        "io_waiting_mean": "Средняя очередь I/O",
        "io_waiting_peak": "Максимальная наблюдаемая очередь I/O",
        "io_wait_p95_ms": "I/O p95 ожидания места успешными запросами, мс",
        "io_service_p95_ms": "I/O p95 обслуживания успешных запросов, мс",
        "io_completed": "Завершённые обслуживания I/O на сервере",
        "io_sample_count": "Число отсчётов I/O",
        "requests": "Завершённые запросы",
        "expected_requests": "Запросы по расписанию",
        "p95_ms": "p95 успешных ответов, мс",
        "p99_ms": "p99 успешных ответов, мс",
        "all_outcomes_p95_ms": "p95 всех исходов, мс",
        "success_pct": "Успешные ответы, %",
        "slo_pct": "Успешные ответы в пределах SLO, %",
        "shed_pct": "Отказы 503, %",
        "timeout_pct": "Тайм-ауты, %",
        "errors": "Неуспешные запросы, включая 503",
        "goodput_rps": "Полезная производительность, запросов/с",
    }
    with (directory / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    with (directory / "summary-ru.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow([labels[key] for key in rows[0]])
        for row in rows:
            writer.writerow(
                [
                    MODE_LABELS[v]
                    if k == "mode"
                    else v.capitalize()
                    if k == "priority"
                    else ("да" if v else "нет")
                    if k == "valid_arrival_rate"
                    else v
                    for k, v in row.items()
                ]
            )

    valid_rows = [r for r in rows if r["valid_arrival_rate"]]
    expected_runs = len(manifest["loadLevels"]) * manifest["repetitions"] * 2
    invalid = sum(not r["valid"] for r in runs)
    workload = manifest["workload"].lower()
    workload_label = {
        "delay": "Delay: независимые асинхронные задержки",
        "cpu": "CPU",
        "io": "I/O: общий семафор",
        "mixed": "Mixed: отдельные CPU- и I/O-запросы",
    }[workload]
    composition = (
        "Critical 25%, High 30%, Normal 25%, Low 20%"
        if manifest["scenario"] == "SuccessDisaster"
        else f"Critical {manifest['criticalRate']} запросов/с, растущий Low"
    )
    if workload == "mixed":
        composition += "; CPU внутри приоритетов: " + "/".join(
            str(manifest["cpuPercent"][p]) + "%" for p in priorities
        )
    caption = (
        f"{manifest['scenario']} — {workload_label}; {manifest['repetitions']} повтора × {manifest['durationSeconds']} с\n"
        f"Стратегия: {manifest['strategy']}; {composition}\n"
        f"Прогонов: {len(runs)}/{expected_runs}; исключено: {invalid}; медиана и мин–макс повторов"
    )

    def draw(ax, priority, metric, percent=False):
        maxima = []
        for mode, color in (("off", "#d04b41"), ("on", "#167b9d")):
            selected = [
                r for r in valid_rows if r["mode"] == mode and r["priority"] == priority
            ]
            points = []
            for load in sorted({r["offered_rps"] for r in selected}):
                values = [
                    r[metric]
                    for r in selected
                    if r["offered_rps"] == load and math.isfinite(r[metric])
                ]
                if values:
                    points.append(
                        (load, statistics.median(values), min(values), max(values))
                    )
                    maxima.append(max(values))
            ax.errorbar(
                [p[0] for p in points],
                [p[1] for p in points],
                yerr=[[p[1] - p[2] for p in points], [p[3] - p[1] for p in points]],
                color=color,
                marker="o",
                capsize=4,
                label=MODE_LABELS[mode],
            )
        ax.set_xlabel("Общая входящая нагрузка, запросов/с")
        reference = (
            (manifest["highSloMs"] if priority == "high" else manifest["sloMs"])
            if metric == "p95_ms"
            else manifest["ioSlots"]
            if metric == "io_active_mean"
            else 0
        )
        ax.set_ylim(0, max(maxima + [reference, 1]) * 1.08)
        ax.set_xticks(sorted({r["offered_rps"] for r in valid_rows}))
        if percent:
            ax.set_ylim(-3, 105)
        ax.grid(alpha=0.25)
        ax.legend(fontsize=8)

    fig, ax = plt.subplots(figsize=(10, 5.5), layout="constrained")
    draw(ax, "critical", "p95_ms")
    ax.axhline(
        manifest["sloMs"],
        color="#555555",
        linestyle="--",
        label=f"SLO: {manifest['sloMs']} мс",
    )
    ax.set_ylabel("Critical p95 успешных ответов, мс")
    ax.set_title(caption, fontsize=10)
    ax.legend()
    for extension in ("png", "svg"):
        fig.savefig(directory / f"critical-p95.{extension}", dpi=180)
    plt.close(fig)

    comparison_name = (
        "success-disaster-comparison"
        if manifest["scenario"] == "SuccessDisaster"
        else "low-flood-comparison"
    )
    panels = (
        [
            ("critical", "p95_ms", "Critical p95 успешных ответов, мс", False),
            ("high", "p95_ms", "High p95 успешных ответов, мс", False),
            (
                "critical",
                "slo_pct",
                "Critical: успешные ответы в пределах SLO, %",
                True,
            ),
            ("high", "slo_pct", "High: успешные ответы в пределах SLO, %", True),
            ("normal", "shed_pct", "Normal: отказы 503, %", True),
            ("low", "shed_pct", "Low: отказы 503, %", True),
        ]
        if manifest["scenario"] == "SuccessDisaster"
        else [
            ("critical", "p95_ms", "Critical p95 успешных ответов, мс", False),
            (
                "critical",
                "slo_pct",
                "Critical: успешные ответы в пределах SLO, %",
                True,
            ),
            ("critical", "success_pct", "Critical: успешные ответы, %", True),
            ("low", "shed_pct", "Low: отказы 503, %", True),
        ]
    )
    fig, axes = plt.subplots(
        len(panels) // 2,
        2,
        figsize=(12, 11 if len(panels) == 6 else 8),
        layout="constrained",
    )
    for ax, (priority, metric, title, percent) in zip(axes.flat, panels):
        draw(ax, priority, metric, percent)
        ax.set_title(title, fontsize=10)
        if metric == "p95_ms":
            ax.axhline(
                manifest["highSloMs"] if priority == "high" else manifest["sloMs"],
                color="#555555",
                linestyle="--",
            )
    fig.suptitle(caption, fontsize=11)
    for extension in ("png", "svg"):
        fig.savefig(directory / f"{comparison_name}.{extension}", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(11, 4), layout="constrained")
    draw(axes[0], "critical", "cpu_pct", True)
    draw(axes[1], "critical", "generator_cpu_pct", True)
    axes[0].set_title("CPU сервера, % одного логического процессора")
    axes[1].set_title("CPU генератора, % одного логического процессора")
    fig.savefig(directory / "cpu-utilization.png", dpi=180)
    plt.close(fig)
    if stream_rows:
        for filename, russian in (
            ("workload-summary.csv", False),
            ("workload-summary-ru.csv", True),
        ):
            with (directory / filename).open(
                "w", encoding="utf-8-sig", newline=""
            ) as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    [labels[key] if russian else key for key in stream_rows[0]]
                )
                for row in stream_rows:
                    writer.writerow(
                        [
                            MODE_LABELS[v]
                            if russian and k == "mode"
                            else ("да" if v else "нет")
                            if russian and k == "valid_arrival_rate"
                            else v
                            for k, v in row.items()
                        ]
                    )
        combined_rows = valid_rows
        valid_rows = [r for r in stream_rows if r["valid_arrival_rate"]]
        kinds = sorted({r["workload"] for r in stream_rows})
        fig, axes = plt.subplots(
            1, len(kinds), figsize=(12, 4.5), layout="constrained", squeeze=False
        )
        all_streams = valid_rows
        for ax, kind in zip(axes.flat, kinds):
            valid_rows = [r for r in all_streams if r["workload"] == kind]
            draw(ax, "critical", "p95_ms")
            ax.axhline(manifest["sloMs"], color="#555555", linestyle="--")
            ax.set_title(f"Critical {kind.upper()}: p95 успешных ответов, мс")
        fig.suptitle(caption, fontsize=9)
        fig.savefig(directory / "critical-by-workload.png", dpi=180)
        plt.close(fig)
        valid_rows = combined_rows
    if workload in ("io", "mixed"):
        fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
        for ax, key, title in zip(
            axes.flat,
            ("io_active_mean", "io_waiting_mean", "io_wait_p95_ms", "io_completed"),
            (
                "Среднее число занятых мест I/O",
                "Среднее число ожидающих запросов I/O",
                "I/O: p95 ожидания места успешными запросами, мс",
                "Завершённые обслуживания I/O на сервере",
            ),
        ):
            draw(ax, "critical", key)
            ax.set_title(title, fontsize=10)
        axes[0, 0].axhline(manifest["ioSlots"], linestyle="--", color="#555555")
        fig.suptitle(caption, fontsize=9)
        fig.savefig(directory / "io-resource.png", dpi=180)
        plt.close(fig)
    if manifest["strategy"] == "Cinnamon":
        plot_cinnamon_timeseries(directory, {r["run"] for r in valid_rows if r["mode"] == "on"})
    print(
        f"{manifest['scenario']} / {workload}: {len(runs)}/{expected_runs} прогонов; исключено: {invalid}"
    )
    print(
        "Нагрузка | режим | Critical p95, мс | High p95, мс | Critical в SLO, % | CPU, %"
    )
    for load in sorted({r["offered_rps"] for r in valid_rows}):
        for mode in ("off", "on"):
            print(
                f"{load:5} | {MODE_LABELS[mode]:16} | {median(valid_rows, load, mode, 'critical', 'p95_ms'):8.1f} | "
                f"{median(valid_rows, load, mode, 'high', 'p95_ms'):8.1f} | {median(valid_rows, load, mode, 'critical', 'slo_pct'):6.1f} | {median(valid_rows, load, mode, 'critical', 'cpu_pct'):6.1f}"
            )


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="Построение таблиц и графиков нагрузочного эксперимента."
    )
    parser.add_argument("directory", type=Path, help="Папка с результатами прогонов k6")
    analyze(parser.parse_args().directory.resolve())
