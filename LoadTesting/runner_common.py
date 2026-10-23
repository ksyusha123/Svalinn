"""Shared Windows process and artifact support for the Git Bash runners."""

import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

import psutil

LOAD_TESTING = Path(__file__).resolve().parent
ROOT = LOAD_TESTING.parent
DLL = ROOT / "TestApp/bin/Release/net10.0/TestApp.dll"
PRIORITIES = ("critical", "high", "normal", "low")


def stamp():
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")


def utc():
    return datetime.now(UTC).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest().upper()


def executable(name):
    result = shutil.which(str(name))
    if not result:
        raise RuntimeError(f"Не найдена команда {name}. Проверьте PATH.")
    return result


def command(args, capture=False):
    return subprocess.run(
        [str(a) for a in args],
        cwd=ROOT,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE if capture else None,
    ).stdout


def build(no_restore=False):
    args = [
        executable("dotnet"),
        "build",
        ROOT / "TestApp/TestApp.csproj",
        "-c",
        "Release",
        "-p:IncludeApiDocs=false",
    ]
    if no_restore:
        args.append("--no-restore")
    command(args)


def request(url, timeout=5):
    # Local benchmark traffic must not go through a user's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(url, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        body = response.read()
        try:
            decoded = json.loads(body) if body else None
        except (ValueError, UnicodeDecodeError):
            decoded = body.decode("utf-8", errors="replace")
        return response.status, dict(response.headers), decoded


def get_json(url, timeout=5):
    status, _, body = request(url, timeout)
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {url}")
    return body


def cpus(mask):
    if mask <= 0:
        raise ValueError("Маска CPU должна быть положительной.")
    return [i for i in range(mask.bit_length()) if mask & (1 << i)]


def check_masks(server, generator=None):
    if os.name != "nt":
        raise RuntimeError("Этот runner предназначен для Git Bash в Windows.")
    available = set(psutil.Process().cpu_affinity())
    if len(cpus(server)) != 1 or not set(cpus(server)) <= available:
        raise ValueError("Маска сервера должна выбирать один доступный логический CPU.")
    if generator is not None and (
        server & generator or not set(cpus(generator)) <= available
    ):
        raise ValueError(
            "Маска генератора должна выбирать доступные CPU и не пересекаться с сервером."
        )


def ensure_port_free(port):
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", port)) == 0:
            raise RuntimeError(f"Порт {port} уже занят другим процессом.")


@contextlib.contextmanager
def directory_lock(directory):
    import msvcrt

    path = Path(directory) / "running.lock"
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise RuntimeError(
                f"Каталог уже используется другим запуском: {directory}"
            ) from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


class Child:
    def __init__(self, args, directory, name, cwd=ROOT, affinity=None, env=None):
        self.logs = []
        self.process = None
        try:
            self.logs = [
                (Path(directory) / (name + suffix)).open("wb")
                for suffix in (".log", ".err")
            ]
            self.process = subprocess.Popen(
                [str(a) for a in args],
                cwd=cwd,
                env=env,
                stdout=self.logs[0],
                stderr=self.logs[1],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.stats = psutil.Process(self.process.pid)
            if affinity is not None:
                self.stats.cpu_affinity(cpus(affinity))
                if set(self.stats.cpu_affinity()) != set(cpus(affinity)):
                    raise RuntimeError("Не удалось применить привязку процесса к CPU.")
        except BaseException:
            self.close()
            raise

    def cpu_time(self):
        times = self.stats.cpu_times()
        return times.user + times.system

    def close(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in self.logs:
            stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def wait_ready(child, probe, attempts=100):
    for attempt in range(attempts):
        if child.process.poll() is not None:
            raise RuntimeError("Сервер завершился. Проверьте файл server.err.")
        try:
            return probe()
        except (OSError, ValueError, RuntimeError):
            if attempt == attempts - 1:
                raise
            time.sleep(0.1)


def main_guard(main):
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nОстановлено пользователем. Результаты сохранены; для продолжения используйте --resume.",
            file=sys.stderr,
        )
        raise SystemExit(130)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        raise SystemExit(1)
