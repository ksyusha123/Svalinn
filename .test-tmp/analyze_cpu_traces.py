import collections
import csv
import datetime
import json
import math
import statistics
import sys
from pathlib import Path

root = Path(sys.argv[1])
system = [json.loads(line) for line in (root / 'system.jsonl').open(encoding='utf-8')]

def percentile(values, p):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values)-1)*p
    low = math.floor(pos)
    high = math.ceil(pos)
    return values[low]*(high-pos)+values[high]*(pos-low) if low != high else values[low]

def stat(values):
    return dict(count=len(values), mean=statistics.mean(values) if values else None,
                p90=percentile(values,.9), maximum=max(values) if values else None)

def epoch(value):
    return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1000

summaries = []
injections = json.loads((root/'synthetic-injections.json').read_text()) if (root/'synthetic-injections.json').exists() else []
for path in sorted((root/'experiment').glob('r*.json')):
    if path.name.endswith('.run.json'):
        continue
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if 'metrics' not in data:
        continue
    name = path.stem
    metrics = data['metrics']
    start = metrics['test_clock_epoch_ms']['values']['med']
    end = start + 60_000
    rows = list(csv.DictReader((root/'experiment'/f'{name}.cpu.csv').open()))
    server_rows = [r for r in rows if start <= float(r['sampleEpochMs']) < end]
    samples = [r for r in system if start <= r['epochMs'] < end and r['phase']['name']==name]
    pid = collections.Counter(r['phase']['serverPid'] for r in samples).most_common(1)[0][0]
    processes = collections.defaultdict(list)
    server_by_sample = []
    for sample in samples:
        cpu = next((p['cpuPctOfOneCore'] for p in sample['processes'] if p['pid']==pid),0)
        server_by_sample.append(cpu)
        for p in sample['processes']:
            if p['pid'] != pid:
                processes[(p['pid'],p['name'])].append(p['cpuPctOfOneCore'])
    top_background = sorted([dict(pid=key[0],name=key[1],
        meanCpuPctOfOneCore=sum(values)/len(samples), maxCpuPctOfOneCore=max(values))
        for key,values in processes.items()], key=lambda r:r['meanCpuPctOfOneCore'],reverse=True)[:12]
    event_file = root/f'{name}.events.jsonl'
    counters = collections.defaultdict(list)
    contention = []
    gc_times = []
    gc_start = None
    gc_count = 0
    runtime_counts = collections.Counter()
    if event_file.exists():
        for line in event_file.open(encoding='utf-8'):
            event = json.loads(line)
            if not start <= epoch(event['utc']) < end:
                continue
            key = event['name']
            runtime_counts[key] += 1
            payload = event['payload']
            if event['provider']=='System.Runtime':
                for value in payload.values():
                    if isinstance(value,dict):
                        counter = value.get('Payload',value)
                        if 'Name' in counter:
                            counters[counter['Name']].append(counter.get('Mean', counter.get('Increment',0)))
            if key=='Contention/Stop':
                contention.append(payload.get('DurationNs',0)/1_000_000)
            if key=='GC/Start':
                gc_count += 1
            # Reason=1 is a GC suspension. Reason=0 includes SampleProfiler suspensions.
            if key=='GC/SuspendEEStart' and payload.get('Reason')==1:
                gc_start=event['timeMs']
            if key=='GC/RestartEEStop' and gc_start is not None:
                gc_times.append(event['timeMs']-gc_start)
                gc_start=None
    work_samples = 0
    work_frames = collections.Counter()
    work_leaf = collections.Counter()
    contention_frames = collections.Counter()
    stacks_file = root/f'{name}.stacks.jsonl'
    if stacks_file.exists():
        for line in stacks_file.open(encoding='utf-8'):
            event=json.loads(line)
            if not start <= epoch(event['utc']) < end:
                continue
            frames=event['frames']
            if 'Contention' in event['name']:
                contention_frames.update(set(frames))
            if any('CpuBenchmark.Work' in f or 'Pbkdf2' in f or 'BCryptKeyDerivation' in f for f in frames):
                work_samples += 1
                work_leaf.update(frames[:1])
                work_frames.update(set(frames))
    summary = dict(run=name,startEpochMs=start,serverPid=pid,
        criticalSuccessPct=100*metrics['critical_success']['values']['rate'],
        criticalP90Ms=metrics['critical_success_latency_ms']['values']['p(90)'],
        criticalSloPct=100*metrics['critical_slo']['values']['rate'],
        allP90Ms=metrics['http_req_duration']['values']['p(90)'],
        generatorDropped=metrics.get('dropped_iterations',{}).get('values',{}).get('count',0),
        cpu0=stat([r['cpuPctPerCore'][0] for r in samples]),
        serverCpu=stat([float(r['serverCpuPercentOfOneLogicalProcessor']) for r in server_rows]),
        cpu0MinusServerApprox=stat([max(0,r['cpuPctPerCore'][0]-cpu) for r,cpu in zip(samples,server_by_sample)]),
        systemSamplingIntervalSeconds=stat([r['intervalSeconds'] for r in samples]),
        topBackground=top_background, runtimeCounters={k:stat(v) for k,v in counters.items()},
        contentionMs=stat(contention),contentionTotalMs=sum(contention),gcCollections=gc_count,
        gcPauseMs=stat(gc_times),gcPauseTotalMs=sum(gc_times),
        runtimeEvents=dict(runtime_counts),workStackSamples=work_samples,
        workLeaf=work_leaf.most_common(12),workFrames=work_frames.most_common(30),
        contentionFrames=contention_frames.most_common(12))
    if injections:
        injection = next((i for i in injections if i['run']==name), None)
        if injection:
            boundaries = [('before',start,injection['startEpochMs']),
                          ('during',injection['startEpochMs'],injection['stopEpochMs']),
                          ('after',injection['stopEpochMs'],end)]
            summary['injectionSegments'] = {}
            for label,low,high in boundaries:
                selected = [r for r in samples if low <= r['epochMs'] < high]
                server_selected = [r for r in server_rows if low <= float(r['sampleEpochMs']) < high]
                summary['injectionSegments'][label] = dict(
                    cpu0=stat([r['cpuPctPerCore'][0] for r in selected]),
                    serverCpu=stat([float(r['serverCpuPercentOfOneLogicalProcessor']) for r in server_selected]),
                    syntheticCpu=stat([sum(p['cpuPctOfOneCore'] for p in r['processes'] if p['pid'] in injection.get('observedWorkerPids',injection['pids'])) for r in selected]))
    if name.endswith('-on'):
        for field in ['cinnamonQueued','cinnamonRejectionRatio','cinnamonLimit',
                      'cinnamonQueueTimeout','cinnamonPriorityRejected']:
            summary[field]=stat([float(r[field]) for r in server_rows if r[field]])
    summaries.append(summary)
(root/'trace-analysis.json').write_text(json.dumps(summaries,ensure_ascii=False,indent=2),encoding='utf-8')
for s in summaries:
    print(s['run'],'Critical',round(s['criticalSuccessPct'],2),round(s['criticalP90Ms'],1),
        'CPU0',round(s['cpu0']['mean'],1),'server',round(s['serverCpu']['mean'],1),
        'contention ms',round(s['contentionTotalMs'],2),'GC ms',round(s['gcPauseTotalMs'],2),
        'TPqueue',s['runtimeCounters'].get('threadpool-queue-length',{}).get('maximum'))
