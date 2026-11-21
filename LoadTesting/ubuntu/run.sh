#!/usr/bin/env bash
set -euo pipefail
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo"
export PATH="$HOME/.local/bin:$PATH"
export PYTHONUTF8=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg
export DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 DOTNET_CLI_DO_NOT_USE_MSBUILD_SERVER=1
export MSBUILDDISABLENODEREUSE=1
stage="${1:-help}"
if [[ $# -gt 0 ]]; then shift; fi
python="$repo/LoadTesting/plotting/.venv/bin/python"
if [[ "$stage" == prepare ]]; then
  uv sync --project LoadTesting/plotting --python 3.14 --locked --no-install-project
  dotnet restore TestApp/TestApp.csproj --configfile LoadTesting/ubuntu/nuget-public.config -p:IncludeApiDocs=false --disable-parallel
  dotnet restore Svalinn.Tests/Svalinn.Tests.csproj --configfile LoadTesting/ubuntu/nuget-public.config --disable-parallel
  dotnet build TestApp/TestApp.csproj -c Release --no-restore -m:1 -p:IncludeApiDocs=false
  dotnet build Svalinn.Tests/Svalinn.Tests.csproj -c Release --no-restore -m:1
  dotnet build-server shutdown
  "$python" -c 'import io; import matplotlib.pyplot as plt; fig, ax = plt.subplots(); ax.plot([0, 1], [0, 1]); fig.savefig(io.BytesIO(), format="png"); plt.close(fig); print("PNG rendering: OK")'
  dotnet run --project Svalinn.Tests -c Release --no-build --no-restore
  "$python" -m unittest discover -s LoadTesting/tests -v
  echo 'Подготовка завершена. Далее: bash LoadTesting/ubuntu/run.sh smoke --output-directory LoadTesting/results/ubuntu-smoke'
  exit
fi
if [[ "$stage" == help ]]; then
  echo 'Использование: bash LoadTesting/ubuntu/run.sh prepare|smoke|baseline|matrix|analyze [параметры]'
  echo 'Подробности: LoadTesting/ubuntu/README.md'
  exit
fi
if [[ ! -x "$python" ]]; then
  echo 'Сначала выполните bash LoadTesting/ubuntu/run.sh prepare' >&2
  exit 1
fi
# This changes only this shell and its children, not global machine settings.
if (( $(ulimit -n) < 4096 )); then ulimit -n 4096; fi
exec "$python" LoadTesting/ubuntu/study.py "$stage" "$@"
