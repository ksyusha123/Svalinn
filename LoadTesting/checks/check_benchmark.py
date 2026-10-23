"""Endpoint checks, including I/O queue cancellation, runnable from Git Bash."""

import argparse
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner_common import (
    DLL,
    LOAD_TESTING,
    PRIORITIES,
    ROOT,
    Child,
    check_masks,
    ensure_port_free,
    executable,
    get_json,
    main_guard,
    request,
    stamp,
    wait_ready,
)


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def start(a, output, name, enabled, cpu=True):
    ensure_port_free(a.port)
    url = f"http://127.0.0.1:{a.port}"
    config = {
        "CpuBenchmark:Enabled": str(cpu).lower(),
        "CpuBenchmark:AffinityMask": a.affinity_mask,
        "CpuBenchmark:Iterations": 1000,
        "Svalinn:Enabled": str(enabled).lower(),
        "Svalinn:Strategy": "Inflight",
        "Svalinn:MaxConcurrentRequests": 0,
        "Svalinn:MinimumPriorityWhenOverloaded": "High",
        "Svalinn:AlwaysAllowCriticalRequests": "true",
        "IoBenchmark:Slots": 1,
        "IoBenchmark:ServiceMs": 1000,
        "Logging:LogLevel:Default": "Error",
    }
    child = Child(
        [executable("dotnet"), DLL, "--urls", url]
        + [f"--{k}={v}" for k, v in config.items()],
        output,
        name,
        ROOT / "TestApp",
    )

    def probe():
        if enabled and not cpu:
            request(url + "/", 1)
            require(
                any(
                    c.status == "LISTEN" and c.laddr.port == a.port
                    for c in child.stats.net_connections(kind="tcp")
                ),
                "Созданный сервер ещё не слушает порт.",
            )
            return
        info = get_json(
            url + ("/benchmark/cpu/info" if cpu else "/benchmark/delay/info"), 1
        )
        require(
            info["processId"] == child.process.pid,
            "Порт обслуживается другим процессом.",
        )

    try:
        wait_ready(child, probe)
    except BaseException:
        child.close()
        raise
    return child


def priority_checks(url, kind, enabled, disabled=False):
    checksums = []
    for priority in PRIORITIES:
        status, headers, body = request(f"{url}/benchmark/{kind}/{priority}", 10)
        expected = (
            404
            if disabled
            else 503
            if enabled and priority in ("normal", "low")
            else 200
        )
        require(
            status == expected,
            f"{kind}/{priority}: ожидался {expected}, получен {status}",
        )
        if expected == 503:
            require("retry-after" in {k.lower() for k in headers}, "Нет Retry-After.")
        if expected == 200:
            if kind == "cpu":
                checksums.append(body["checksum"])
            else:
                require(body["resource"] == kind, "Неверный формат ответа.")
    if checksums:
        require(len(set(checksums)) == 1, "Контрольные суммы вычислений различаются.")


