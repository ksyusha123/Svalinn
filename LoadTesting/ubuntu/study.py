"""Sequential, resumable Ubuntu study for a 2-vCPU / 2-GiB machine."""
import argparse
import csv
import math
import platform
import random
import signal
import statistics
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
import run_experiment as runner
from runner_common import ROOT, DLL, LOAD_TESTING, directory_lock, read_json, sha256, utc, write_json
sys.path.insert(0, str(LOAD_TESTING / 'plotting'))
from analyze_profiles import outcome, validate_run

VARIANTS = ('Off', 'Inflight', 'Cpu', 'AdaptiveNoQueue', 'AdaptiveQueue', 'Cinnamon')
MIB = 1024 * 1024


def variants(name):
    return ('Adaptive' if name.startswith('Adaptive') else 'Inflight' if name == 'Off' else name,
            'off' if name == 'Off' else 'on', 0 if name == 'AdaptiveNoQueue' else 4)


def source_hashes():
    paths = runner.source_files() + [str(p.relative_to(ROOT).as_posix())
                                   for p in (LOAD_TESTING / 'ubuntu').glob('*') if p.is_file()]
    return {p: sha256(ROOT / p) for p in sorted(set(paths))}


def host_identifier():
    return Path('/etc/machine-id').read_text().strip()


def environment():
    return dict(createdUtc=utc(), platform=platform.platform(), machine=platform.machine(),
                cpuAffinity=psutil.Process().cpu_affinity(), memoryBytes=psutil.virtual_memory().total,
                cpuInfo=Path('/proc/cpuinfo').read_text() if sys.platform.startswith('linux') else '',
                dotnet=runner.command(['dotnet', '--info'], True),
                k6=runner.command(['k6', 'version'], True), python=sys.version)


def protocol(a):
    return dict(iterations=a.iterations, timeoutSeconds=2, sloMs=1000,
                serverMask=a.server_mask, generatorMask=a.generator_mask,
                ioSlots=4, ioServiceMs=150, ioJitterMs=50, repetitions=a.repetitions,
                initialLimit=4, minLimit=1, maxLimit=32, inflightThreshold=4,
                queueCapacity=4, queueTimeoutMs=250, warmupRequests=20,
                successTargetForBaseline=99, minAvailableMiB=a.min_available_mib, seed=a.seed)


def options(a, case, folder):
    strategy, mode, queue = variants(case['variant'])
    p = [
        '--scenario', 'SuccessDisaster', '--workload', case['workload'],
        '--profile', case['profile'], '--strategy', strategy, '--modes', mode,
        '--rates', str(case['rate']), '--repetitions', '1', '--iterations', str(a.iterations),
        '--timeout-seconds', '2', '--slo-ms', '1000', '--high-slo-ms', '1000',
        '--server-affinity-mask', str(a.server_mask), '--generator-affinity-mask', str(a.generator_mask),
        '--adaptive-min-limit', '1', '--adaptive-initial-limit', '4', '--adaptive-max-limit', '32',
        '--adaptive-queue-capacity', str(queue), '--adaptive-queue-timeout-ms', '250',
        '--cinnamon-min-limit', '1', '--cinnamon-initial-limit', '4', '--cinnamon-max-limit', '32',
        '--cinnamon-queue-capacity', '4', '--cinnamon-queue-timeout-ms', '250',
        '--duration-seconds', str(case.get('duration', 180)),
        '--baseline-rate', str(case.get('baseline', 1)), '--baseline-seconds', '60',
        '--recovery-seconds', '120', '--spike-seconds', '30',
        '--skip-build', '--output-directory', str(folder),
    ]
    return p


def smoke_plan():
    return [dict(workload=w, profile='Plateau', rate=2, variant=v, repetition=1, duration=10)
            for w in ('Cpu', 'Io', 'Mixed') for v in VARIANTS]


