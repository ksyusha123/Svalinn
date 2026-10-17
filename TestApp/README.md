# TestApp

Тестовое ASP.NET Core-приложение на .NET 10 для исследования приоритетного сброса
нагрузки в Svalinn. Оно выполняет синтетическую работу трёх видов: независимую
задержку, вычисления и обслуживание через ограниченный I/O-пул. Бизнес-логики,
базы данных и внешних сервисов нет.

[Нагрузочные сценарии и анализ результатов](../LoadTesting/README.md).
Все команды ниже выполняются из корня репозитория в PowerShell.

## Быстрый запуск

Нужен .NET SDK 10. Для CPU/I/O-режима нужна Windows: приложение использует
`ProcessorAffinity`. 

Сборка и запуск delay-стенда без необязательных OpenAPI/Scalar:

```powershell
dotnet build TestApp/TestApp.csproj -c Release -p:IncludeApiDocs=false
dotnet run --project TestApp -c Release --no-build --no-launch-profile -- --urls http://127.0.0.1:5025
```

По умолчанию Svalinn включён, delay-маршруты доступны, CPU/I/O-маршруты отключены.
Примеры отдельных запросов находятся в [TestApp.http](TestApp.http).

Чтобы выключить Svalinn, остановите сервер и запустите:

```powershell
dotnet run --project TestApp -c Release --no-build --no-launch-profile -- --urls http://127.0.0.1:5025 --Svalinn:Enabled=false
```

Для интерфейса документации выполните обычный запуск со сборкой:

```powershell
dotnet run --project TestApp --launch-profile http
```

При `IncludeApiDocs=true` (значение по умолчанию) в Development доступны
`/scalar/v1` и `/openapi/v1.json`. Эти зависимости нужно восстановить через NuGet.
Запуск с `--no-build` использует уже собранный вариант приложения.

## Устройство

| Файл | Назначение |
| --- | --- |
| [Program.cs](Program.cs) | Конфигурация приложения, подключение Svalinn и регистрация стендов |
| [DelayBenchmark.cs](DelayBenchmark.cs) | Независимые задержки для проверок допуска и сброса |
| [CpuBenchmark.cs](CpuBenchmark.cs) | PBKDF2-SHA256, настройка CPU affinity и рабочих потоков |
| [IoBenchmark.cs](IoBenchmark.cs) | Общий семафор, очередь, отмена и телеметрия I/O |
| [appsettings.json](appsettings.json) | Исходные настройки Svalinn и delay-стенда |

Путь рабочего запроса: определение приоритета по метаданным маршрута → сбор числа
активных запросов → решение стратегии Svalinn → обработчик либо `503`.
Принятый запрос учитывается в числе активных до завершения обработчика.

По умолчанию выбрана стратегия `Inflight`, её порог равен 4: во время перегрузки Normal/Low могут получить
`503` с `Retry-After: 2`, а High/Critical допускаются. Это порог начала сброса,
а не жёсткий предел конкурентности. Если защищённые запросы сами перегружают
ресурс, стратегия не гарантирует соблюдения SLO.

## Стратегии Svalinn

`Svalinn:Strategy` выбирает `Inflight` (по умолчанию) или `Cpu`.
`Inflight` использует число активных запросов. `Cpu` использует загрузку CPU
процесса, которую один фоновый сервис измеряет с заданным интервалом. В обработчике
запроса читается опубликованный снимок; отдельного измерения CPU на каждый запрос нет.

Загрузка вычисляется по приращению `Process.TotalProcessorTime`, реальному времени
и доступной CPU-ёмкости процесса. Значения сглаживаются экспоненциально (EWMA).
При достижении верхнего порога включается перегрузка, при снижении до нижнего —
выключается. Между порогами предыдущее состояние сохраняется. Первое показание
инициализирует сглаженное значение без усреднения с искусственным нулём.

| Настройка `Svalinn:Cpu` | По умолчанию | Назначение |
| --- | --- | --- |
| `SampleIntervalMs` | 250 | Интервал измерений, 50–60000 мс |
| `SmoothingFactor` | 0.3 | Вес нового измерения, больше 0 и не больше 1 |
| `OverloadThresholdPercent` | 90 | Вход в перегрузку, по сглаженной загрузке |
| `RecoveryThresholdPercent` | 75 | Выход из перегрузки; должен быть ниже верхнего порога |
| `ProcessorCapacity` | `null` | Явная доступная CPU-ёмкость, допускается положительное дробное значение |

