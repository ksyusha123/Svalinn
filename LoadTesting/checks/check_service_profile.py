"""Check the synthetic dependency cost, including rejection and restoration."""

import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runner_common import (
    DLL,
    LOAD_TESTING,
    ROOT,
    Child,
    ensure_port_free,
    get_json,
    stamp,
    wait_ready,
)


def main():
    port = 5091
    base = f"http://127.0.0.1:{port}"
    output = LOAD_TESTING / "results" / ("service-profile-check-" + stamp())
    output.mkdir(parents=True)
    opener = build_opener(ProxyHandler({}))

    def call(value):
        req = Request(
            base + "/benchmark/io/critical", headers={"X-Benchmark-Service-Ms": value}
        )
        try:
            response = opener.open(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            import json

            return response.status, dict(response.headers), json.loads(response.read())

    ensure_port_free(port)
    with Child(
        [
            "dotnet",
            DLL,
            "--urls",
            base,
            "--CpuBenchmark:Enabled=true",
            "--CpuBenchmark:Iterations=1000",
            "--CpuBenchmark:AffinityMask=1",
            "--Svalinn:Enabled=false",
            "--IoBenchmark:ServiceMs=10",
            "--IoBenchmark:ServiceJitterMs=0",
            "--Logging:LogLevel:Default=Error",
        ],
        output,
        "override",
        ROOT / "TestApp",
    ) as server:
        wait_ready(server, lambda: get_json(base + "/benchmark/cpu/info"))
        status, headers, body = call("30")
        expected = 30
        assert status == 200 and body["serviceMs"] == expected, body
        # Windows timer granularity permits an async delay to finish slightly early.
        assert float(headers["X-Benchmark-Io-Service-Ms"]) >= max(
            0, expected - 20
        ), headers
        for value in ("0", "10001", "bad"):
            assert call(value)[0] == 400, value
        assert call("10")[2]["serviceMs"] == 10
        assert get_json(base + "/benchmark/io/critical")["serviceMs"] == 10
        assert get_json(base + "/benchmark/telemetry")["active"] == 0
    ensure_port_free(port)
    with Child(
        [
            "dotnet", DLL, "--urls", base,
            "--CpuBenchmark:Enabled=true",
            "--CpuBenchmark:Iterations=1000",
            "--CpuBenchmark:AffinityMask=1",
            "--Svalinn:Enabled=false",
            "--Logging:LogLevel:Default=Error",
        ],
        output, "randomized", ROOT / "TestApp",
    ) as server:
        wait_ready(server, lambda: get_json(base + "/benchmark/cpu/info"))
        samples = []
        for priority in ("critical", "high", "normal", "low"):
            for _ in range(8):
                body = get_json(base + "/benchmark/io/" + priority)
                samples.append(body["serviceMs"])
        assert all(100 <= ms <= 200 for ms in samples), samples
        assert len(set(samples)) > 1, samples
        print(f"PASS: all I/O endpoints use random 100..200 ms delays; {min(samples)}..{max(samples)} observed")
    print("PASS: dependency service override is bounded, measured and restored")


if __name__ == "__main__":
    main()
