param([int]$Port = 5027, [long]$AffinityMask = 1)
$ErrorActionPreference = 'Stop'
$loadTesting = Split-Path $PSScriptRoot -Parent
$root = Split-Path $loadTesting -Parent
$dll = Join-Path $root 'TestApp/bin/Release/net10.0/TestApp.dll'
$output = Join-Path $loadTesting ('results/smoke-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $output | Out-Null
$baseUrl = "http://127.0.0.1:$Port"
$dotnet = (Get-Command dotnet).Source

function Check-Mode([string]$Mode, [bool]$Cpu, [bool]$Svalinn) {
    $args = @('"' + $dll + '"', '--urls', $baseUrl,
        "--CpuBenchmark:Enabled=$Cpu", "--CpuBenchmark:AffinityMask=$AffinityMask",
        '--CpuBenchmark:Iterations=1000', "--Svalinn:Enabled=$Svalinn",
        # Принудительно включаем перегрузку даже для одиночного запроса, чтобы проверить метаданные приоритета.
        '--Svalinn:MaxConcurrentRequests=0', '--Svalinn:MinimumPriorityWhenOverloaded=High',
        '--Svalinn:AlwaysAllowCriticalRequests=true', '--Logging:LogLevel:Default=Error')
    $server = Start-Process $dotnet -ArgumentList $args -WorkingDirectory (Join-Path $root 'TestApp') `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $output "$Mode.log") `
        -RedirectStandardError (Join-Path $output "$Mode.err")
    try {
        for ($attempt = 0; $attempt -lt 50; $attempt++) {
            if ($server.HasExited) { throw "Сервер завершился в режиме $Mode" }
            try {
                $ready = Invoke-WebRequest "$baseUrl/benchmark/cpu/info" -SkipHttpErrorCheck -TimeoutSec 1
                if ($Cpu -and ($ready.Content | ConvertFrom-Json).processId -ne $server.Id) { throw 'Порт обслуживается другим процессом.' }
                break
            } catch { if ($attempt -eq 49) { throw }; Start-Sleep -Milliseconds 100 }
        }
        $checksums = @()
        foreach ($priority in @('critical', 'high', 'normal', 'low')) {
            $response = Invoke-WebRequest "$baseUrl/benchmark/cpu/$priority" -SkipHttpErrorCheck -TimeoutSec 5
            $expected = if (!$Cpu) { 404 } elseif ($Svalinn -and $priority -in @('normal', 'low')) { 503 } else { 200 }
            if ($response.StatusCode -ne $expected) { throw "$Mode/$priority — ожидался код $expected, получен $($response.StatusCode)" }
            if ($expected -eq 200) { $checksums += ($response.Content | ConvertFrom-Json).checksum }
            if ($expected -eq 503 -and !$response.Headers.ContainsKey('Retry-After')) { throw 'Отсутствует заголовок Retry-After' }
        }
        if ($Cpu -and @($checksums | Select-Object -Unique).Count -ne 1) { throw 'Контрольные суммы вычислений различаются.' }
        Write-Host "ПРОВЕРКА ПРОЙДЕНА: $Mode"
    } finally { if (!$server.HasExited) { Stop-Process -Id $server.Id }; $server.WaitForExit() }
}

Check-Mode 'cpu-disabled' $false $false
Check-Mode 'svalinn-off' $true $false
Check-Mode 'svalinn-on-forced-overload' $true $true
