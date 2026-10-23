#!/usr/bin/env bash
set -euo pipefail
load_testing="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$load_testing/run-python.sh" "$load_testing/run_confirmatory_experiments.py" "$@"
