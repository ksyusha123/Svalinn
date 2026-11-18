"""Measure one I/O endpoint with optional bounded protection; no capacity verdict."""
import argparse
import csv
import os
import time
from pathlib import Path

from runner_common import (
    ROOT, LOAD_TESTING, DLL, Child, build, check_masks, ensure_port_free,
    executable, get_json, request, read_json, sha256, stamp, utc, wait_ready, write_json,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-directory', type=Path)
    parser.add_argument('--warmup-seconds', type=int, default=30)
    parser.add_argument('--measure-seconds', type=int, default=180)
    parser.add_argument('--rates', type=int, nargs='+', default=[15, 20, 25, 30, 35, 40])
    parser.add_argument('--skip-build', action='store_true')
    parser.add_argument('--strategy', choices=['Off', 'Cinnamon', 'Adaptive'], default='Off')
    parser.add_argument('--profile', choices=['Plateau', 'Spike'], default='Plateau')
    a = parser.parse_args()
    if a.profile == 'Spike':
        if len(a.rates) != 1:
            parser.error('Spike requires one peak rate, e.g. --rates 40.')
        a.measure_seconds = 135
    if min(a.warmup_seconds, a.measure_seconds, *a.rates) <= 0:
        parser.error('Durations and rates must be positive.')
    check_masks(1, 4)
    output = (a.output_directory or LOAD_TESTING / 'results' / ('io-baseline-' + stamp())).resolve()
    output.mkdir(parents=True, exist_ok=False)
    if not a.skip_build:
        build(no_restore=True)
    write_json(output / 'manifest.json', dict(
        createdUtc=utc(), endpoint='/benchmark/io/normal', sheddingEnabled=a.strategy != 'Off', strategy=a.strategy,
        ioSlots=4, ioServiceMs=150, ioServiceJitterMs=50,
        rates=a.rates, warmupSeconds=a.warmup_seconds, measureSeconds=a.measure_seconds, profile=a.profile,
        phasePlan=([dict(name='baseline', seconds=45, rate=15), dict(name='spike', seconds=30, rate=a.rates[0]),
                    dict(name='recovery', seconds=60, rate=15)] if a.profile == 'Spike' else None),
        timeoutSeconds=5, serverAffinityMask=1, generatorAffinityMask=4,
        binarySha256=sha256(DLL), latency='Client wall time; successful responses and all outcomes separately',
    ))
    strategy_settings = read_json(ROOT / 'TestApp/appsettings.json')['Svalinn'].get(a.strategy, {})
    config = {'Svalinn:Enabled': a.strategy != 'Off'}
    if a.strategy != 'Off':
        config['Svalinn:Strategy'] = a.strategy
        config.update({'Svalinn:' + a.strategy + ':' + k: v for k, v in strategy_settings.items()})
    write_json(output / 'strategy.json', dict(strategy=a.strategy, settings=strategy_settings))
    import shutil
    for source in [Path(__file__), LOAD_TESTING / 'k6/io-baseline.js', ROOT / 'TestApp/appsettings.json', ROOT / 'TestApp/IoBenchmark.cs', ROOT / 'Svalinn/CinnamonOptions.cs', ROOT / 'Svalinn/AdaptiveLoadOptions.cs', ROOT / 'Svalinn/Strategy/AdaptiveLoadSheddingStrategy.cs', ROOT / 'Svalinn/Strategy/CinnamonLoadSheddingStrategy.cs']:
        shutil.copy2(source, output / source.name)
    rows = []
    base = 'http://127.0.0.1:5093'
    for rate in a.rates:
        name = f'rate{rate}'
        write_json(output / 'status.json', dict(state='running', rate=rate, completed=rows))
        ensure_port_free(5093)
        with Child([
            executable('dotnet'), DLL, '--urls', base,
            '--CpuBenchmark:Enabled=true', '--CpuBenchmark:Iterations=1000',
            '--CpuBenchmark:AffinityMask=1',
            '--IoBenchmark:Slots=4', '--IoBenchmark:ServiceMs=150',
            '--IoBenchmark:ServiceJitterMs=50', '--Logging:LogLevel:Default=Error',
        ] + [f'--{k}={str(v).lower() if isinstance(v, bool) else v}' for k, v in config.items()], output, name + '.server', ROOT / 'TestApp') as server:
            wait_ready(server, lambda: get_json(base + '/benchmark/cpu/info'))
            assert get_json(base + '/')['svalinnEnabled'] == (a.strategy != 'Off')
            env = os.environ.copy()
            env.update(BASE_URL=base, RATE=str(rate), WARMUP_SECONDS=str(a.warmup_seconds),
                       PROFILE=a.profile,
                       MEASURE_SECONDS=str(a.measure_seconds), SUMMARY_PATH=str(output / (name + '.json')),
                       NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
            samples = []
            with Child([
                executable('k6'), 'run', '--quiet', '--address', '127.0.0.1:0',
                '--out', 'json=' + str(output / (name + '.raw.jsonl')),
                LOAD_TESTING / 'k6/io-baseline.js',
            ], output, name + '.k6', affinity=4, env=env) as generator:
                started = time.monotonic()
                while generator.process.poll() is None:
                    if server.process.poll() is not None:
                        raise RuntimeError('Server exited during measurement.')
                    if time.monotonic() - started > a.warmup_seconds + a.measure_seconds + 60:
                        raise RuntimeError('Generator exceeded allowed duration.')
                    sample = dict(elapsedSeconds=time.monotonic() - started, sampleEpochMs=time.time() * 1000,
                                  **get_json(base + '/benchmark/telemetry'))
                    if a.strategy != 'Off':
                        status, _, text = request(base + '/metrics')
                        if status != 200:
                            raise RuntimeError('Controller telemetry unavailable.')
                        (output / (name + '.controller.prom')).write_text(text, encoding='utf-8')
                        prefix = 'svalinn_' + a.strategy.lower() + '_'
                        for line in text.splitlines():
                            if line.startswith(prefix):
                                key, value = line.rsplit(' ', 1)
                                sample[key] = float(value)
                    sample['snapshotEpochMs'] = time.time() * 1000
                    samples.append(sample)
                    time.sleep(.5)
                if generator.process.returncode:
                    raise RuntimeError(f'k6 failed: see {name}.k6.err')
            with (output / (name + '.telemetry.csv')).open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(samples[0]))
                writer.writeheader()
                writer.writerows(samples)
        metrics = read_json(output / (name + '.json'))['metrics']
        def values(key):
            return metrics.get(key, {}).get('values', {})
        count = values('measured_requests').get('count', 0)
        if not count:
            raise RuntimeError('No measured requests.')
        row = dict(target_rps=rate, sent=count, actual_rps=count / a.measure_seconds,
                   success_pct=100 * values('measured_successes').get('count', 0) / count,
                   timeout_pct=100 * values('measured_timeouts').get('count', 0) / count,
                   shed_pct=100 * values('measured_shed').get('count', 0) / count,
                   timely_success_pct=100 * values('measured_timely_successes').get('count', 0) / count,
                   other_error_pct=100 * values('measured_other_errors').get('count', 0) / count,
                   dropped_measurement=values('dropped_iterations{scenario:measure}').get('count', 0),
                   dropped_total=values('dropped_iterations').get('count', 0))
        for label, metric in [('successful', 'measured_success_latency_ms'), ('all', 'measured_latency_ms')]:
            for column, key in [('p50', 'med'), ('p90', 'p(90)'), ('p95', 'p(95)'), ('p99', 'p(99)')]:
                row[f'{label}_{column}_ms'] = values(metric).get(key)
        rows.append(row)
        with (output / 'summary.csv').open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(row))
            writer.writeheader()
            writer.writerows(rows)
        print(row, flush=True)
    write_json(output / 'status.json', dict(state='complete', completed=rows))
    print(f'Results: {output}', flush=True)


if __name__ == '__main__':
    main()