def baseline_plan(a, estimate):
    grids = {
        'Cpu': a.cpu_rates or scaled(estimate, (.3, .6, .9, 1.2, 1.5, 2)),
        'Io': a.io_rates or [8, 15, 20, 25, 30, 40],
        'Mixed': a.mixed_rates or scaled(min(2 * estimate, 2 * 4000 / 150), (.3, .6, .9, 1.2, 1.5, 2)),
    }
    if any(not levels or min(levels) < 1 or max(levels) > 60 for levels in grids.values()):
        raise ValueError('Для этого стенда сетки baseline должны содержать только 1..60 RPS.')
    cases = [dict(workload=w, profile='Plateau', rate=rate, variant='Off', repetition=r)
             for w, levels in grids.items() for rate in levels for r in range(1, a.repetitions + 1)]
    return cases


def scaled(capacity, fractions):
    # Bound generator cost on 2 GiB; a missing degradation boundary is reported, never invented.
    return sorted({max(1, min(60, round(capacity * f))) for f in fractions})


def matrix_plan(a, capacities):
    cases = []
    for w in ('Cpu', 'Io', 'Mixed'):
        c = capacities[w]
        if c > 33:
            raise ValueError('Для 1.8× нагрузки требуется >60 RPS. Увеличьте стоимость CPU и повторите baseline в новом каталоге.')
        for r in range(1, a.repetitions + 1):
            for rate in sorted({max(1, round(c * f)) for f in (.5, .9, 1.2, 1.8)}):
                cases += [dict(workload=w, profile='Plateau', rate=rate, variant=v, repetition=r) for v in VARIANTS]
            for profile in ('Spike', 'OverloadRecovery'):
                cases += [dict(workload=w, profile=profile, rate=max(2, round(c * 1.8)),
                               baseline=max(1, round(c * .5)), variant=v, repetition=r) for v in VARIANTS]
    for profile, factor in (('DependencySlowdown', .9), ('CompositionShift', 1.2)):
        for r in range(1, a.repetitions + 1):
            cases += [dict(workload='Io', profile=profile, rate=max(1, round(capacities['Io'] * factor)),
                           variant=v, repetition=r) for v in VARIANTS]
    # Balance ordering within each workload/profile/rate/repetition block.
    groups = {}
    for case in cases:
        key = case['workload'], case['profile'], case['rate'], case['repetition']
        groups.setdefault(key, []).append(case)
    result = []
    rng = random.Random(a.seed)
    for group in groups.values():
        rng.shuffle(group)
        result.extend(group)
    return result


