"""Run paired Cinnamon/Adaptive I/O trials, optionally alongside a saved OFF series."""
import argparse
import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

from runner_common import LOAD_TESTING, build, read_json, stamp, write_json


def baseline_rows(directory):
    rows = list(csv.DictReader((directory / 'summary.csv').open(encoding='utf-8-sig')))
    for row in rows:
        rate = row['target_rps']
        timely = successes = 0
        with (directory / f'rate{rate}.raw.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                point = json.loads(line)
                if point.get('type') == 'Point' and point['metric'] == 'measured_success_latency_ms':
                    successes += 1
                    timely += point['data']['value'] < 500
        row.update(strategy='Off', shed_pct=0,
                   timely_success_pct=100 * timely / int(row['sent']),
                   successful_count=successes)
    return rows


def save_comparison(output, rows):
    columns = ['repetition', 'strategy', 'target_rps', 'sent', 'actual_rps', 'successful_count',
               'success_pct', 'shed_pct', 'timeout_pct', 'other_error_pct',
               'timely_success_pct', 'successful_p50_ms', 'successful_p90_ms',
               'successful_p95_ms', 'successful_p99_ms', 'all_p50_ms', 'all_p90_ms',
               'all_p95_ms', 'all_p99_ms', 'dropped_measurement', 'dropped_total']
    with (output / 'comparison.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in columns} for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', type=Path)
    parser.add_argument('--baseline-directory', type=Path,
                        default=LOAD_TESTING / 'results/io-baseline-20261104-15-to-40')
    parser.add_argument('--rates', type=int, nargs='+', default=[15, 20, 25, 30, 35, 40])
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--warmup-seconds', type=int, default=30)
    parser.add_argument('--measure-seconds', type=int, default=180)
    parser.add_argument('--no-baseline', action='store_true', help='Compare fresh protected runs only.')
    a = parser.parse_args()
    if min(a.repetitions, a.warmup_seconds, a.measure_seconds, *a.rates) <= 0:
        parser.error('Rates, repetitions and durations must be positive.')
    if len(set(a.rates)) != len(a.rates):
        parser.error('Rates must be unique.')
    output = (a.output_directory or LOAD_TESTING / 'results' / ('io-comparison-' + stamp())).resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    if not a.no_baseline:
        baseline = a.baseline_directory.resolve()
        manifest = read_json(baseline / 'manifest.json')
        for key, expected in dict(ioSlots=4, ioServiceMs=150, ioServiceJitterMs=50,
                                  warmupSeconds=a.warmup_seconds, measureSeconds=a.measure_seconds, timeoutSeconds=5).items():
            if manifest[key] != expected:
                raise ValueError(f'Baseline mismatch: {key}')
        rows = [row for row in baseline_rows(baseline) if int(row['target_rps']) in a.rates]
    build(no_restore=True)
    save_comparison(output, rows)
    write_json(output / 'manifest.json', dict(baselineDirectory=None if a.no_baseline else str(baseline),
        rates=a.rates, warmupSeconds=a.warmup_seconds, measureSeconds=a.measure_seconds,
        order='Paired by rate; Cinnamon/Adaptive order alternates by rate and repetition',
        sloMs=500, endpoint='/benchmark/io/normal', repetitions=a.repetitions,
        note='Both strategies use appsettings.json, captured in each run. Historical OFF is optional.'))
    shutil.copy2(__file__, output / Path(__file__).name)
    for directory in ['Svalinn', 'TestApp']:
        for source in (LOAD_TESTING.parent / directory).rglob('*'):
            if source.is_file() and source.suffix in {'.cs', '.json', '.csproj'} and not {'bin', 'obj'}.intersection(source.parts):
                destination = output / 'sources' / source.relative_to(LOAD_TESTING.parent)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    current = None
    try:
        for repetition in range(1, a.repetitions + 1):
            for index, rate in enumerate(a.rates):
                order = ['Cinnamon', 'Adaptive'] if (index + repetition - 1) % 2 == 0 else ['Adaptive', 'Cinnamon']
                for strategy in order:
                    current = f'r{repetition}-{strategy}-rate{rate}'
                    folder = output / current
                    write_json(output / 'status.json', dict(state='running', current=current,
                               completed=[f"r{r.get('repetition', 0)}-{r['strategy']}-rate{r['target_rps']}" for r in rows if r['strategy'] != 'Off']))
                    print(f'START {current}', flush=True)
                    subprocess.run([sys.executable, str(LOAD_TESTING / 'run_io_baseline.py'),
                                    '--strategy', strategy, '--rates', str(rate), '--skip-build',
                                    '--warmup-seconds', str(a.warmup_seconds),
                                    '--measure-seconds', str(a.measure_seconds),
                                    '--output-directory', str(folder)], check=True)
                    row = next(csv.DictReader((folder / 'summary.csv').open(encoding='utf-8-sig')))
                    summary = read_json(folder / f'rate{rate}.json')['metrics']
                    row.update(strategy=strategy, repetition=repetition,
                               successful_count=summary.get('measured_successes', {}).get('values', {}).get('count', 0))
                    rows.append(row)
                    save_comparison(output, rows)
        write_json(output / 'status.json', dict(state='complete', runs=2 * len(a.rates) * a.repetitions))
        print(f'COMPLETE {output}', flush=True)
    except BaseException as error:
        write_json(output / 'status.json', dict(state='failed', current=current, error=str(error)))
        raise


if __name__ == '__main__':
    main()
