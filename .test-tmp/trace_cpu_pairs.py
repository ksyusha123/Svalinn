import csv
import ctypes
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'LoadTesting'))
import run_experiment as runner
from runner_common import stamp, write_json, sha256

options = argparse.ArgumentParser()
options.add_argument('--repetitions',type=int,default=3)
options.add_argument('--synthetic-peers',type=int,default=0)
diagnostic_options = options.parse_args()

output = ROOT / 'LoadTesting/results' / ('cinnamon-traced-pairs-' + stamp())
output.mkdir(parents=True)
binary = output / 'binary'
shutil.copytree(ROOT / 'TestApp/bin/Release/net10.0', binary)
runner.DLL = binary / 'TestApp.dll'
probe = ROOT / '.test-tmp/TraceProbe/bin/Release/net10.0/TraceProbe.dll'
phase = {'name': 'setup', 'serverPid': None}
stop_monitor = threading.Event()
available_cpus = psutil.Process().cpu_affinity()
original_check_masks = runner.check_masks
def check_masks(server, generator=None):
    process = psutil.Process()
    previous = process.cpu_affinity()
    try:
        process.cpu_affinity(available_cpus)
        original_check_masks(server, generator)
    finally:
        process.cpu_affinity(previous)
runner.check_masks = check_masks
psutil.Process().cpu_affinity([4])

# One native system snapshot avoids opening a handle for every background process.
class UnicodeString(ctypes.Structure):
    _fields_ = [('length', ctypes.c_ushort), ('maximum', ctypes.c_ushort), ('buffer', ctypes.c_void_p)]
class ProcessInfo(ctypes.Structure):
    _fields_ = [('next', ctypes.c_ulong), ('threads', ctypes.c_ulong),
                ('private', ctypes.c_longlong), ('faults', ctypes.c_ulong),
                ('threadsHigh', ctypes.c_ulong), ('cycles', ctypes.c_ulonglong),
                ('created', ctypes.c_longlong), ('user', ctypes.c_longlong),
                ('kernel', ctypes.c_longlong), ('image', UnicodeString),
                ('priority', ctypes.c_long), ('pid', ctypes.c_void_p), ('parent', ctypes.c_void_p)]
