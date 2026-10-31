"""HTTP smoke check for Cinnamon admission, queue limits, deadlines and metrics."""
import argparse
import concurrent.futures
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5087)
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(route):
        try:
            response = opener.open(base + route, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, dict(response.headers), response.read().decode()

    def await_metric(name, expected):
        for _ in range(100):
            if name + '{group="default"} ' + str(expected) + '\n' in request('/metrics')[2]:
                return
            time.sleep(.01)
        raise AssertionError(f"Metric {name} did not reach {expected}")

    # Refuse to touch a server already bound to the chosen port.
    import socket
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", args.port)) == 0:
            raise RuntimeError("Port is already occupied")
    output = ROOT / "LoadTesting/results" / f"cinnamon-http-check-{time.time_ns()}"
    output.mkdir(parents=True)
    with (output / "server.log").open("w", encoding="utf-8") as log:
        server = subprocess.Popen([
            "dotnet", str(ROOT / "TestApp/bin/Release/net10.0/TestApp.dll"), '--urls', base,
            '--Svalinn:Enabled=true', '--Svalinn:Strategy=Cinnamon',
            '--Svalinn:Cinnamon:InitialLimit=1', '--Svalinn:Cinnamon:MinLimit=1', '--Svalinn:Cinnamon:MaxLimit=1',
            '--Svalinn:Cinnamon:EnablePi=false', '--Svalinn:Cinnamon:EnableAutoTuner=false',
            '--Svalinn:Cinnamon:QueueCapacity=1', '--Svalinn:Cinnamon:QueueTimeoutMs=100',
            '--DelayBenchmark:CriticalDelayMs=750', '--Logging:LogLevel:Default=Error',
        ], cwd=ROOT / 'TestApp', stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            for _ in range(100):
                try:
                    if request('/metrics')[0] == 200:
                        break
                except OSError:
                    if server.poll() is not None:
                        raise RuntimeError("Test server exited")
                    time.sleep(.1)
            else:
                raise RuntimeError("Test server did not become ready")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                running = pool.submit(request, '/benchmark/delay/critical')
                await_metric('svalinn_cinnamon_inflight', 1)
                waiting = pool.submit(request, '/benchmark/delay/low')
                await_metric('svalinn_cinnamon_queued', 1)
                full = request('/benchmark/delay/high')
                assert full[0] == 503 and 'full' in full[2] and full[1].get('Retry-After'), full
                assert request('/metrics')[0] == 200
                timeout = waiting.result()
                assert timeout[0] == 503 and 'timeout' in timeout[2] and timeout[1].get('Retry-After'), timeout
                assert running.result()[0] == 200
            await_metric('svalinn_cinnamon_inflight', 0)
            await_metric('svalinn_cinnamon_queued', 0)
            await_metric('svalinn_cinnamon_queue_full_total', 1)
            await_metric('svalinn_cinnamon_queue_timeout_total', 1)
            (output / 'metrics.txt').write_text(request('/metrics')[2], encoding='utf-8')
            print('PASS HTTP: Cinnamon queue capacity, timeout, Retry-After, metrics, permit cleanup')
            print(output)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == '__main__':
    main()
