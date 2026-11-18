import csv
import json
import sys
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

root = Path(sys.argv[1])
analysis = json.loads((root/'trace-analysis.json').read_text())
system = [json.loads(line) for line in (root/'system.jsonl').open(encoding='utf-8')]
injections = json.loads((root/'synthetic-injections.json').read_text())
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':10})
fig,axes=plt.subplots(2,2,figsize=(13,7.4),sharex='col',layout='constrained')
for col,summary in enumerate(analysis):
    name=summary['run'];start=summary['startEpochMs']
    windows=[r for r in csv.DictReader((root/'experiment/windows.csv').open(encoding='utf-8-sig'))
             if r['run']==name and r['priority']=='critical']
    x=[float(r['start_seconds'])+float(r['duration_seconds'])/2 for r in windows]
    ax=axes[0,col]
    ax.plot(x,[float(r['p90_ms'])/1000 for r in windows],'o-',color='#7c3aed',label='p90 Critical')
    ax.axhline(1,color='#b91c1c',ls='--',lw=1,label='SLO 1 с')
    ax.set_ylim(0,5.2);ax.set_ylabel('p90 успешных ответов, с');ax.grid(alpha=.2)
    ax.set_title(('OFF — без защиты' if name.endswith('off') else 'ON — Cinnamon')+
        f"\nHTTP 200 Critical: {summary['criticalSuccessPct']:.2f}%")
    ax2=ax.twinx()
    ax2.plot(x,[float(r['success_pct']) for r in windows],'.:',color='#15803d',label='HTTP 200, %')
    ax2.set_ylim(0,105);ax2.set_ylabel('HTTP 200, %',color='#15803d')
    ax.legend(loc='upper left',fontsize=8)
    selected=[r for r in system if r['phase']['name']==name and start<=r['epochMs']<start+60000]
    bottom=axes[1,col]
    bottom.plot([(r['epochMs']-start)/1000 for r in selected],
        [r['cpuPctPerCore'][0] for r in selected],color='#64748b',alpha=.65,label='CPU 0: всего')
    servers=list(csv.DictReader((root/'experiment'/f'{name}.cpu.csv').open(encoding='utf-8-sig')))
    bottom.plot([(float(r['sampleEpochMs'])-start)/1000 for r in servers],
        [float(r['serverCpuPercentOfOneLogicalProcessor']) for r in servers],color='#2563eb',label='Сервер')
    injection=next(i for i in injections if i['run']==name)
    workers=injection.get('observedWorkerPids',injection['pids'])
    bottom.plot([(r['epochMs']-start)/1000 for r in selected],
        [sum(p['cpuPctOfOneCore'] for p in r['processes'] if p['pid'] in workers) for r in selected],
        color='#ea580c',label='Добавленная CPU-нагрузка')
    for target in (ax,bottom):
        target.axvspan((injection['startEpochMs']-start)/1000,
            (injection['stopEpochMs']-start)/1000,color='#f59e0b',alpha=.12)
        target.set_xlim(0,60)
    bottom.set_ylim(0,105);bottom.set_ylabel('% одного логического CPU')
    bottom.set_xlabel('Время подачи запросов, с');bottom.grid(alpha=.2)
    bottom.legend(loc='lower right',fontsize=8)
fig.suptitle('22 RPS: контролируемая конкуренция за CPU 0\nЖёлтая область — 30 секунд добавленной нагрузки; показатели задержки и успеха — по окнам 5 с',fontsize=13)
fig.savefig(root/'cpu-contention-evidence.png',dpi=160)
