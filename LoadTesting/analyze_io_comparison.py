"""Validate raw I/O comparison outcomes and summarize repeated protected trials."""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    a = parser.parse_args()
    root = a.directory
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8-sig'))
    status = json.loads((root / 'status.json').read_text(encoding='utf-8-sig'))
    rows = list(csv.DictReader((root / 'comparison.csv').open(encoding='utf-8-sig')))
    rows = [row for row in rows if row['strategy'] != 'Off']
    complete = status['state'] == 'complete'
    expected = {(str(rep), strategy, str(rate)) for rep in range(1, manifest['repetitions'] + 1)
                for strategy in ('Adaptive', 'Cinnamon') for rate in manifest['rates']}
    observed = {(r['repetition'], r['strategy'], r['target_rps']) for r in rows}
    if len(observed) != len(rows) or not observed <= expected or (complete and observed != expected):
        raise ValueError('Incomplete or duplicated comparison matrix')
    groups = defaultdict(list)
    validations = []
    for row in rows:
        if row['strategy'] == 'Off':
            continue
        name = f"r{row['repetition']}-{row['strategy']}-rate{row['target_rps']}"
        folder = root / name
        rate = row['target_rps']
        counts = defaultdict(float)
        latency_count = 0
        shed_times = []
        for line in (folder / f'rate{rate}.raw.jsonl').open(encoding='utf-8'):
            point = json.loads(line)
            if point.get('type') != 'Point':
                continue
            metric, data = point['metric'], point['data']
            if metric == 'dropped_iterations' and data['value']:
                raise ValueError('Generator dropped iterations in raw data: ' + name)
            if metric.startswith('measured_') and metric != 'measured_latency_ms' and metric != 'measured_success_latency_ms':
                counts[metric] += data['value']
            if metric == 'measured_success_latency_ms':
                latency_count += 1
            if metric == 'measured_shed' and data['value']:
                shed_times.append(datetime.fromisoformat(data['time'].replace('Z', '+00:00')).timestamp() * 1000)
        sent = int(row['sent'])
        outcomes = sum(counts[k] for k in ['measured_successes', 'measured_shed', 'measured_timeouts', 'measured_other_errors'])
        if counts['measured_requests'] != sent or outcomes != sent or latency_count != counts['measured_successes']:
            raise ValueError('Raw outcome attribution mismatch: ' + name)
        for column, metric in [('success_pct', 'measured_successes'), ('shed_pct', 'measured_shed'),
                               ('timeout_pct', 'measured_timeouts'), ('other_error_pct', 'measured_other_errors'),
                               ('timely_success_pct', 'measured_timely_successes')]:
            if abs(float(row[column]) - 100 * counts[metric] / sent) > 1e-8:
                raise ValueError('Summary/raw metric mismatch: ' + name + ':' + column)
        if abs(sent - int(rate) * manifest['measureSeconds']) > 1:
            raise ValueError('Offered traffic mismatch: ' + name)
        if float(row['dropped_total']) or float(row['dropped_measurement']):
            raise ValueError('Generator dropped iterations: ' + name)
        telemetry = list(csv.DictReader((folder / f'rate{rate}.telemetry.csv').open(encoding='utf-8-sig')))
        if max(int(t['active']) for t in telemetry) > 4:
            raise ValueError('I/O resource oversubscribed: ' + name)
        prefix = 'svalinn_' + row['strategy'].lower() + '_'
        for metric, maximum in [('queued', 4), ('inflight', 4), ('limit', 4)]:
            field = next(key for key in telemetry[0] if key.startswith(prefix + metric)
                         and key[len(prefix + metric):] in ('', '{group="default"}'))
            if max(float(t[field]) for t in telemetry) > maximum:
                raise ValueError('Controller bound exceeded: ' + name + ':' + metric)
        details = dict(run=name, valid=True, requests=sent, successes=int(counts['measured_successes']),
                       shed=int(counts['measured_shed']), timely=int(counts['measured_timely_successes']),
                       timeouts=int(counts['measured_timeouts']), otherErrors=int(counts['measured_other_errors']))
        if row['strategy'] == 'Adaptive':
            probe_epochs = [float(t['sampleEpochMs']) for t in telemetry if float(t['svalinn_adaptive_probing'])]
            details.update(shedNearSampledProbe=sum(any(abs(epoch - sample) <= 750 for sample in probe_epochs) for epoch in shed_times),
                latencyBackoffs=max(float(t['svalinn_adaptive_latency_backoffs_total']) for t in telemetry),
                failureBackoffs=max(float(t['svalinn_adaptive_failure_backoffs_total']) for t in telemetry),
                sampledQueueMax=max(float(t['svalinn_adaptive_queued']) for t in telemetry),
                internalIoQueueMax=max(int(t['waiting']) for t in telemetry))
        validations.append(details)
        row['goodput_rps'] = counts['measured_timely_successes'] / manifest['measureSeconds']
        groups[int(rate), row['strategy']].append(row)
    aggregates = []
    for (rate, strategy), group in sorted(groups.items()):
        record = dict(rate=rate, strategy=strategy, repetitions=len(group))
        for metric in ['success_pct', 'timely_success_pct', 'successful_p90_ms', 'goodput_rps']:
            values = [float(r[metric]) for r in group]
            record[metric] = dict(median=statistics.median(values), minimum=min(values), maximum=max(values))
        aggregates.append(record)
    result = dict(complete=complete, protocol=manifest, validations=validations, aggregates=aggregates,
                  note='Medians and min/max of run-level metrics; p90 values are not pooled percentiles. Probe correlation uses sampled epochs +/-750 ms.')
    (root / 'comparison-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    lines = ['Adaptive с приоритетной очередью и Cinnamon: I/O',
             f"Статус: {'завершено' if complete else 'промежуточные данные'}. Прогонов: {len(rows)}.",
             'Одинаковые настройки: 4 I/O-слота, обслуживание 150±50 мс, InitialLimit/MaxLimit=4, QueueCapacity=4, QueueTimeoutMs=250.',
             f"Прогрев {manifest['warmupSeconds']} с, измерение {manifest['measureSeconds']} с. Normal, SLO своевременности <500 мс.",
             'Медиана показателя отдельных прогонов [min..max]; p90 разных прогонов не объединяется.',
             'RPS | Стратегия | HTTP 200 % | HTTP 200 <500 мс % | p90 HTTP 200 мс | Своевременный goodput RPS']
    for r in aggregates:
        fields = []
        for metric in ['success_pct', 'timely_success_pct', 'successful_p90_ms', 'goodput_rps']:
            v = r[metric]
            fields.append(f"{v['median']:.2f} [{v['minimum']:.2f}..{v['maximum']:.2f}]")
        lines.append(f"{r['rate']} | {r['strategy']} | " + ' | '.join(fields))
    lines += ['', 'Проверки по сырым данным k6: исходы суммируются до числа отправленных запросов, успешные измерения согласованы с HTTP 200, фактический поток соответствует заданному, пропусков генератора нет.',
              f"Ограничения: короткие прогоны, повторов по плану {manifest['repetitions']}, фоновые процессы не изолированы. Только Normal; защиту важных классов этот профиль не измеряет. Исторические серии не являются парным контролем.",
              'Сырые данные, настройки, телеметрия и исходники сохранены рядом. Details: comparison-summary.json.']
    (root / 'report.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