def run_checked(args, folder, min_available_mib):
    """Run one child at a time, stop before exhausting host memory, save host samples."""
    samples = []
    process = None
    initial_swap = psutil.swap_memory()
    try:
        with (folder / 'runner.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen([sys.executable, str(LOAD_TESTING / 'run_experiment.py'), *args],
                                       cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
            while process.poll() is None:
                mem, swap = psutil.virtual_memory(), psutil.swap_memory()
                times = psutil.cpu_times()
                samples.append(dict(epochSeconds=time.time(), availableBytes=mem.available,
                                    swapUsedBytes=swap.used, swapInBytes=swap.sin, swapOutBytes=swap.sout,
                                    cpuStealSeconds=getattr(times, 'steal', 0)))
                if mem.available < min_available_mib * MIB:
                    raise RuntimeError(f'Мало свободной памяти (<{min_available_mib} MiB). Серия остановлена; см. host.csv.')
                time.sleep(.5)
            if process.returncode:
                raise RuntimeError(f'Runner завершился с кодом {process.returncode}; см. {folder / "runner.log"}')
            final_swap = psutil.swap_memory()
            if final_swap.sin > initial_swap.sin or final_swap.sout > initial_swap.sout:
                raise RuntimeError('Во время прогона был swap I/O. Данные сохранены; результат не принят для сравнения.')
    finally:
        if process is not None and process.poll() is None:
            # The runner owns dotnet/k6, including on interruption and low memory.
            parent = psutil.Process(process.pid)
            children = parent.children(recursive=True)
            for p in reversed(children):
                try: p.terminate()
                except psutil.NoSuchProcess: pass
            process.terminate()
            _, alive = psutil.wait_procs(children + [parent], timeout=5)
            for p in alive:
                try: p.kill()
                except psutil.NoSuchProcess: pass
            process.wait()
        if samples:
            with (folder / 'host.csv').open('w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=list(samples[0]))
                writer.writeheader(); writer.writerows(samples)


def outcomes(folder):
    path = None
    # Exclude metadata r1-rate*.run.json; request summaries contain experiment/metrics.
    for candidate in sorted(folder.glob('r1-rate*-*.json')):
        if candidate.name.endswith('.run.json'): continue
        path = candidate
        break
    if path is None: raise ValueError(f'Нет результата k6: {folder}')
    data = read_json(path)
    reasons = validate_run(data['experiment'], data['metrics'])
    if reasons: raise ValueError(f'Невалидная нагрузка {folder}: {reasons}')
    rows = []
    for phase in data['experiment']['phases']:
        for priority in phase['rates']:
            rows.append(dict(phase=phase['name'], priority=priority,
                             **outcome(data['metrics'], f'phase_{phase["index"]}_{priority}', phase['durationSeconds'])))
    return rows


def baseline_capacity(root, cases):
    passing = {}
    for i, case in enumerate(cases):
        rows = outcomes(root / f'case-{i:04}')
        ok = all(r['requests'] > 0 and r['success_pct'] >= 99 and r['timeout_pct'] == 0
                 and math.isfinite(r['p90_ms']) and r['p90_ms'] < 1000 for r in rows)
        passing.setdefault(case['workload'], {}).setdefault(case['rate'], []).append(ok)
    capacities, details = {}, {}
    for w, levels in passing.items():
        good = [rate for rate, checks in levels.items() if all(checks)]
        if not good: raise ValueError(f'{w}: нет обслуживаемого уровня; задайте более низкие --{w.lower()}-rates.')
        c = max(good)
        bad_above = [rate for rate, checks in levels.items() if rate > c and not all(checks)]
        if not bad_above:
            raise ValueError(f'{w}: верхняя граница не найдена. Повторите baseline в новом каталоге с расширенной сеткой --{w.lower()}-rates.')
        capacities[w] = c
        details[w] = dict(highestPassingRps=c, firstFailingAboveRps=min(bad_above), checks=levels)
    return dict(capacities=capacities, details=details,
                note='Highest tested level passing all repeats, not exact saturation. Inspect latency/queue timelines before interpretation.')


def verify_smoke(rows):
    if not rows or any(r['requests'] == 0 or r['success_pct'] < 99 or not math.isfinite(r['p90_ms'])
                       or r['p90_ms'] >= 1000 for r in rows):
        raise ValueError('Smoke не прошёл: на малой нагрузке ожидаются успешные ответы всех классов и p90 <1000 мс.')


def load_seconds(cases):
    return sum(c.get('duration', 180) if c['profile'] == 'Plateau'
               else 60 + (30 if c['profile'] == 'Spike' else 180) + 120 for c in cases)


def execute(a):
    if a.stage == 'analyze':
        analyze(a.output_directory)
        return
    if not sys.platform.startswith('linux'): raise ValueError('Этот сценарий запускается на Ubuntu.')
    runner.check_masks(a.server_mask, a.generator_mask)
    if psutil.virtual_memory().available < 768 * MIB:
        raise ValueError('Для старта требуется хотя бы 768 MiB доступной RAM.')
    if shutil.disk_usage(ROOT).free < 3 * 1024**3:
        raise ValueError('Нужно не менее 3 GiB свободного диска для результатов.')
    if not DLL.exists(): raise ValueError('Сначала выполните run.sh prepare.')
    root = a.output_directory.resolve()
    if root.exists() and not a.resume: raise ValueError('Укажите новый каталог или --resume.')
    root.mkdir(parents=True, exist_ok=True)
    with directory_lock(root):
        config = dict(protocol=protocol(a), binarySha256=sha256(DLL), sources=source_hashes(), stage=a.stage,
                      hostId=host_identifier(),
                      binaryHashes={p.name: sha256(p) for p in sorted(DLL.parent.glob('*'))
                                    if p.is_file() and p.suffix in ('.dll', '.json')})
        if a.resume:
            old = read_json(root / 'plan.json')
            if any(old[k] != v for k, v in config.items()):
                raise ValueError('Изменены протокол, исходники или сборка. Нужен новый каталог.')
            plan = old
        else:
            if a.stage == 'smoke': cases = smoke_plan()
            elif a.stage == 'baseline':
                calibration = root / 'calibration'; calibration.mkdir()
                ca = runner.validate(runner.parser().parse_args(options(a, smoke_plan()[0], calibration)))
                runner.calibrate(ca)
                estimate = read_json(calibration / 'calibration.json')['estimatedSingleCpuCapacityRps']
                cases = baseline_plan(a, estimate)
            else:
                if a.baseline_directory is None: raise ValueError('Укажите --baseline-directory после успешного baseline.')
                base = read_json(a.baseline_directory / 'plan.json')
                if base['stage'] != 'baseline' or any(base[k] != config[k] for k in ('protocol', 'binaryHashes', 'sources', 'hostId')):
                    raise ValueError('Baseline должен иметь те же параметры, исходники и сборку.')
                if read_json(a.baseline_directory / 'status.json')['state'] != 'complete':
                    raise ValueError('Baseline не завершён.')
                cases = matrix_plan(a, read_json(a.baseline_directory / 'capacity.json')['capacities'])
            plan = dict(**config, cases=cases, createdUtc=utc(), loadSeconds=load_seconds(cases),
                        environment=environment())
            write_json(root / 'plan.json', plan)
        cases = plan['cases']
        print(f'{len(cases)} прогонов; {load_seconds(cases)/3600:.2f} ч подачи нагрузки, плюс подготовка/прогрев/анализ.', flush=True)
        if a.dry_run: return
        current = None
        try:
            for i, case in enumerate(cases):
                current = f'case-{i:04}'
                folder = root / current
                if (folder / 'accepted.json').exists():
                    observed = outcomes(folder)
                    if a.stage == 'smoke': verify_smoke(observed)
                    continue
                if folder.exists():
                    # Retain failed/partial attempts, never merge them into the new run.
                    archive = root / 'incomplete'; archive.mkdir(exist_ok=True)
                    folder.rename(archive / (current + '-' + str(time.time_ns())))
                folder.mkdir()
                write_json(root / 'status.json', dict(state='running', current=current, case=case))
                print(f'{i+1}/{len(cases)} {case}', flush=True)
                run_folder = folder / 'run'
                run_checked(options(a, case, run_folder), folder, a.min_available_mib)
                observed = outcomes(run_folder)
                if a.stage == 'smoke': verify_smoke(observed)
                # Move runner artifacts up only after validation. Keep runner.log and host.csv.
                for p in run_folder.iterdir(): p.rename(folder / p.name)
                run_folder.rmdir()
                write_json(folder / 'accepted.json', dict(case=case, acceptedUtc=utc()))
            if a.stage == 'baseline': write_json(root / 'capacity.json', baseline_capacity(root, cases))
            write_json(root / 'status.json', dict(state='complete', cases=len(cases)))
        except BaseException as error:
            write_json(root / 'status.json', dict(state='failed', current=current, error=str(error)))
            raise
    print(f'Готово: {root}. Для графиков: run.sh analyze --output-directory {root}', flush=True)


def analyze(root):
    plan = read_json(root / 'plan.json')
    rows = []
    for i, case in enumerate(plan['cases']):
        folder = root / f'case-{i:04}'
        if not (folder / 'accepted.json').exists():
            raise ValueError(f'Нет принятого результата: {folder}')
        runner.command([sys.executable, LOAD_TESTING / 'plotting/analyze-results.py', folder])
        rows.extend(dict(case=f'case-{i:04}', workload=case['workload'], variant=case['variant'],
                         rate=case['rate'], profile=case['profile'], repetition=case['repetition'], **r)
                    for r in outcomes(folder))
    with (root / 'all-runs.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    comparison(root, rows)
    print(f'Сводка: {root / "all-runs.csv"}; графики — внутри case-*.', flush=True)


def comparison(root, rows):
    """Aggregate run-level estimates, retaining counts for missing successful percentiles."""
    keys = ('workload', 'profile', 'phase', 'rate', 'variant', 'priority')
    groups = {}
    for r in rows:
        groups.setdefault(tuple(r[k] for k in keys), []).append(r)
    summary = []
    for key, group in sorted(groups.items()):
        record = dict(zip(keys, key), repetitions=len(group))
        for metric in ('success_pct', 'slo_pct', 'goodput_rps', 'p90_ms', 'p99_ms', 'shed_pct', 'timeout_pct'):
            values = [r[metric] for r in group if math.isfinite(r[metric])]
            record.update({metric+'_median': statistics.median(values) if values else None,
                           metric+'_min': min(values) if values else None,
                           metric+'_max': max(values) if values else None,
                           metric+'_observed_runs': len(values)})
        summary.append(record)
    with (root / 'comparison-summary.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(summary[0])); writer.writeheader(); writer.writerows(summary)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    for priority in ('critical', 'high'):
        for metric, unit in (('success_pct', '% HTTP 200'), ('goodput_rps', 'Timely responses/s'), ('p90_ms', 'p90 successful, ms')):
            fig, axes = plt.subplots(1, 3, figsize=(15, 4.8))
            for ax, workload in zip(axes, ('Cpu','Io','Mixed')):
                for variant in VARIANTS:
                    points = sorted((r for r in summary if r['workload']==workload and r['profile']=='Plateau'
                                     and r['priority']==priority and r['variant']==variant
                                     and r[metric+'_median'] is not None), key=lambda r:r['rate'])
                    if not points: continue
                    x=[r['rate'] for r in points]; y=[r[metric+'_median'] for r in points]
                    ax.plot(x,y,marker='o',label=variant)
                    ax.fill_between(x,[r[metric+'_min'] for r in points],[r[metric+'_max'] for r in points],alpha=.12)
                if metric=='p90_ms': ax.axhline(1000,color='black',linestyle='--',linewidth=.7)
                ax.set(title=workload, xlabel='Offered RPS', ylabel=unit)
                ax.grid(alpha=.2)
            handles, labels=axes[0].get_legend_handles_labels()
            fig.legend(handles,labels,loc='lower center',ncol=3)
            fig.suptitle(f'{priority.title()}: median and min/max across runs; percentiles not pooled')
            fig.tight_layout(rect=(0,.12,1,.93))
            fig.savefig(root / f'comparison-{priority}-{metric}.png',dpi=140)
            plt.close(fig)


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('stage', choices=('smoke', 'baseline', 'matrix', 'analyze'))
    p.add_argument('--output-directory', type=Path, required=True)
    p.add_argument('--baseline-directory', type=Path)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--repetitions', type=runner.bounded(int, 1, 20), default=3)
    p.add_argument('--iterations', type=runner.bounded(int, 1000, 10000000), default=500000)
    p.add_argument('--server-mask', type=int, default=1)
    p.add_argument('--generator-mask', type=int, default=2)
    p.add_argument('--seed', type=int, default=20261119)
    p.add_argument('--min-available-mib', type=runner.bounded(int, 128, 1024), default=192)
    for resource in ('cpu', 'io', 'mixed'):
        p.add_argument('--' + resource + '-rates', type=runner.rates)
    return p


if __name__ == '__main__':
    from runner_common import main_guard
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    main_guard(lambda: execute(parser().parse_args()))