def io_checks(url, port):
    def state():
        value = get_json(url + "/benchmark/telemetry", 2)
        require(
            0 <= value["active"] <= 1 and value["waiting"] >= 0,
            "Нарушены ограничения счётчиков I/O.",
        )
        return value

    def wait(condition, message):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = state()
            if condition(value):
                return value
            time.sleep(0.01)
        raise RuntimeError(message)

    before = state()
    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = [
            pool.submit(request, url + "/benchmark/io/" + p, 10)
            for p in ("critical", "high", "critical", "high")
        ]
        wait(
            lambda s: s["active"] == 1 and s["waiting"] >= 2,
            "Не обнаружена очередь общего пула.",
        )
        while not all(t.done() for t in tasks):
            state()
            time.sleep(0.01)
        require(
            all(t.result()[0] == 200 for t in tasks), "Ошибка конкурентного запроса."
        )
    value = wait(
        lambda s: s["active"] == 0 and s["waiting"] == 0, "Очередь не освободилась."
    )
    require(
        value["completed"] - before["completed"] == 4,
        "Потеряны завершённые обслуживания.",
    )
    completed = value["completed"]

    # Closing the TCP connection cancels RequestAborted on the server, unlike cancelling a Python Future.
    def begin():
        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        sock.sendall(
            b"GET /benchmark/io/critical HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        )
        return sock

    def cancel(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    service = begin()
    queued = None
    try:
        wait(lambda s: s["active"] == 1, "Обслуживание не началось.")
        queued = begin()
        wait(
            lambda s: s["active"] == 1 and s["waiting"] == 1,
            "Второй запрос не попал в очередь.",
        )
        cancel(queued)
        queued = None
        value = wait(
            lambda s: s["waiting"] == 0, "Отменённый запрос остался в очереди."
        )
        require(
            value["active"] == 1 and value["completed"] == completed,
            "Отмена очереди повлияла на обслуживание.",
        )
        cancel(service)
        value = wait(
            lambda s: s["active"] == 0 and s["waiting"] == 0,
            "Отмена не освободила I/O-ресурс.",
        )
        require(
            value["completed"] == completed,
            "Отменённое обслуживание посчитано завершённым.",
        )
    finally:
        cancel(service)
        if queued is not None:
            cancel(queued)
    status, headers, _ = request(url + "/benchmark/io/critical", 5)
    require(
        status == 200 and "x-benchmark-io-wait-ms" in {k.lower() for k in headers},
        "Ресурс недоступен после отмены.",
    )
    value = wait(
        lambda s: s["active"] == 0 and s["waiting"] == 0, "Ресурс не освободился."
    )
    require(
        value["completed"] == completed + 1, "Неверное число завершений после отмены."
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["cpu", "io", "delay"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--affinity-mask", type=int, default=1)
    a = parser.parse_args()
    a.port = a.port or {"cpu": 5027, "io": 5028, "delay": 5029}[a.kind]
    check_masks(a.affinity_mask)
    require(DLL.exists(), "Сначала выполните Release-сборку TestApp.")
    output = LOAD_TESTING / "results" / (a.kind + "-check-" + stamp())
    output.mkdir(parents=True)
    url = f"http://127.0.0.1:{a.port}"
    if a.kind == "cpu":
        for name, cpu, enabled in [
            ("cpu-disabled", False, False),
            ("svalinn-off", True, False),
            ("svalinn-on-forced-overload", True, True),
        ]:
            with start(a, output, name, enabled, cpu):
                priority_checks(url, "cpu", enabled, not cpu)
            print(f"ПРОВЕРКА ПРОЙДЕНА: {name}", flush=True)
    elif a.kind == "delay":
        for enabled in (False, True):
            with start(a, output, str(enabled), enabled, False):
                if not enabled:
                    info = get_json(url + "/benchmark/delay/info")
                    require(
                        [e["delayMs"] for e in info["endpoints"]]
                        == [800, 1600, 800, 3000],
                        "Неверные задержки.",
                    )
                    require(
                        [e["priority"] for e in info["endpoints"]]
                        == ["Critical", "High", "Normal", "Low"],
                        "Неверные приоритеты.",
                    )
                    require(
                        get_json(url + "/benchmark/delay/slo")["targets"][
                            "criticalP95LatencyMs"
                        ]
                        == 1200,
                        "Неверный SLO.",
                    )
                priority_checks(url, "delay", enabled)
            print(f"ПРОВЕРКА ПРОЙДЕНА: delay, Svalinn={enabled}", flush=True)
    else:
        with start(a, output, "server", True):
            priority_checks(url, "io", True)
            io_checks(url, a.port)
        print(
            "ПРОВЕРКА ПРОЙДЕНА: приоритеты I/O, очередь, отмена и повторное использование места.",
            flush=True,
        )


if __name__ == "__main__":
    main_guard(main)
