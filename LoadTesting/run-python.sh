#!/usr/bin/env bash
set -euo pipefail
load_testing="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python="$load_testing/plotting/.venv/Scripts/python.exe"
if [[ ! -x "$python" ]]; then
  echo "Нет Python-окружения. Выполните: uv sync --project LoadTesting/plotting --locked --no-install-project" >&2
  exit 1
fi
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
exec "$python" "$@"
