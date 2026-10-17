param([int]$Port = 5028)
$ErrorActionPreference = 'Stop'
$loadTesting = Split-Path $PSScriptRoot -Parent
$root = Split-Path $loadTesting -Parent
$output = Join-Path $loadTesting ('results/io-check-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $output | Out-Null
$dll = Join-Path $root 'TestApp/bin/Release/net10.0/TestApp.dll'
$url = "http://127.0.0.1:$Port"
$server = Start-Process (Get-Command dotnet).Source -WindowStyle Hidden -PassThru -WorkingDirectory (Join-Path $root 'TestApp') `
    -ArgumentList @('"' + $dll + '"', '--urls', $url, '--CpuBenchmark:Enabled=true', '--CpuBenchmark:AffinityMask=1',
        '--IoBenchmark:Slots=1', '--IoBenchmark:ServiceMs=1000', '--Svalinn:Enabled=true', '--Svalinn:MaxConcurrentRequests=0',
        '--Logging:LogLevel:Default=Error') `
    -RedirectStandardOutput (Join-Path $output 'server.log') -RedirectStandardError (Join-Path $output 'server.err')
$client = [Net.Http.HttpClient]::new()
try {
    for ($i = 0; $i -lt 50; $i++) {
        if ($server.HasExited) { throw 'Сервер завершился до проверки.' }
        try { $state = Invoke-RestMethod "$url/benchmark/telemetry" -TimeoutSec 1; break }
        catch { if ($i -eq 49) { throw }; Start-Sleep -Milliseconds 100 }
    }
    foreach ($priority in @('critical', 'high', 'normal', 'low')) {
        $response = $client.GetAsync("$url/benchmark/io/$priority").GetAwaiter().GetResult()
        $expected = if ($priority -in @('critical', 'high')) { 200 } else { 503 }
        if ([int]$response.StatusCode -ne $expected) { throw "Неверный допуск I/O: $priority" }
        $response.Dispose()
    }
    function Wait-State([scriptblock]$Condition, [string]$Failure) {
        $watch = [Diagnostics.Stopwatch]::StartNew()
        do {
            $snapshot = Invoke-RestMethod "$url/benchmark/telemetry" -TimeoutSec 2
            if ($snapshot.active -lt 0 -or $snapshot.active -gt $snapshot.capacity -or $snapshot.waiting -lt 0) {
                throw 'Некорректные значения счётчиков I/O.'
            }
            if (& $Condition $snapshot) { return $snapshot }
            Start-Sleep -Milliseconds 10
        } while ($watch.Elapsed.TotalSeconds -lt 5)
        throw $Failure
    }

    # Конкурентные запросы всех приоритетов используют один пул. В этом режиме
    # допущены Critical и High; Normal/Low уже проверены выше на отклонение.
    $before = Invoke-RestMethod "$url/benchmark/telemetry"
    $tasks = @('critical', 'high', 'critical', 'high' | ForEach-Object {
        $client.GetAsync("$url/benchmark/io/$_")
    })
    $state = Wait-State { param($s) $s.active -eq 1 -and $s.waiting -ge 2 } 'Не обнаружена очередь общего пула.'
    while (@($tasks | Where-Object { !$_.IsCompleted }).Count) {
        $state = Invoke-RestMethod "$url/benchmark/telemetry"
        if ($state.active -lt 0 -or $state.active -gt 1 -or $state.waiting -lt 0) {
            throw 'Нарушено ограничение конкурентности.'
        }
        Start-Sleep -Milliseconds 10
    }
    foreach ($task in $tasks) {
        $response = $task.GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 200) { throw 'Ошибка конкурентного запроса.' }
        $response.Dispose()
    }
    $state = Wait-State { param($s) $s.active -eq 0 -and $s.waiting -eq 0 } 'Очередь не освободилась.'
    if ($state.completed - $before.completed -ne 4) { throw 'Потеряны завершённые обслуживания.' }

    # Отменяем запрос в очереди, пока другое обслуживание удерживает место.
    # Затем отменяем само обслуживание. Проверяем последствия, а не счётчики отмен.
    $completedBeforeCancellation = $state.completed
    $serviceCancellation = [Threading.CancellationTokenSource]::new()
    $queueCancellation = [Threading.CancellationTokenSource]::new()
    try {
        $serviceTask = $client.GetAsync("$url/benchmark/io/critical", $serviceCancellation.Token)
        $state = Wait-State { param($s) $s.active -eq 1 } 'Обслуживание не началось.'
        $queueTask = $client.GetAsync("$url/benchmark/io/critical", $queueCancellation.Token)
        $state = Wait-State { param($s) $s.active -eq 1 -and $s.waiting -eq 1 } 'Второй запрос не попал в очередь.'
        $queueCancellation.Cancel()
        try { $queueTask.GetAwaiter().GetResult().Dispose() } catch { }
        if (!$queueTask.IsCanceled) { throw 'Ожидающий запрос не был отменён.' }
        $state = Wait-State { param($s) $s.waiting -eq 0 } 'Отменённый запрос остался в очереди.'
        if ($state.active -ne 1 -or $state.completed -ne $completedBeforeCancellation) {
            throw 'Отмена очереди изменила занятое место или обслуживание закончилось до проверки.'
        }
        $serviceCancellation.Cancel()
        try { $serviceTask.GetAwaiter().GetResult().Dispose() } catch { }
        if (!$serviceTask.IsCanceled) { throw 'Обслуживаемый запрос не был отменён.' }
        $state = Wait-State { param($s) $s.active -eq 0 -and $s.waiting -eq 0 } 'Отмена не освободила I/O-ресурс.'
        if ($state.completed -ne $completedBeforeCancellation) { throw 'Отменённое обслуживание посчитано завершённым.' }
    } finally {
        $queueCancellation.Cancel(); $serviceCancellation.Cancel()
        $queueCancellation.Dispose(); $serviceCancellation.Dispose()
    }
    $response = $client.GetAsync("$url/benchmark/io/critical").GetAwaiter().GetResult()
    if ([int]$response.StatusCode -ne 200 -or !$response.Headers.Contains('X-Benchmark-Io-Wait-Ms')) {
        throw 'После отмены ресурс недоступен либо отсутствуют метрики ожидания.'
    }
    $response.Dispose()
    $state = Wait-State { param($s) $s.active -eq 0 -and $s.waiting -eq 0 } 'Ресурс не освободился после повторного использования.'
    if ($state.completed -ne $completedBeforeCancellation + 1) { throw 'Неверное число завершений после отмены.' }
    Write-Host 'ПРОВЕРКА ПРОЙДЕНА: приоритеты I/O, ограничение конкурентности, очередь, отмена и повторное использование места.'
} finally {
    $client.Dispose()
    if (!$server.HasExited) { Stop-Process -Id $server.Id }
    $server.WaitForExit()
}
