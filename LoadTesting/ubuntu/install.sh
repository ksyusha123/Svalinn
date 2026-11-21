#!/usr/bin/env bash
set -euo pipefail
source /etc/os-release
if [[ "$ID" != ubuntu || "$VERSION_ID" != 24.04 || "$(uname -m)" != x86_64 ]]; then
  echo 'Этот установщик предназначен для Ubuntu 24.04 x86_64.' >&2
  exit 1
fi
if [[ "$EUID" == 0 ]]; then
  echo 'Запускайте от обычного пользователя; sudo используется только для apt.' >&2
  exit 1
fi
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git unzip tmux util-linux software-properties-common
# .NET 10 for Ubuntu 24.04 is supplied by the Ubuntu feed, not packages.microsoft.com.
sudo add-apt-repository -y universe
sudo apt-get update
sudo apt-get install -y dotnet-sdk-10.0
sudo install -d -m 0755 /etc/apt/keyrings
key_file="$(mktemp)"
trap 'rm -f -- "$key_file"' EXIT
curl -fsSL https://dl.k6.io/key.gpg -o "$key_file"
sudo gpg --batch --yes --dearmor -o /etc/apt/keyrings/svalinn-k6.gpg "$key_file"
echo 'deb [signed-by=/etc/apt/keyrings/svalinn-k6.gpg] https://dl.k6.io/deb stable main' |
  sudo tee /etc/apt/sources.list.d/k6.list >/dev/null
sudo apt-get update
sudo apt-get install -y k6
if ! command -v uv >/dev/null && [[ ! -x "$HOME/.local/bin/uv" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh -o "$key_file"
  sh "$key_file"
fi
export PATH="$HOME/.local/bin:$PATH"
uv python install 3.14
dotnet --list-sdks
k6 version
uv --version
echo 'Установка завершена. Далее: bash LoadTesting/ubuntu/run.sh prepare'
