#!/usr/bin/env bash
set -euo pipefail
checks="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$checks/../run-python.sh" "$checks/check_benchmark.py" io "$@"
