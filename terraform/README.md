# ВМ Svalinn в Yandex Cloud

Конфигурация создаёт ВМ Ubuntu 24.04: 2 vCPU, 2 ГБ RAM, SSD 10 ГБ,
публичный IPv4 и пользователя из переменной `ssh_user` с указанным SSH-ключом.
Используется существующая подсеть `ajcr3der5jfv8j91ae3e` (`default-ru-central1-e`)
в зоне `ru-central1-e`.

## Подготовка

Установите [Terraform](https://developer.hashicorp.com/terraform/install)
версии 1.5 или новее и [Yandex Cloud CLI](https://yandex.cloud/ru/docs/cli/quickstart).
Если профиль CLI ещё не настроен, выполните `yc init` под учётной записью
с правами на создание ВМ в каталоге `b1g1m90mm920k570odpp` и использование подсети.

## Создание ВМ (PowerShell)

Перед запуском создайте локальный файл `terraform/secrets.auto.tfvars`:

```hcl
ssh_public_key = "<публичный SSH-ключ>"
ssh_user       = "<SSH-пользователь>"
```

Terraform автоматически загружает этот файл; он исключён из Git правилом
`terraform/*.tfvars`. Обе переменные помечены `sensitive`, чтобы скрывать ключ и имя пользователя
в выводе Terraform. Вместо файла можно передать ключ через переменную окружения
`TF_VAR_ssh_public_key` и `TF_VAR_ssh_user` из хранилища секретов.
Ключ по-прежнему сохраняется в локальном state-файле: храните его защищённо.

Из корня проекта:

```powershell
Set-Location terraform
$env:YC_TOKEN = yc iam create-token
terraform init
terraform fmt -check
terraform validate
terraform plan -out=svalinn.tfplan
terraform apply svalinn.tfplan
```

Проверьте план перед `apply`: для новой установки он должен создавать одну ВМ.
`apply` создаёт платные облачные ресурсы. IAM-токен действует не более 12 часов;
при истечении повторите команду получения `YC_TOKEN`.
Аутентификация описана в [документации Yandex Cloud](https://yandex.cloud/ru/docs/terraform/authentication).

После первого `init` добавьте `.terraform.lock.hcl` в Git, чтобы закрепить
выбранную версию провайдера. Локальные state-файлы, планы и каталог провайдера
исключены из Git. Сохраняйте `terraform.tfstate`: он нужен для дальнейшего управления ВМ.

## Подключение

Для установки .NET SDK 10 на созданную ВМ используйте
[Ansible playbook](../ansible/README.md).

```powershell
terraform output -raw ssh_command
ssh <ssh_user>@<public_ip>
```

Адрес показан в output `public_ip`. Если приватный ключ лежит вне стандартного
расположения SSH, используйте `ssh -i <путь_к_приватному_ключу> <ssh_user>@<public_ip>`.

## Если ВМ уже существует

Перед первым планированием импортируйте её вместо повторного создания:

```powershell
terraform import yandex_compute_instance.svalinn <ID_существующей_ВМ>
terraform plan
```

Проверьте изменения после импорта, особенно возможную замену ВМ или диска,
до выполнения `apply`. Для управления ресурсом нужен его ID, а не имя.

## Удаление

```powershell
terraform destroy
```

Команда удалит управляемую ВМ вместе с загрузочным диском (`auto_delete = true`).
