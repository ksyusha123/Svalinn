import collections
import argparse
import contextlib
import ctypes
import csv
import json
import os
import shutil
import struct
import sys
import threading
import time
from pathlib import Path
import psutil

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'LoadTesting'))
import run_experiment as runner
from runner_common import stamp,write_json,read_json,sha256,directory_lock,command
parser=argparse.ArgumentParser()
parser.add_argument('--server-cpu',type=int)
parser.add_argument('--repetitions',type=int,default=2)
args=parser.parse_args()

output=ROOT/'LoadTesting/results'/('free-core-comparison-'+stamp())
output.mkdir(parents=True)
print('OUTPUT',output,flush=True)
available=psutil.Process().cpu_affinity()

def topology():
    length=ctypes.c_ulong()
    call=ctypes.WinDLL('kernel32').GetLogicalProcessorInformationEx
    call(0,None,ctypes.byref(length))
    buffer=ctypes.create_string_buffer(length.value)
    if not call(0,buffer,ctypes.byref(length)):raise ctypes.WinError()
    offset=0;cores=[]
    while offset<length.value:
        relationship,size=struct.unpack_from('II',buffer,offset)
        mask=struct.unpack_from('Q',buffer,offset+32)[0]
        cpus=[i for i in range(64) if mask>>i&1 and i in available]
        if cpus:cores.append(cpus)
        offset+=size
    return cores

cores=topology()
print('PREFLIGHT 30 seconds',cores,flush=True)
psutil.cpu_percent(percpu=True)
preflight=[]
for _ in range(60):
    time.sleep(.5)
    preflight.append(dict(epochMs=time.time()*1000,cpuPctPerCore=psutil.cpu_percent(percpu=True)))
scores=[dict(cpus=core,meanPairLoad=sum(sum(r['cpuPctPerCore'][i] for i in core) for r in preflight)/len(preflight),
             maxPairLoad=max(sum(r['cpuPctPerCore'][i] for i in core) for r in preflight)) for core in cores]
scores.sort(key=lambda s:(s['meanPairLoad'],-max(s['cpus'])))
if args.server_cpu is not None:
    forced=next(s for s in scores if args.server_cpu in s['cpus'])
    scores.remove(forced);scores.insert(0,forced)
if scores[0]['meanPairLoad']>10:raise RuntimeError('No sufficiently quiet physical core in preflight')
server_core=scores[0]['cpus'];server_cpu=server_core[0]
generator_core=scores[1]['cpus'];generator_cpu=generator_core[0]
monitor_cpu=next(c[0] for c in reversed(cores) if c not in [server_core,generator_core])
write_json(output/'preflight.json',dict(samples=preflight,ranking=scores))
print('SELECTED server',server_core,'generator',generator_core,'monitor',monitor_cpu,flush=True)

binary=output/'binary'
shutil.copytree(ROOT/'TestApp/bin/Release/net10.0',binary)
runner.DLL=binary/'TestApp.dll'
phase={'strategy':None,'name':'setup','serverPid':None}
stop=threading.Event()

class UnicodeString(ctypes.Structure):
    _fields_=[('length',ctypes.c_ushort),('maximum',ctypes.c_ushort),('buffer',ctypes.c_void_p)]
class ProcessInfo(ctypes.Structure):
    _fields_=[('next',ctypes.c_ulong),('threads',ctypes.c_ulong),('private',ctypes.c_longlong),
        ('faults',ctypes.c_ulong),('threadsHigh',ctypes.c_ulong),('cycles',ctypes.c_ulonglong),
        ('created',ctypes.c_longlong),('user',ctypes.c_longlong),('kernel',ctypes.c_longlong),
        ('image',UnicodeString),('priority',ctypes.c_long),('pid',ctypes.c_void_p),('parent',ctypes.c_void_p)]
