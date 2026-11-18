import collections
import csv
import json
import math
import statistics
import sys
from pathlib import Path

root=Path(sys.argv[1])
plan=json.loads((root/'plan.json').read_text())
system=[json.loads(line) for line in (root/'system.jsonl').open(encoding='utf-8')]
server_cpu=plan['serverCpu'];siblings=[c for c in plan['serverPhysicalCore'] if c!=server_cpu]

def read_csv(path):return list(csv.DictReader(path.open(encoding='utf-8-sig')))
def summary(values):
    values=[v for v in values if math.isfinite(v)]
    return dict(median=statistics.median(values),minimum=min(values),maximum=max(values)) if values else {}

runs=[]
for strategy in ('Adaptive','Cinnamon'):
    folder=root/strategy
    if not (folder/'runs.csv').exists():continue
    phases=read_csv(folder/'phases.csv')
    for run in read_csv(folder/'runs.csv'):
        name=run['run']
        data=json.loads((folder/f'{name}.json').read_text(encoding='utf-8-sig'))
        metrics=data['metrics'];start=metrics['test_clock_epoch_ms']['values']['med'];end=start+60000
        samples=[s for s in system if start<=s['epochMs']<end and s['phase']['strategy']==strategy and s['phase']['name']==name]
        pid=collections.Counter(s['phase']['serverPid'] for s in samples).most_common(1)[0][0]
        cpu=[float(r['serverCpuPercentOfOneLogicalProcessor']) for r in read_csv(folder/f'{name}.cpu.csv') if start<=float(r['sampleEpochMs'])<end]
        total=statistics.mean(s['cpuPctPerCore'][server_cpu] for s in samples)
        own=statistics.mean(next((p['cpuPctOfOneCore'] for p in s['processes'] if p['pid']==pid),0) for s in samples)
        sibling=statistics.mean(sum(s['cpuPctPerCore'][c] for c in siblings) for s in samples)
        background=max(0,total-own)
        intervals=[s['intervalSeconds'] for s in samples]
        coverage=(samples[-1]['epochMs']-samples[0]['epochMs'])/1000
        quiet=background<=10 and sibling<=5 and len(samples)>=90 and coverage>=57 and statistics.median(intervals)<=1
        selected=[p for p in phases if p['run']==name]
        row=dict(strategy=strategy,run=name,mode=run['mode'],rate=int(run['load_key'].removeprefix('rate')),
            repetition=int(run['repetition']),valid=run['valid']=='True',invalidReasons=run['invalid_reasons'],
            monitorSamples=len(samples),monitorCoverageSeconds=coverage,
            samplingMedianSeconds=statistics.median(intervals),serverPid=pid,startEpochMs=start,
            serverCpuMean=statistics.mean(cpu),serverCoreTotalMean=total,
            estimatedBackgroundServerCpuMean=background,siblingCpuMean=sibling,quietCoreObserved=quiet,
            totalTimelyGoodputRps=sum(float(p['goodput_rps']) for p in selected),
            priorities={p['priority']:{key:float(p[key]) for key in ('requests','success_pct','slo_pct','shed_pct','timeout_pct','p90_ms','goodput_rps')} for p in selected})
        runs.append(row)
groups=[]
for strategy in ('Adaptive','Cinnamon'):
    for rate in (22,72):
        for mode in ('off','on'):
            selected=[r for r in runs if r['strategy']==strategy and r['rate']==rate and r['mode']==mode]
            if not selected:continue
            group=dict(strategy=strategy,rate=rate,mode=mode,runCount=len(selected),
                allValid=all(r['valid'] for r in selected),allQuiet=all(r['quietCoreObserved'] for r in selected),
                priorities={p:{metric:summary([r['priorities'][p][metric] for r in selected])
                    for metric in ('success_pct','slo_pct','p90_ms','shed_pct','timeout_pct','goodput_rps')}
                    for p in ('critical','high','normal','low')},
                totalTimelyGoodputRps=summary([r['totalTimelyGoodputRps'] for r in selected]),
                serverCpuMean=summary([r['serverCpuMean'] for r in selected]),
                estimatedBackgroundServerCpuMean=summary([r['estimatedBackgroundServerCpuMean'] for r in selected]),
                siblingCpuMean=summary([r['siblingCpuMean'] for r in selected]))
            groups.append(group)