При `ProcessorCapacity=null` используется `Environment.ProcessorCount`, дополнительно
ограниченный маской affinity процесса на Windows. Ёмкость определяется при старте
сборщика. Для дробной CPU-квоты или последующего изменения ограничений задайте её
явно и перезапустите приложение. В runner сервер использует один логический CPU,
поэтому автоматически передаётся `ProcessorCapacity=1`.

До первого показания и при возрасте показания больше `max(2000 мс, 4 × интервал)`
CPU-стратегия временно использует `Inflight` с текущим `MaxConcurrentRequests`.
При свежем показании решение зависит от CPU, а не от количества активных запросов.
При включённой перегрузке обе стратегии используют `MinimumPriorityWhenOverloaded`,
`AlwaysAllowCriticalRequests` и `RetryAfterSeconds`. По умолчанию High/Critical
продолжают проходить, Normal/Low получают 503. CPU-стратегия не ограничивает
нагрузку High/Critical и не обнаруживает насыщение I/O-пула при свободном CPU.

Пример после Release-сборки без API-документации:

```powershell
dotnet run --project TestApp -c Release --no-build --no-launch-profile -- `
  --urls http://127.0.0.1:5026 `
  --CpuBenchmark:Enabled=true --CpuBenchmark:AffinityMask=1 `
  --Svalinn:Strategy=Cpu --Svalinn:Cpu:ProcessorCapacity=1
```

В режиме `Cpu` `/metrics` дополнительно публикует gauges:
`process_cpu_percent`, `process_cpu_smoothed_percent`, `cpu_overloaded`,
`cpu_sample_fresh`, `cpu_processor_capacity`, `cpu_sample_age_ms`
(все имена с префиксом `svalinn_`). До первого измерения есть только признак
свежести 0. Сам `/metrics` остаётся маршрутом Normal и может получить 503.
В режиме `Inflight` фоновое измерение CPU не выполняется.

Проверки расчёта, гистерезиса, допуска и жизненного цикла сборщика запускаются
без дополнительных тестовых пакетов:

```powershell
dotnet run --project Svalinn.Tests -c Release
```

## Маршруты

Все перечисленные маршруты используют GET. Рабочие обработчики при успехе
возвращают `200` и JSON; Svalinn может отклонить запрос до входа в обработчик.

| Маршрут | Назначение и доступность |
| --- | --- |
| `/` | Состояние включения режимов и ссылки на служебные маршруты |
| `/benchmark/delay/{priority}` | Независимый `Task.Delay`; доступен всегда |
| `/benchmark/delay/info` | Список delay-маршрутов, приоритеты и задержки |
| `/benchmark/delay/slo` | Цели delay-сценариев и настройки Svalinn |
| `/benchmark/cpu/{priority}` | Фиксированный объём PBKDF2-SHA256 |
| `/benchmark/cpu/info` | Итерации, потоки, affinity, алгоритм и ID процесса |
| `/benchmark/io/{priority}` | Ожидание места в общем пуле и асинхронное обслуживание |
| `/benchmark/telemetry` | Состояние общего I/O-пула |
| `/metrics` | Счётчики Svalinn в формате Prometheus; только при включённом Svalinn |

`{priority}` — `critical`, `high`, `normal` или `low`.
CPU, I/O и их телеметрия включаются **вместе** через `CpuBenchmark:Enabled=true`.
Отдельного флага включения I/O сейчас нет.

`/benchmark/telemetry` обходит Svalinn и не увеличивает его счётчик активных запросов.
`/benchmark/cpu/info` имеет Critical-приоритет. Остальные служебные маршруты из
таблицы проходят через Svalinn с приоритетом Normal и могут получить `503`.

## Модели работы

### Delay

У каждого запроса собственная асинхронная задержка, общего ограниченного ресурса
нет. Отмена запроса прерывает ожидание. Ответ содержит `resource: "delay"` и `delayMs`.

| Суффикс маршрута | Приоритет | Настройка в `DelayBenchmark` | По умолчанию |
| --- | --- | --- | --- |
| `critical` | Critical | `CriticalDelayMs` | 800 мс |
| `high` | High | `HighDelayMs` | 1600 мс |
| `normal` | Normal | `NormalDelayMs` | 800 мс |
| `low` | Low | `LowDelayMs` | 3000 мс |

Допустимые задержки: 0–30000 мс. В `DelayBenchmark:Slo` хранятся цели этих сценариев:
успешность Critical 99,9%, High 99%; p95 ниже 1200 и 2500 мс соответственно.
Сам сервер не проверяет достижение целей — это делают нагрузочные сценарии.

