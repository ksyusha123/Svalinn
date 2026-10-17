"""Сводка подтверждающей матрицы без объединения процентилей разных прогонов."""

import argparse
import csv
import json
import math
import statistics
from pathlib import Path


def median(rows, key):
    values = [float(r[key]) for r in rows if math.isfinite(float(r[key]))]
    return statistics.median(values) if values else math.nan


def passes(row):
    availability = 99.9 if row["priority"] == "critical" else 99.0
    return (
        row["valid_arrival_rate"] == "True"
        and float(row["p95_ms"]) < float(row["slo_ms"])
        and float(row["success_pct"]) >= availability
        and float(row["slo_pct"]) >= 95
    )


def summarize(directory):
    matrix = json.loads((directory / "matrix.json").read_text(encoding="utf-8-sig"))
    repeat = matrix["repetitions"]
    lines = [
        f"ПОДТВЕРЖДАЮЩИЕ ИСПЫТАНИЯ SVALINN: {matrix['strategy']}",
        "",
        f"Сценарий: {matrix['scenario']}; {repeat} повторов по {matrix['durationSeconds']} секунд.",
        "p95, доступность и сброс ниже — медианы показателей отдельных повторов.",
        "Прошёл = достигнута нагрузка, p95 < SLO, >=95% всех запросов успешны в пределах SLO,",
        "успешность Critical >=99,9% (High >=99%). Это эмпирическая проверка, не гарантия доступности.",
        "",
    ]
    complete = True
    for series in matrix["series"]:
        folder = directory / series["directory"]
        with (folder / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        expected = {
            (rate, mode, repetition, priority)
            for rate in series["rates"]
            for mode in ("off", "on")
            for repetition in range(1, repeat + 1)
            for priority in ("critical", "high", "normal", "low")
        }
        keys = [
            (float(r["offered_rps"]), r["mode"], int(r["repetition"]), r["priority"])
            for r in rows
        ]
        valid = (
            set(keys) == expected
            and len(keys) == len(expected)
            and all(
                r["valid_arrival_rate"] == "True"
                and float(r["duration_seconds"]) == matrix["durationSeconds"]
                for r in rows
            )
        )
        complete &= valid
        lines += [
            f"{series['workload']}: {'полная валидная серия' if valid else 'НЕПОЛНАЯ ИЛИ НЕВАЛИДНАЯ СЕРИЯ'}",
            "RPS | режим | Critical p95 [мин; макс], мс | успешность, % | в SLO, % | прошёл Critical/High | сброс Normal/Low, %",
        ]
        stable = []
        for rate in series["rates"]:
            for mode in ("off", "on"):
                selected = [
                    r
                    for r in rows
                    if float(r["offered_rps"]) == rate and r["mode"] == mode
                ]
                critical = [r for r in selected if r["priority"] == "critical"]
                high = [r for r in selected if r["priority"] == "high"]
                count = sum(passes(r) for r in critical)
                high_count = sum(passes(r) for r in high)
                values = [
                    float(r["p95_ms"])
                    for r in critical
                    if math.isfinite(float(r["p95_ms"]))
                ]
                span = (
                    f"{min(values):.0f}; {max(values):.0f}" if values else "нет данных"
                )
                shed = [
                    median([r for r in selected if r["priority"] == p], "shed_pct")
                    for p in ("normal", "low")
                ]
                lines.append(
                    f"{rate:3} | {mode:3} | {median(critical, 'p95_ms'):7.1f} [{span}] | "
                    f"{median(critical, 'success_pct'):6.2f} | {median(critical, 'slo_pct'):6.2f} | "
                    f"{count}/{repeat}, {high_count}/{repeat} | {shed[0]:.1f}/{shed[1]:.1f}"
                )
                if valid and mode == "on" and count == repeat and high_count == repeat:
                    stable.append(rate)
        lines += [
            f"Уровни ON, где все повторы прошли критерии Critical и High: {stable or 'нет'}.",
            "Это только проверенные уровни, а не точная или универсальная граница.",
            "",
        ]
        if series["workload"].lower() == "mixed":
            with (folder / "workload-summary.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                streams = list(csv.DictReader(handle))
            lines += [
                "Mixed: Critical отдельно по типу работы",
                "RPS | режим | тип | p95, мс | успешность, % | в SLO, %",
            ]
            for rate in series["rates"]:
                for mode in ("off", "on"):
                    for kind in ("cpu", "io"):
                        selected = [
                            r
                            for r in streams
                            if float(r["offered_rps"]) == rate
                            and r["mode"] == mode
                            and r["priority"] == "critical"
                            and r["workload"] == kind
                        ]
                        lines.append(
                            f"{rate:3} | {mode:3} | {kind:3} | {median(selected, 'p95_ms'):7.1f} | "
                            f"{median(selected, 'success_pct'):6.2f} | {median(selected, 'slo_pct'):6.2f}"
                        )
            lines.append("")
    lines += [
        "Ограничения: локальный генератор на другом логическом CPU; синтетическая I/O-модель;",
        "последовательный порядок типов нагрузки; фоновые процессы и частоты CPU полностью не контролируются.",
        "Меньший p95 успешных ответов при росте тайм-аутов не означает улучшения.",
        "Low/Normal могут отбрасываться уже ниже мощности ресурса; доля сброса показывает цену защиты,",
        "но сама по себе не доказывает, что каждый отказ был необходим или избыточен.",
        "",
        "СТАТУС: "
        + (
            "матрица завершена и прошла проверку полноты."
            if complete
            else "нужна проверка неполных/невалидных прогонов."
        ),
    ]
    (directory / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return complete


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if summarize(args.directory) else 1)
