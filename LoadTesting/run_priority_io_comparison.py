"""Compare Adaptive/Cinnamon on a shared I/O pool with four priority classes."""
import argparse
import csv
import json
import math
import shutil
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import run_experiment as runner
from runner_common import DLL, LOAD_TESTING, build, sha256, stamp, utc, write_json

sys.path.insert(0, str(LOAD_TESTING / 'plotting'))
from analyze_profiles import analyze, outcome, validate_run


def summarize(root, rows, protocol, complete=False):
    with (root / 'runs-by-priority.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        if rows:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    groups = defaultdict(list)
    for row in rows:
        groups[row['rate'], row['strategy'], row['priority']].append(row)
    summary = []
    order = dict(critical=0, high=1, normal=2, low=3)
    for (rate, strategy, priority), group in sorted(groups.items(), key=lambda item: (item[0][0], item[0][1], order[item[0][2]])):
        record = dict(rate=rate, strategy=strategy, priority=priority, repetitions=len(group))
        for key in ('success_pct', 'slo_pct', 'shed_pct', 'timeout_pct', 'p90_ms', 'goodput_rps'):
            values = [r[key] for r in group if math.isfinite(r[key])]
            record.update({key + '_median': statistics.median(values) if values else None,
                           key + '_min': min(values) if values else None, key + '_max': max(values) if values else None,
                           key + '_observed_runs': len(values)})
        summary.append(record)
    with (root / 'comparison.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        if summary:
            writer = csv.DictWriter(stream, fieldnames=list(summary[0]))
            writer.writeheader(); writer.writerows(summary)
    write_json(root / 'comparison-summary.json', dict(complete=complete, protocol=protocol, rows=summary,
        note='Medians/min/max of individual runs, including run-level p90. Percentiles are not pooled.'))
    lines = ['I/O: Adaptive с очередью и Cinnamon, смесь Critical/High/Normal/Low = 25/30/25/20%.',
             f"Статус: {'завершено' if complete else 'промежуточные результаты'}.",
             'Общий I/O-пул: 4 слота, обслуживание 150±50 мс. Обе стратегии: InitialLimit=4, MinLimit=1, MaxLimit=4, QueueCapacity=4, QueueTimeoutMs=250.',
             f"На уровень: {protocol['repetitions']} повтора по {protocol['durationSeconds']} с; новый сервер и 20 последовательных запросов Critical для прогрева перед каждым прогоном.",
             'Сравниваются ON-прогоны; порядок стратегий чередуется по нагрузке и повтору. Сервер CPU 0, k6 CPU 2; фоновые процессы не изолированы.',
             'Бюджет своевременности: <1000 мс. p90 — только успешные ответы. В таблице медиана [min..max] отдельных прогонов.',
             'RPS | Стратегия | Приоритет | HTTP 200 % | p90 успешных мс | HTTP 200 <1с % | Сброс %']
    for r in summary:
        def cell(key):
            if r[key+'_median'] is None: return '— (нет успешных ответов)'
            return f"{r[key+'_median']:.2f} [{r[key+'_min']:.2f}..{r[key+'_max']:.2f}]"
        lines.append(f"{r['rate']} | {r['strategy']} | {r['priority']} | " + ' | '.join(cell(k) for k in ('success_pct', 'p90_ms', 'slo_pct', 'shed_pct')))
    lines += ['', 'Проверки: отсутствие пропусков генератора; заданная интенсивность каждого класса; атрибуция всех запросов фазам и временным окнам. Нарушение SLO не означает невалидность нагрузки.',
              'Ограничения: два коротких повтора, фон не изолирован; данные не доказывают устойчивое ранжирование. Пробы Adaptive, приоритетные резервы и PI/Auto-tuner Cinnamon сохранены. OFF в этой серии не выполнялся.']
    (root / 'report.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', type=Path)
    parser.add_argument('--rates', type=int, nargs='+', default=[15, 25, 40, 60])
    parser.add_argument('--repetitions', type=int, default=2)
    parser.add_argument('--duration-seconds', type=int, default=60)
    a = parser.parse_args()
    if min(*a.rates, a.repetitions, a.duration_seconds) <= 0 or len(set(a.rates)) != len(a.rates):
        parser.error('Positive rates/durations/repetitions and unique rates are required.')
    root = (a.output_directory or LOAD_TESTING / 'results' / ('priority-io-comparison-' + stamp())).resolve()
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / 'status.json', dict(state='preparing'))
    build(no_restore=True)
    protocol = dict(createdUtc=utc(), rates=a.rates, repetitions=a.repetitions, durationSeconds=a.duration_seconds,
        priorityShares=dict(critical=25, high=30, normal=25, low=20), queueCapacity=4, queueTimeoutMs=250,
        minLimit=1, initialLimit=4, maxLimit=4, ioSlots=4, ioServiceMs=150, ioServiceJitterMs=50,
        warmupRequests=20, reportingBudgetMs=1000, modes=['on'],
        order='Paired by rate, reversed by rate and repetition', binarySha256=sha256(DLL))
    write_json(root / 'plan.json', protocol)
    shutil.copy2(__file__, root / Path(__file__).name)
    shutil.copytree(DLL.parent, root / 'binary')
    write_json(root / 'binary-hashes.json', {str(p.relative_to(root/'binary')): sha256(p) for p in (root/'binary').rglob('*') if p.is_file()})
    configurations = {}
    rows, completed = [], []
    current = 'preparing'
    try:
        for strategy in ('Adaptive', 'Cinnamon'):
            args = runner.validate(runner.parser().parse_args([
                '--scenario', 'SuccessDisaster', '--profile', 'Plateau', '--workload', 'Io',
                '--strategy', strategy, '--rates', ','.join(map(str, a.rates)), '--modes', 'on',
                '--duration-seconds', str(a.duration_seconds), '--repetitions', str(a.repetitions),
                '--adaptive-initial-limit', '4', '--adaptive-min-limit', '1', '--adaptive-max-limit', '4',
                '--adaptive-queue-capacity', '4', '--adaptive-queue-timeout-ms', '250',
                '--cinnamon-initial-limit', '4', '--cinnamon-min-limit', '1', '--cinnamon-max-limit', '4',
                '--cinnamon-queue-capacity', '4', '--cinnamon-queue-timeout-ms', '250',
                '--skip-build', '--output-directory', str(root / strategy)]))
            args.output_directory.mkdir()
            settings = runner.settings(args)
            runner.snapshot(args.output_directory, settings, False)
            runner.calibrate(args)
            configurations[strategy] = args, settings
        for repetition in range(1, a.repetitions + 1):
            for index, rate in enumerate(a.rates):
                order = ('Adaptive', 'Cinnamon') if (index + repetition) % 2 else ('Cinnamon', 'Adaptive')
                for strategy in order:
                    name = f'r{repetition}-rate{rate}-on'
                    current = strategy + '/' + name
                    write_json(root / 'status.json', dict(state='running', current=current, completed=completed))
                    print('START ' + current, flush=True)
                    args, settings = configurations[strategy]
                    runner.run_one(args, settings, repetition, rate, 'on', name, '')
                    data = json.loads((args.output_directory / (name + '.json')).read_text(encoding='utf-8-sig'))
                    reasons = validate_run(data['experiment'], data['metrics'])
                    if reasons:
                        raise ValueError(current + ': ' + '; '.join(reasons))
                    for priority in ('critical', 'high', 'normal', 'low'):
                        row = dict(rate=rate, strategy=strategy, repetition=repetition, priority=priority,
                                   **outcome(data['metrics'], 'phase_0_' + priority, a.duration_seconds))
                        rows.append(row)
                        print(f"  {priority}: HTTP200={row['success_pct']:.2f}% p90={row['p90_ms']:.1f}ms shed={row['shed_pct']:.2f}%", flush=True)
                    completed.append(current)
                    summarize(root, rows, protocol)
        for strategy, (args, _) in configurations.items():
            if analyze(args.output_directory):
                raise ValueError('Final analysis invalid: ' + strategy)
        summarize(root, rows, protocol, complete=True)
        write_json(root / 'status.json', dict(state='complete', runs=len(completed), completed=completed))
        print('COMPLETE ' + str(root), flush=True)
    except BaseException as error:
        write_json(root / 'status.json', dict(state='failed', current=current, completed=completed, error=str(error)))
        raise


if __name__ == '__main__':
    main()