### CPU

Все приоритеты выполняют одинаковую работу: PBKDF2-SHA256 с фиксированными
входными данными и результатом 32 байта. Ответ содержит `checksum` и `iterations`.
Количество итераций не уменьшается при перегрузке. Отмена проверяется перед
вычислением; начатый синхронный PBKDF2 не прерывается токеном отмены.

### I/O

Все приоритеты используют один `SemaphoreSlim`. По умолчанию одновременно
обслуживаются 4 запроса по 100 мс; остальные асинхронно ждут места. Это модель
ограниченной зависимости, а не реальная БД, сеть или диск. Очередь не имеет
отдельного ограничения длины. Ожидание и обслуживание учитывают отмену;
занятое место освобождается при завершении или отмене обслуживания.

Успешный ответ содержит `resource: "io"`, `serviceMs`, `capacity` и заголовки
`X-Benchmark-Io-Wait-Ms`, `X-Benchmark-Io-Service-Ms`.
Телеметрия содержит `capacity`, `serviceMs` и три счётчика: `active`, `waiting`,
`completed`. Счётчики обновляются через `Interlocked`, без отдельного `lock`.
Поля читаются независимо: снимок приблизительный и может захватить переход запроса
между ожиданием и обслуживанием. `waiting` включает короткий участок получения места,
даже если семафор свободен; после завершения всех запросов `active` и `waiting` равны нулю.
`completed` считает завершённые обслуживания в текущем процессе, а не доставленные
клиенту ответы. Серверные пики и счётчики отмен не собираются. Тайм-ауты и успешность
ответов измеряются на стороне k6; отмена и освобождение места продолжают работать.

Mixed создаёт генератор k6: часть запросов идёт на CPU, часть на I/O.
Отдельного Mixed-маршрута нет.

## Конфигурация ресурсного режима

| Ключ | По умолчанию | Ограничение / смысл |
| --- | --- | --- |
| `CpuBenchmark:Enabled` | `false` | Включает CPU и I/O |
| `CpuBenchmark:Iterations` | `100000` | 1000–10000000 |
| `CpuBenchmark:MinWorkerThreads` | `16` | 1–256; минимум рабочих потоков ThreadPool |
| `CpuBenchmark:AffinityMask` | `0` | При включении требуется положительная маска доступных CPU |
| `IoBenchmark:Slots` | `4` | 1–256 мест общего пула |
| `IoBenchmark:ServiceMs` | `100` | 1–10000 мс обслуживания |

В `appsettings.json` также заданы `Svalinn:Enabled=true`, `MaxConcurrentRequests=4`,
`MinimumPriorityWhenOverloaded=High`, `AlwaysAllowCriticalRequests=true`,
`RetryAfterSeconds=2`.

Настройки можно переопределять аргументами приложения (`--Section:Key=value`)
или переменными окружения (`Section__Key=value`). Например, после Release-сборки:

```powershell
dotnet run --project TestApp -c Release --no-build --no-launch-profile -- `
  --urls http://127.0.0.1:5026 `
  --CpuBenchmark:Enabled=true --CpuBenchmark:AffinityMask=1 `
  --CpuBenchmark:Iterations=100000 --IoBenchmark:Slots=4 --IoBenchmark:ServiceMs=100
```

Маска `1` выбирает логический CPU 0. Она должна соответствовать доступным
процессу CPU. Автоматический запуск с разделением сервера и генератора описан
в [LoadTesting](../LoadTesting/README.md).

## Проверка стенда

После Release-сборки без API-документации, в PowerShell 7:

```powershell
./LoadTesting/checks/check-delay-benchmark.ps1
./LoadTesting/checks/check-cpu-benchmark.ps1
./LoadTesting/checks/check-io-benchmark.ps1
```

Полная delay-серия с автоматическим сравнением OFF/ON:

```powershell
./LoadTesting/run-delay-experiment.ps1 -Rates 5,10,20,40 -SkipBuild -Analyze
```

Задержка Critical по умолчанию 800 мс; её можно изменить параметром
`-CriticalDelayMs` (например, `-CriticalDelayMs 600`). Доля Critical в SuccessDisaster составляет 25%.

Скрипты запускают и останавливают собственные процессы сервера. По умолчанию
используют порты 5029, 5027 и 5028 соответственно; параметр `-Port` меняет порт.
Они проверяют задержки и приоритеты, работу CPU, общую ёмкость I/O, очередь,
отмену и возврат места. Логи сохраняются в `LoadTesting/results/`.
