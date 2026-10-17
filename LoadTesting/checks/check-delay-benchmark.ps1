param([int]$Port = 5029)
$ErrorActionPreference = 'Stop'
$loadTesting = Split-Path $PSScriptRoot -Parent
$root = Split-Path $loadTesting -Parent
$output = Join-Path $loadTesting ('results/delay-check-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $output | Out-Null
$dll = Join-Path $root 'TestApp/bin/Release/net10.0/TestApp.dll'
$url = "http://127.0.0.1:$Port"
$client = [Net.Http.HttpClient]::new()
try {
    foreach ($enabled in @($false, $true)) {
        $server = Start-Process (Get-Command dotnet).Source -WindowStyle Hidden -PassThru `
            -WorkingDirectory (Join-Path $root 'TestApp') `
            -ArgumentList @('"' + $dll + '"', '--urls', $url, '--CpuBenchmark:Enabled=false',
                "--Svalinn:Enabled=$enabled", '--Svalinn:MaxConcurrentRequests=0',
                '--Svalinn:MinimumPriorityWhenOverloaded=High', '--Svalinn:AlwaysAllowCriticalRequests=true',
                '--Logging:LogLevel:Default=Error') `
            -RedirectStandardOutput (Join-Path $output "$enabled.log") `
            -RedirectStandardError (Join-Path $output "$enabled.err")
        try {
            for ($attempt = 0; $attempt -lt 50; $attempt++) {
                if ($server.HasExited) { throw 'Сервер завершился до проверки.' }
                try { $response = $client.GetAsync("$url/").GetAwaiter().GetResult(); $response.Dispose(); break }
                catch { if ($attempt -eq 49) { throw }; Start-Sleep -Milliseconds 100 }
            }
            if (!$enabled) {
                $info = Invoke-RestMethod "$url/benchmark/delay/info"
                if (($info.endpoints.delayMs -join ',') -ne '800,1600,800,3000') { throw 'Задержки не соответствуют настройкам по умолчанию.' }
                if (($info.endpoints.priority -join ',') -ne 'Critical,High,Normal,Low') { throw 'Неверные приоритеты в описании.' }
                $slo = Invoke-RestMethod "$url/benchmark/delay/slo"
                if ($slo.targets.criticalP95LatencyMs -ne 1200) { throw 'Неверный SLO delay-стенда.' }
            }
            foreach ($priority in @('critical', 'high', 'normal', 'low')) {
                $response = $client.GetAsync("$url/benchmark/delay/$priority").GetAwaiter().GetResult()
                try {
                    $expected = if ($enabled -and $priority -in @('normal', 'low')) { 503 } else { 200 }
                    if ([int]$response.StatusCode -ne $expected) { throw "Неверный статус для $priority при Svalinn=$enabled" }
                    if ($expected -eq 200) {
                        $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
                        if ($body.resource -ne 'delay') { throw 'Неверный формат ответа.' }
                    } elseif (!$response.Headers.Contains('Retry-After')) { throw 'Нет Retry-After.' }
                } finally { $response.Dispose() }
            }
            Write-Host "ПРОВЕРКА ПРОЙДЕНА: delay, Svalinn=$enabled"
        } finally { if (!$server.HasExited) { Stop-Process -Id $server.Id }; $server.WaitForExit(); $server.Dispose() }
    }
} finally { $client.Dispose() }