query = ctypes.WinDLL('ntdll').NtQuerySystemInformation
query.argtypes = [ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
query.restype = ctypes.c_long

def process_snapshot():
    size = 4 * 1024 * 1024
    while True:
        buffer = ctypes.create_string_buffer(size)
        needed = ctypes.c_ulong()
        status = query(5, buffer, size, ctypes.byref(needed))
        if status == 0:
            break
        if status & 0xffffffff != 0xc0000004:
            raise OSError(f'NtQuerySystemInformation: {status & 0xffffffff:x}')
        size = max(size * 2, needed.value)
    offset = 0
    while True:
        info = ProcessInfo.from_buffer(buffer, offset)
        pid = info.pid or 0
        if pid:
            name = ctypes.wstring_at(info.image.buffer, info.image.length // 2) if info.image.buffer else 'System'
            yield pid, name, info.created, (info.user + info.kernel) / 10_000_000
        if not info.next:
            break
        offset += info.next


def monitor():
    previous = {}
    previous_time = time.monotonic()
    psutil.cpu_percent(percpu=True)
    with (output / 'system.jsonl').open('w', encoding='utf-8', buffering=1) as stream:
        while not stop_monitor.wait(.5):
            now = time.monotonic()
            elapsed = now - previous_time
            processes = []
            current = {}
            for pid, name, created, cpu in process_snapshot():
                key = (pid, created)
                current[key] = cpu
                percent = 100 * (cpu - previous.get(key, cpu)) / elapsed
                if percent > .1:
                    processes.append({'pid': pid, 'name': name,
                                      'cpuPctOfOneCore': round(percent, 2)})
            processes.sort(key=lambda p: p['cpuPctOfOneCore'], reverse=True)
            row = {'epochMs': time.time()*1000, 'phase': phase.copy(),
                   'intervalSeconds': elapsed, 'cpuPctPerCore': psutil.cpu_percent(percpu=True),
                   'availableMemoryBytes': psutil.virtual_memory().available,
                   'processes': processes}
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
            previous, previous_time = current, now


original_start = runner.start_server
original_child = runner.Child
injections = []
injection_threads = []

def create_child(*args, **kwargs):
    child = original_child(*args, **kwargs)
    if diagnostic_options.synthetic_peers and str(args[0][0]).lower().endswith('k6.exe'):
        run_name = phase['name']
        def inject():
            time.sleep(10)
            peers = []
            try:
                for _ in range(diagnostic_options.synthetic_peers):
                    peer = subprocess.Popen([sys._base_executable,'-c',
                        'import ctypes,time\nk=ctypes.WinDLL("kernel32");k.GetCurrentProcess.restype=ctypes.c_void_p;h=k.GetCurrentProcess();k.SetProcessAffinityMask.argtypes=[ctypes.c_void_p,ctypes.c_size_t];k.SetPriorityClass.argtypes=[ctypes.c_void_p,ctypes.c_ulong];assert k.SetProcessAffinityMask(h,1);assert k.SetPriorityClass(h,0x8000)\nwhile True:\n end=time.perf_counter()+.075\n while time.perf_counter()<end: pass\n time.sleep(.025)'],
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    peers.append(peer)
                event = dict(run=run_name,startEpochMs=time.time()*1000,pids=[p.pid for p in peers],cpu=0,
                             dutyCycle=.75,periodMs=100,priority='AboveNormal')
                injections.append(event)
                write_json(output/'synthetic-injections.json',injections)
                print('INJECT CPU PEERS',run_name,event['pids'],flush=True)
                time.sleep(30)
            finally:
                for peer in peers:
                    if peer.poll() is None:
                        peer.terminate()
                    peer.wait()
                if peers:
                    event['stopEpochMs']=time.time()*1000
                    write_json(output/'synthetic-injections.json',injections)
                    print('REMOVE CPU PEERS',run_name,flush=True)
        thread = threading.Thread(target=inject)
        injection_threads.append(thread)
        thread.start()
    return child


class TracedServer:
    def __init__(self, server, trace, recorder, log):
        self.server, self.trace, self.recorder, self.log = server, trace, recorder, log
    def __getattr__(self, name):
        return getattr(self.server, name)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        try:
            Path(str(self.trace) + '.stop').touch()
            try:
                code = self.recorder.wait(timeout=30)
                if code != 0:
                    raise RuntimeError(f'Trace recorder failed: {code}')
            except subprocess.TimeoutExpired:
                self.recorder.kill()
                self.recorder.wait()
                raise RuntimeError('Trace recorder timed out')
        finally:
            self.log.close()
            self.server.close()
            phase.update(name='between-runs', serverPid=None)


def start_server(a, enabled, name):
    phase.update(name=name, serverPid=None)
    server = original_start(a, enabled, name)
    phase['serverPid'] = server.process.pid
    if not name.startswith('r'):
        return server
    trace = output / (name + '.nettrace')
    log = (output / (name + '.trace.log')).open('wb')
    recorder = subprocess.Popen(['dotnet', str(probe), 'record', str(server.process.pid), str(trace)],
        stdout=log, stderr=log, creationflags=subprocess.CREATE_NO_WINDOW)
    psutil.Process(recorder.pid).cpu_affinity([6])
    try:
        for _ in range(300):
            if Path(str(trace) + '.ready').exists():
                print('TRACE READY', name, 'pid', server.process.pid, flush=True)
                return TracedServer(server, trace, recorder, log)
            if recorder.poll() is not None:
                raise RuntimeError('Trace recorder exited; see trace.log')
            time.sleep(.1)
        raise RuntimeError('Trace recorder did not become ready')
    except BaseException:
        if recorder.poll() is None:
            recorder.terminate()
            recorder.wait()
        log.close()
        server.close()
        raise


runner.start_server = start_server
runner.Child = create_child
runner.ordered_modes = lambda modes, repetition: [m for m in ('off', 'on') if m in modes]
write_json(output / 'diagnostic-plan.json', {
    'runOrder': [f'r{r}-{m}' for r in range(1,diagnostic_options.repetitions+1) for m in ('off','on')],
    'syntheticPeers': diagnostic_options.synthetic_peers,
    'durationSeconds': 60, 'rate': 22, 'serverCpu': 0, 'generatorCpu': 2,
    'monitorCpu': 4, 'traceRecorderCpu': 6,
    'runtimeKeywords': '0x14001: GC, contention, threading',
    'sampleProfiler': True, 'runtimeCountersIntervalSeconds': 1,
    'systemSamplingNominalSeconds': .5,
    'binarySha256': sha256(runner.DLL), 'probeSha256': sha256(probe),
})
print('OUTPUT', output, flush=True)
monitor_thread = threading.Thread(target=monitor, daemon=True)
monitor_thread.start()
started = time.monotonic()
try:
    runner.run(runner.parser().parse_args([
        '--scenario', 'SuccessDisaster', '--workload', 'Cpu', '--strategy', 'Cinnamon',
        '--rates', '22', '--duration-seconds', '60', '--repetitions', str(diagnostic_options.repetitions),
        '--warmup-requests', '20', '--skip-build', '--analyze',
        '--output-directory', str(output / 'experiment')]))
    write_json(output / 'diagnostic-completed.json', {'elapsedSeconds': time.monotonic()-started})
finally:
    for thread in injection_threads:
        thread.join()
    stop_monitor.set()
    monitor_thread.join(timeout=10)
print('COMPLETE', output, flush=True)
