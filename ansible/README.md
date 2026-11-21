# Настройка зависимостей

`playbook.yml` устанавливает .NET SDK 10, k6, uv и Python 3.14
на существующую Ubuntu 24.04 через SSH. Также устанавливаются системный Python,
venv, GnuPG, Git, unzip и tmux. Python 3.14 устанавливается через uv для
SSH-пользователя; системный Python остаётся доступен Ansible.

В `inventory.ini` указан адрес `153.51.118.242` из Terraform. SSH-пользователь
задаётся через переменную окружения `SVALINN_SSH_USER` (значение `ssh_user`
из Terraform). После пересоздания ВМ проверьте IP из корня проекта в PowerShell:

```powershell
terraform -chdir=terraform output -raw public_ip
```

Если IP изменился, обновите `ansible_host` в `inventory.ini`.
Нужен приватный SSH-ключ, соответствующий публичному ключу из локального `terraform/secrets.auto.tfvars`.

## 1. Подготовить Ansible на Windows

Ansible запускается из Linux/WSL. Для установки WSL с Ubuntu выполните в PowerShell от администратора:

```powershell
wsl --install -d Ubuntu-24.04
```

При необходимости перезагрузите Windows, откройте Ubuntu и завершите создание
локального Linux-пользователя. Далее все команды выполняются **в терминале Ubuntu/WSL
на вашем компьютере**.

```bash
sudo apt update
sudo apt install -y python3-venv openssh-client
cd "/mnt/c/Users/<windows_user>/Desktop/Svalinn/ansible"
python3 -m venv ~/.venvs/svalinn-ansible
source ~/.venvs/svalinn-ansible/bin/activate
python -m pip install -r requirements.txt
ansible --version
```

Виртуальное окружение хранится в файловой системе Linux, вне каталога проекта.

## 2. Подготовить SSH-ключ в WSL

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
cp "/mnt/c/Users/<windows_user>/.ssh/<key_name>" ~/.ssh/svalinn
chmod 600 ~/.ssh/svalinn
read -rp "SSH user: " SVALINN_SSH_USER
export SVALINN_SSH_USER
ssh -i ~/.ssh/svalinn "${SVALINN_SSH_USER}@153.51.118.242"
```

Приватный ключ должен оставаться вне репозитория. При первом подключении проверьте
и подтвердите отпечаток SSH-ключа сервера. После проверки подключения выйдите с ВМ
командой `exit`, чтобы вернуться в WSL.

Для ключа с парольной фразой загрузите его в SSH-agent перед запуском Ansible:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/svalinn
```

## 3. Проверить и установить

В терминале WSL из папки `ansible`, с активным виртуальным окружением:

```bash
ansible-playbook -i inventory.ini playbook.yml --syntax-check
ansible -i inventory.ini svalinn -m ansible.builtin.ping --private-key ~/.ssh/svalinn
ansible-playbook -i inventory.ini playbook.yml --private-key ~/.ssh/svalinn
```

Последняя команда устанавливает инструменты и выводит версии .NET SDK, k6 и Python.
Для k6 используется официальный APT-репозиторий Grafana, для uv — установщик Astral.
После переноса проекта на ВМ установите Python-зависимости и соберите приложение
от обычного SSH-пользователя, из корня проекта:

```bash
bash LoadTesting/ubuntu/run.sh prepare
```

Команда создаёт окружение Python через `uv sync --locked` и запускает проверки.
Повторно запускать `LoadTesting/ubuntu/install.sh` после Ansible не требуется.

Playbook можно запускать повторно: установленные пакеты повторно не устанавливаются.
Используется `state: present`: новые patch-версии уже установленных пакетов
этот playbook принудительно не обновляет. Точные версии пакетов не закреплены;
при первой установке APT выбирает доступные версии линейки 10.0.

При следующем запуске в новом терминале достаточно:

```bash
cd "/mnt/c/Users/<windows_user>/Desktop/Svalinn/ansible"
source ~/.venvs/svalinn-ansible/bin/activate
read -rp "SSH user: " SVALINN_SSH_USER
export SVALINN_SSH_USER
ansible-playbook -i inventory.ini playbook.yml --private-key ~/.ssh/svalinn
```

Если нужно проверить результат вручную на ВМ:

```bash
dotnet --list-sdks
k6 version
uv run --no-project --python 3.14 python --version
```

В выводе должен присутствовать SDK `10.0.x`.
Пересоздавать ВМ или выполнять `terraform apply` для установки зависимостей не нужно.

## Источники

- [Установка .NET на Ubuntu 24.04](https://learn.microsoft.com/en-us/dotnet/core/install/linux-ubuntu-install#ubuntu-2404)
- [Установка Ansible и требования к контроллеру](https://docs.ansible.com/projects/ansible/latest/installation_guide/intro_installation.html)
- [Установка k6](https://grafana.com/docs/k6/latest/set-up/install-k6/)
- [Установщик uv](https://docs.astral.sh/uv/reference/installer/)