query=ctypes.WinDLL('ntdll').NtQuerySystemInformation
query.argtypes=[ctypes.c_ulong,ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(ctypes.c_ulong)]
query.restype=ctypes.c_long
def snapshot_processes():
    size=4*1024*1024
    while True:
        buffer=ctypes.create_string_buffer(size);needed=ctypes.c_ulong()
        status=query(5,buffer,size,ctypes.byref(needed))
        if status==0:break
        if status&0xffffffff!=0xc0000004:raise OSError(f'Process snapshot failed: {status:x}')
        size=max(size*2,needed.value)
    offset=0
    while True:
        info=ProcessInfo.from_buffer(buffer,offset)
        if info.pid:
            name=ctypes.wstring_at(info.image.buffer,info.image.length//2) if info.image.buffer else 'System'
            yield info.pid,name,info.created,(info.user+info.kernel)/10_000_000
        if not info.next:break
        offset+=info.next

def monitor():
    previous={};last=time.monotonic();psutil.cpu_percent(percpu=True)
    try:
        with (output/'system.jsonl').open('w',encoding='utf-8',buffering=1) as stream:
            while not stop.wait(.5):
                now=time.monotonic();elapsed=now-last;current={};processes=[]
                for pid,name,created,cpu in snapshot_processes():
                    key=(pid,created);current[key]=cpu
                    pct=100*(cpu-previous.get(key,cpu))/elapsed
                    if pct>.1:processes.append(dict(pid=pid,name=name,cpuPctOfOneCore=round(pct,2)))
                stream.write(json.dumps(dict(epochMs=time.time()*1000,phase=phase.copy(),intervalSeconds=elapsed,
                    cpuPctPerCore=psutil.cpu_percent(percpu=True),processes=sorted(processes,key=lambda p:-p['cpuPctOfOneCore'])),ensure_ascii=False)+'\n')
                previous,last=current,now
    except BaseException as error:
        write_json(output/'monitor-error.json',dict(error=repr(error)))
        raise

original_check=runner.check_masks
def check_masks(server,generator=None):
    process=psutil.Process();previous=process.cpu_affinity()
    try:
        process.cpu_affinity(available);original_check(server,generator)
    finally:process.cpu_affinity(previous)
runner.check_masks=check_masks
original_start=runner.start_server
class Server:
    def __init__(self,child):self.child=child
    def __getattr__(self,key):return getattr(self.child,key)
    def __enter__(self):return self
    def __exit__(self,*args):
        self.child.close();phase.update(name='between-runs',serverPid=None)
def start_server(a,enabled,name):
    phase.update(strategy=a.strategy,name=name,serverPid=None)
    # The child inherits its one-CPU affinity before CLR/Server-GC initialization.
    # Pinning only inside TestApp is too late to configure CLR startup topology.
    process=psutil.Process();process.cpu_affinity([server_cpu])
    try:child=original_start(a,enabled,name)
    finally:process.cpu_affinity([monitor_cpu])
    phase['serverPid']=child.process.pid
    return Server(child)
runner.start_server=start_server

schedule=[]
for repetition in range(1,args.repetitions+1):
    for load in ((22,72) if repetition==1 else (72,22)):
        order=['Adaptive','Cinnamon'] if (repetition==1)==(load==22) else ['Cinnamon','Adaptive']
        for strategy in order:
            for mode in ('off','on'):
                schedule.append(dict(repetition=repetition,load=load,strategy=strategy,mode=mode))
write_json(output/'plan.json',dict(serverCpu=server_cpu,serverPhysicalCore=server_core,
    generatorCpu=generator_cpu,generatorPhysicalCore=generator_core,monitorCpu=monitor_cpu,
    preflightSeconds=30,durationSeconds=60,repetitions=args.repetitions,rates=[22,72],schedule=schedule,
    pairing='OFF then ON for each strategy/rate; strategy and rate order reversed on repeat 2',
    quietPrePairMeanPairLoadThreshold=10,quietPrePairMaxPairLoadThreshold=25,
    binarySha256=sha256(runner.DLL),dotnetProcessorCountOverride=os.environ.get('DOTNET_PROCESSOR_COUNT'),
    traceEnabled=False,processorAffinityIsNotExclusive=True,
    startupAffinity='one logical CPU inherited before CLR startup'))

psutil.Process().cpu_affinity([monitor_cpu])
monitor_thread=threading.Thread(target=monitor,daemon=True);monitor_thread.start()
started=time.monotonic();prepared={};quiet_checks=[]
try:
    with contextlib.ExitStack() as stack:
        for strategy in ('Adaptive','Cinnamon'):
            config=runner.validate(runner.parser().parse_args([
                '--scenario','SuccessDisaster','--workload','Cpu','--strategy',strategy,
                '--rates','22,72','--duration-seconds','60','--repetitions',str(args.repetitions),
                '--server-affinity-mask',str(1<<server_cpu),'--generator-affinity-mask',str(1<<generator_cpu),
                '--skip-build','--output-directory',str(output/strategy)]))
            config.output_directory.mkdir()
            stack.enter_context(directory_lock(config.output_directory))
            current=runner.settings(config);runner.snapshot(config.output_directory,current,False)
            runner.calibrate(config);prepared[strategy]=(config,current)
        for index,case in enumerate(schedule):
            config,current=prepared[case['strategy']]
            if case['mode']=='off':
                phase.update(strategy=case['strategy'],name='quiet-check',serverPid=None)
                for attempt in range(6):
                    readings=[psutil.cpu_percent(interval=.5,percpu=True) for _ in range(10)]
                    loads=[sum(r[c] for c in server_core) for r in readings]
                    result=dict(case=case,epochMs=time.time()*1000,attempt=attempt,
                                mean=sum(loads)/len(loads),maximum=max(loads))
                    quiet_checks.append(result);write_json(output/'quiet-checks.json',quiet_checks)
                    if result['mean']<=10 and result['maximum']<=25:break
                    print('WAIT FOR QUIET CORE',result,flush=True)
                else:raise RuntimeError('Selected physical core did not become quiet; preserve completed runs')
            name=f"r{case['repetition']}-rate{case['load']}-{case['mode']}"
            print('CASE',index+1,'/',len(schedule),case,flush=True)
            runner.run_one(config,current,case['repetition'],case['load'],case['mode'],name,'')
            write_json(output/'status.json',dict(completed=index+1,total=len(schedule),last=case,elapsedSeconds=time.monotonic()-started))
        for strategy,(config,_) in prepared.items():
            phase.update(strategy=strategy,name='analysis',serverPid=None)
            command([sys.executable,ROOT/'LoadTesting/plotting/analyze-results.py',config.output_directory])
    if (output/'monitor-error.json').exists():raise RuntimeError('System monitor failed')
    write_json(output/'completed.json',dict(elapsedSeconds=time.monotonic()-started))
finally:
    stop.set();monitor_thread.join(timeout=10)
print('COMPLETE',output,flush=True)