(root/'comparison-summary.json').write_text(json.dumps(dict(runs=runs,groups=groups),indent=2,ensure_ascii=False),encoding='utf-8')
for g in groups:
    c=g['priorities']['critical'];l=g['priorities']['low']
    print(g['strategy'],g['rate'],g['mode'],'valid',g['allValid'],'quiet',g['allQuiet'],
          'critical success',c['success_pct'],'p90',c['p90_ms'],'Low shed',l['shed_pct'])

report=['CPU-сравнение Adaptive/Cinnamon на выбранном малозагруженном ядре, 22 и 72 RPS',
        f"Сервер CPU {server_cpu}; SMT-соседи {siblings}; k6 CPU {plan['generatorCpu']}; монитор CPU {plan['monitorCpu']}.",
        f"Повторов: {plan['repetitions']}, 60 секунд на прогон. OFF -> ON для каждой пары. " + ('Порядок стратегий и уровней изменён во втором повторе.' if plan['repetitions']>1 else 'Один повтор: результаты предварительные.'),
        'В таблице медианы отдельных прогонов; диапазоны min..max. Процентили разных прогонов не объединены.',
        'SLO: p90 успешных Critical <1000 мс; успешность и доля своевременных ответов приведены отдельно.','',
        'RPS | Стратегия | Режим | Critical HTTP 200 % | Critical p90 мс | Critical своевременно % | Low сброс % | Goodput всего RPS']
def display(values):return f"{values['median']:.2f} [{values['minimum']:.2f}..{values['maximum']:.2f}]" if values else 'нет данных'
for g in groups:
    c=g['priorities']['critical'];l=g['priorities']['low']
    report.append(f"{g['rate']} | {g['strategy']} | {g['mode']} | {display(c['success_pct'])} | {display(c['p90_ms'])} | {display(c['slo_pct'])} | {display(l['shed_pct'])} | {display(g['totalTimelyGoodputRps'])}")
report+=['','Контроль среды по каждому прогону:','Расчётный фон CPU сервера = max(0, средняя общая загрузка логического CPU минус CPU процесса сервера).',
    'Это приближённая оценка, а не эксклюзивное резервирование ядра. Загрузка SMT-соседа измеряется отдельно.',
    'Критерий чистого прогона: фон <=10%, сосед <=5% в среднем; >=90 снимков, покрытие >=57 с, медианный интервал <=1 с.']
for r in runs:
    report.append(f"{r['strategy']} {r['run']}: valid={r['valid']}, quiet={r['quietCoreObserved']}, CPU={r['serverCpuMean']:.2f}%, background~{r['estimatedBackgroundServerCpuMean']:.2f}%, sibling={r['siblingCpuMean']:.2f}%, samples={r['monitorSamples']}, coverage={r['monitorCoverageSeconds']:.2f}s")
report+=['','Ограничения: короткая серия не даёт устойчивой статистической оценки; нагрузка ступенчатая, без проверки длительного восстановления.',
    'Настройки стратегий — defaults runner, не defaults appsettings; все значения находятся в manifest.json.',
    'Трассировка .NET в этой серии отключена. Другие пользовательские процессы не останавливались и их affinity не менялась.',
    'Сохранены бинарные файлы, хеши, исходники, manifest, логи, k6 JSON, controller CSV, системный мониторинг и предварительные проверки ядра.',
    'Результаты OFF каждой стратегии — отдельные контрольные прогоны, их нельзя считать независимыми измерениями эффекта другой стратегии.']
(root/'report.txt').write_text('\n'.join(report)+'\n',encoding='utf-8')
