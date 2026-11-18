"""Run Cinnamon and Adaptive with limit 4 against the saved unprotected I/O series."""
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
    columns = ['strategy', 'target_rps', 'sent', 'actual_rps', 'successful_count',
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
    a = parser.parse_args()
    output = (a.output_directory or LOAD_TESTING / 'results' / ('io-comparison-' + stamp())).resolve()
    output.mkdir(parents=True, exist_ok=False)
    baseline = a.baseline_directory.resolve()
    manifest = read_json(baseline / 'manifest.json')
    for key, expected in dict(ioSlots=4, ioServiceMs=150, ioServiceJitterMs=50,
                              warmupSeconds=30, measureSeconds=180, timeoutSeconds=5).items():
        if manifest[key] != expected:
            raise ValueError(f'Baseline mismatch: {key}')
    build(no_restore=True)
    rows = baseline_rows(baseline)
    save_comparison(output, rows)
    write_json(output / 'manifest.json', dict(baselineDirectory=str(baseline),
        rates=[15, 20, 25, 30, 35, 40], warmupSeconds=30, measureSeconds=180,
        order='Paired by rate; Cinnamon/Adaptive order alternates',
        sloMs=500, endpoint='/benchmark/io/normal', repetitions=1,
        note='Adaptive uses appsettings.json; NormalPriorityFraction=0.75, with optional idle-quota borrowing.'))
    shutil.copy2(__file__, output / Path(__file__).name)
    current = None
    try:
        for index, rate in enumerate([15, 20, 25, 30, 35, 40]):
            order = ['Cinnamon', 'Adaptive'] if index % 2 == 0 else ['Adaptive', 'Cinnamon']
            for strategy in order:
                current = f'{strategy}-rate{rate}'
                folder = output / current
                write_json(output / 'status.json', dict(state='running', current=current,
                           completed=[f"{r['strategy']}-rate{r['target_rps']}" for r in rows if r['strategy'] != 'Off']))
                print(f'START {current}', flush=True)
                subprocess.run([sys.executable, str(LOAD_TESTING / 'run_io_baseline.py'),
                                '--strategy', strategy, '--rates', str(rate), '--skip-build',
                                '--output-directory', str(folder)], check=True)
                row = next(csv.DictReader((folder / 'summary.csv').open(encoding='utf-8-sig')))
                summary = read_json(folder / f'rate{rate}.json')['metrics']
                row.update(strategy=strategy,
                           successful_count=summary.get('measured_successes', {}).get('values', {}).get('count', 0))
                rows.append(row)
                save_comparison(output, rows)
        write_json(output / 'status.json', dict(state='complete', runs=12))
        print(f'COMPLETE {output}', flush=True)
    except BaseException as error:
        write_json(output / 'status.json', dict(state='failed', current=current, error=str(error)))
        raise


if __name__ == '__main__':
    main()
