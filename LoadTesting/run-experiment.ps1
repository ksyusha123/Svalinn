param(
    [ValidateSet('Inflight', 'Cpu')][string]$Strategy = 'Inflight',
    [ValidateRange(50, 60000)][int]$CpuSampleIntervalMs = 250,
    [ValidateRange(0.000001, 1)][double]$CpuSmoothingFactor = 0.3,
    [ValidateRange(0.000001, 100)][double]$CpuOverloadPercent = 90,
    [ValidateRange(0, 99.999999)][double]$CpuRecoveryPercent = 75,
    [ValidateSet('LowFlood', 'SuccessDisaster')][string]$Scenario = 'LowFlood',
    [ValidateSet('Cpu', 'Io', 'Mixed', 'Delay')][string]$Workload = 'Cpu',
    [ValidateRange(0, 30000)][int]$CriticalDelayMs = 800,
    [ValidateRange(0, 30000)][int]$HighDelayMs = 1600,
    [ValidateRange(0, 30000)][int]$NormalDelayMs = 800,
    [ValidateRange(0, 30000)][int]$LowDelayMs = 3000,
    [ValidateRange(1, 60000)][int]$HighSloMs,
    [int[]]$Rates = @(20, 40, 60, 80, 100, 120, 140, 160),
    [ValidateRange(1, 256)][int]$IoSlots = 4,
    [ValidateRange(1, 10000)][int]$IoServiceMs = 100,
    [ValidateRange(0, 100)][int]$CriticalCpuPercent = 50,
    [ValidateRange(0, 100)][int]$HighCpuPercent = 50,
    [ValidateRange(0, 100)][int]$NormalCpuPercent = 50,
    [ValidateRange(0, 100)][int]$LowCpuPercent = 50,
    [ValidateRange(1, 1000)][int]$WarmupRequests = 20,
    [int[]]$LowRates = @(5, 15, 25, 35, 45, 55, 70, 85),
    [ValidateRange(1, 1000)][int]$CriticalRate = 5,
    [ValidateRange(5, 600)][int]$DurationSeconds = 30,
    [ValidateRange(1, 20)][int]$Repetitions = 3,
    [ValidateRange(1000, 10000000)][int]$Iterations = 100000,
    [ValidateRange(1, 256)][int]$MinWorkerThreads = 16,
    [ValidateRange(1, 10000)][int]$MaxConcurrentRequests = 4,
    [ValidateRange(1, 60000)][int]$SloMs = 250,
    [ValidateRange(1, 60)][int]$TimeoutSeconds = 5,
    [long]$ServerAffinityMask = 1,
    [long]$GeneratorAffinityMask = 4,
    [int]$Port = 5026,
    [string]$OutputDirectory,
    [switch]$Resume,
    [switch]$SkipBuild,
    [switch]$Analyze,
    [string]$PythonExecutable = (Join-Path $PSScriptRoot 'plotting/.venv/Scripts/python.exe')
)
$ErrorActionPreference = 'Stop'
if ($CpuRecoveryPercent -ge $CpuOverloadPercent) { throw 'CpuRecoveryPercent должен быть ниже CpuOverloadPercent.' }
$root = Split-Path $PSScriptRoot -Parent
$Scenario = if ($Scenario -eq 'SuccessDisaster') { 'SuccessDisaster' } else { 'LowFlood' }
$Workload = switch ($Workload) { 'Cpu' { 'Cpu' }; 'Io' { 'Io' }; 'Mixed' { 'Mixed' }; 'Delay' { 'Delay' } }
if (!$PSBoundParameters.ContainsKey('SloMs') -and $Workload -eq 'Delay') { $SloMs = 1200 }
if (!$PSBoundParameters.ContainsKey('HighSloMs')) { $HighSloMs = if ($Workload -eq 'Delay') { 2500 } else { $SloMs } }
$isSuccessDisaster = $Scenario -eq 'SuccessDisaster'
if ($isSuccessDisaster -and ($PSBoundParameters.ContainsKey('LowRates') -or $PSBoundParameters.ContainsKey('CriticalRate'))) {
    throw 'Для SuccessDisaster задайте общую интенсивность через Rates; доли приоритетов фиксированы.'
}
if (!$isSuccessDisaster -and $PSBoundParameters.ContainsKey('Rates')) { throw 'Для LowFlood используйте LowRates и CriticalRate.' }
if ($Resume -and !$OutputDirectory) { throw 'Для Resume укажите папку прерванной серии.' }
if (!$OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot ("results/$Scenario-$Workload-$Strategy-" + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
$output = [IO.Path]::GetFullPath($OutputDirectory)
if ((Test-Path $output) -and !$Resume) { throw "Укажите новую папку результатов: $output" }
if ($Resume -and !(Test-Path (Join-Path $output 'manifest.json'))) { throw 'Не найден manifest.json прерванной серии.' }
if ($ServerAffinityMask -le 0 -or ($ServerAffinityMask -band ($ServerAffinityMask - 1)) -ne 0) {
    throw 'ServerAffinityMask должна выбирать ровно один логический процессор.'
}
if ($GeneratorAffinityMask -le 0 -or ($ServerAffinityMask -band $GeneratorAffinityMask) -ne 0) {
    throw 'Маски привязки сервера и генератора должны быть положительными и не пересекаться.'
}
$available = [Diagnostics.Process]::GetCurrentProcess().ProcessorAffinity.ToInt64()
if (($available -band $ServerAffinityMask) -ne $ServerAffinityMask -or ($available -band $GeneratorAffinityMask) -ne $GeneratorAffinityMask) {
    throw 'Указанные процессоры недоступны.'
}
if (@($LowRates | Where-Object { $_ -lt 0 }).Count) { throw 'Интенсивность Low не может быть отрицательной.' }
if (!$Rates.Count -or @($Rates | Where-Object { $_ -le 0 }).Count) { throw 'Rates должен содержать положительные значения.' }
$loadLevels = if ($isSuccessDisaster) { $Rates } else { $LowRates }
if (!$loadLevels.Count -or @($loadLevels | Select-Object -Unique).Count -ne $loadLevels.Count) { throw 'Уровни нагрузки должны быть непустыми и не повторяться.' }
$k6Script = if ($isSuccessDisaster) { 'k6/success-disaster.js' } else { 'k6/low-flood.js' }
$dotnet = (Get-Command dotnet).Source
$k6 = (Get-Command k6).Source
if (!$SkipBuild) {
    & $dotnet build (Join-Path $root 'TestApp/TestApp.csproj') -c Release -p:IncludeApiDocs=false
    if ($LASTEXITCODE -ne 0) { throw 'Сборка завершилась с ошибкой.' }
}
$dll = Join-Path $root 'TestApp/bin/Release/net10.0/TestApp.dll'
if (!(Test-Path $dll)) { throw 'Сборка Release отсутствует.' }
if (!$Resume) { New-Item -ItemType Directory -Path $output | Out-Null }
$baseUrl = "http://127.0.0.1:$Port"
$settings = @{
    strategy = $Strategy; cpuSampleIntervalMs = $CpuSampleIntervalMs; cpuSmoothingFactor = $CpuSmoothingFactor
    cpuOverloadPercent = $CpuOverloadPercent; cpuRecoveryPercent = $CpuRecoveryPercent; cpuProcessorCapacity = 1
    workload = $Workload; ioSlots = $IoSlots; ioServiceMs = $IoServiceMs; warmupRequests = $WarmupRequests
    cpuPercent = @{ critical = $CriticalCpuPercent; high = $HighCpuPercent; normal = $NormalCpuPercent; low = $LowCpuPercent }
    scenario = $Scenario; rates = $Rates; loadLevels = @($loadLevels); k6Script = $k6Script
    priorityShares = if ($isSuccessDisaster) { @{ critical = 25; high = 30; normal = 25; low = 20 } } else { $null }
    createdUtc = [DateTime]::UtcNow.ToString('o'); lowRates = $LowRates; criticalRate = $CriticalRate
    durationSeconds = $DurationSeconds; repetitions = $Repetitions; iterations = $Iterations
    minWorkerThreads = $MinWorkerThreads; maxConcurrentRequests = $MaxConcurrentRequests
    criticalDelayMs = $CriticalDelayMs; highDelayMs = $HighDelayMs; normalDelayMs = $NormalDelayMs; lowDelayMs = $LowDelayMs
    highSloMs = $HighSloMs; sloMs = $SloMs; timeoutSeconds = $TimeoutSeconds
    serverAffinityMask = $ServerAffinityMask; generatorAffinityMask = $GeneratorAffinityMask
    logicalProcessors = [Environment]::ProcessorCount; os = [Environment]::OSVersion.ToString()
    dotnetVersion = (& $dotnet --version); k6Version = (& $k6 version | Select-Object -First 1)
    binarySha256 = (Get-FileHash -LiteralPath $dll -Algorithm SHA256).Hash
    processorDescription = [Environment]::GetEnvironmentVariable('PROCESSOR_IDENTIFIER')
    baseCommit = (& git -c "safe.directory=$($root.Replace('\','/'))" -C $root rev-parse HEAD)
}
if ($isSuccessDisaster) { $settings.Remove('criticalRate'); $settings.Remove('lowRates') }
else { $settings.Remove('rates'); $settings.Remove('priorityShares') }
if ($Workload -ne 'Mixed') {
    foreach ($priority in @('critical', 'high', 'normal', 'low')) { $settings.cpuPercent[$priority] = if ($Workload -eq 'Cpu') { 100 } else { 0 } }
}
$snapshotPrefix = ''
if ($Resume) {
    $previous = Get-Content (Join-Path $output 'manifest.json') -Raw | ConvertFrom-Json
    foreach ($key in @('strategy', 'cpuSampleIntervalMs', 'cpuSmoothingFactor', 'cpuOverloadPercent', 'cpuRecoveryPercent', 'cpuProcessorCapacity', 'scenario', 'workload', 'ioSlots', 'ioServiceMs', 'warmupRequests',
        'durationSeconds', 'repetitions', 'iterations', 'minWorkerThreads', 'maxConcurrentRequests',
        'sloMs', 'highSloMs', 'criticalDelayMs', 'highDelayMs', 'normalDelayMs', 'lowDelayMs', 'timeoutSeconds', 'serverAffinityMask', 'generatorAffinityMask', 'binarySha256', 'k6Version', 'dotnetVersion', 'criticalRate')) {
        if ($settings[$key] -ne $previous.$key) { throw "Нельзя продолжить: изменён параметр $key." }
    }
    if (($settings.loadLevels -join ',') -ne ($previous.loadLevels -join ',')) { throw 'Нельзя продолжить: изменены уровни нагрузки.' }
    foreach ($priority in @('critical', 'high', 'normal', 'low')) {
        if ($settings.cpuPercent[$priority] -ne $previous.cpuPercent.$priority) { throw "Изменена доля CPU для $priority." }
    }
    foreach ($entry in (Get-Content (Join-Path $output 'source-hashes.json') -Raw | ConvertFrom-Json)) {
        # Исправления записи телеметрии и обработки результатов допустимы; модель нагрузки и сервер должны совпадать.
        if ($entry.Path -in @('LoadTesting/run-experiment.ps1', 'LoadTesting/plotting/analyze-results.py')) { continue }
        if ((Get-FileHash -LiteralPath (Join-Path $root $entry.Path) -Algorithm SHA256).Hash -ne $entry.Sha256) {
            throw "Нельзя продолжить: изменён исходник $($entry.Path)."
        }
    }
    $snapshotPrefix = 'resume-' + (Get-Date -Format 'yyyyMMdd-HHmmssfff') + '-'
    $settings | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $output ($snapshotPrefix + 'settings.json')) -Encoding utf8
} else {
    $settings | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $output 'manifest.json') -Encoding utf8
}

# Сохраняем фактические исходники эксперимента, включая изменения вне коммита Git.
$sourceFiles = @('TestApp/Program.cs', 'TestApp/CpuBenchmark.cs', 'TestApp/IoBenchmark.cs', 'TestApp/DelayBenchmark.cs', 'TestApp/TestApp.csproj',
    'TestApp/appsettings.json', 'LoadTesting/k6/low-flood.js', 'LoadTesting/k6/success-disaster.js',
    'LoadTesting/k6/workload-model.js', 'LoadTesting/plotting/analyze-results.py', 'LoadTesting/run-experiment.ps1')
$sourceFiles += @(Get-ChildItem (Join-Path $root 'Svalinn') -File -Recurse |
    Where-Object { $_.Extension -in @('.cs', '.csproj') -and $_.FullName -notmatch '[\\/](bin|obj)[\\/]' } |
    ForEach-Object { $_.FullName.Substring($root.TrimEnd('\', '/').Length + 1).Replace('\', '/') })
$sourceFiles | ForEach-Object {
    [pscustomobject]@{ Path = $_; Sha256 = (Get-FileHash -LiteralPath (Join-Path $root $_) -Algorithm SHA256).Hash }
} | ConvertTo-Json | Set-Content (Join-Path $output ($snapshotPrefix + 'source-hashes.json')) -Encoding utf8
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::Open((Join-Path $output ($snapshotPrefix + 'experiment-source.zip')), [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($source in $sourceFiles) {
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, (Join-Path $root $source), $source.Replace('\','/')) | Out-Null
    }
} finally { $archive.Dispose() }

function Start-Server([bool]$Enabled, [string]$Name) {
    $args = @('"' + $dll + '"', '--urls', $baseUrl,
        "--CpuBenchmark:Enabled=$($Workload -ne 'Delay')", "--CpuBenchmark:Iterations=$Iterations",
        "--CpuBenchmark:MinWorkerThreads=$MinWorkerThreads", "--CpuBenchmark:AffinityMask=$ServerAffinityMask",
        "--DelayBenchmark:CriticalDelayMs=$CriticalDelayMs", "--DelayBenchmark:HighDelayMs=$HighDelayMs",
        "--DelayBenchmark:NormalDelayMs=$NormalDelayMs", "--DelayBenchmark:LowDelayMs=$LowDelayMs",
        "--DelayBenchmark:Slo:CriticalP95LatencyMs=$SloMs", "--DelayBenchmark:Slo:HighP95LatencyMs=$HighSloMs",
        "--IoBenchmark:Slots=$IoSlots", "--IoBenchmark:ServiceMs=$IoServiceMs",
        "--Svalinn:Enabled=$($Enabled.ToString().ToLowerInvariant())",
        "--Svalinn:Strategy=$Strategy", "--Svalinn:Cpu:SampleIntervalMs=$CpuSampleIntervalMs",
        "--Svalinn:Cpu:SmoothingFactor=$($CpuSmoothingFactor.ToString([Globalization.CultureInfo]::InvariantCulture))",
        "--Svalinn:Cpu:OverloadThresholdPercent=$($CpuOverloadPercent.ToString([Globalization.CultureInfo]::InvariantCulture))",
        "--Svalinn:Cpu:RecoveryThresholdPercent=$($CpuRecoveryPercent.ToString([Globalization.CultureInfo]::InvariantCulture))",
        '--Svalinn:Cpu:ProcessorCapacity=1',
        "--Svalinn:MaxConcurrentRequests=$MaxConcurrentRequests",
        '--Svalinn:MinimumPriorityWhenOverloaded=High', '--Svalinn:AlwaysAllowCriticalRequests=true',
        '--Logging:LogLevel:Default=Error', '--Logging:LogLevel:Microsoft.AspNetCore=Error')
    $server = Start-Process $dotnet -ArgumentList $args -WorkingDirectory (Join-Path $root 'TestApp') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $output "$Name.server.log") -RedirectStandardError (Join-Path $output "$Name.server.err")
    try {
        if ($Workload -eq 'Delay') { $server.ProcessorAffinity = [IntPtr]$ServerAffinityMask }
        for ($attempt = 0; $attempt -lt 100; $attempt++) {
            if ($server.HasExited) { throw "Сервер завершился; проверьте $Name.server.err" }
            try {
                if ($Workload -eq 'Delay') {
                    $info = Invoke-RestMethod "$baseUrl/benchmark/delay/info" -TimeoutSec 2
                    if ($info.processId -ne $server.Id) { throw 'Порт занят другим процессом.' }
                    if (($info.endpoints.delayMs -join ',') -ne (@($CriticalDelayMs, $HighDelayMs, $NormalDelayMs, $LowDelayMs) -join ',')) { throw 'Настройки задержек не совпадают.' }
                    if ($server.ProcessorAffinity.ToInt64() -ne $ServerAffinityMask) { throw 'Неверная привязка сервера к CPU.' }
                    return $server
                }
                $info = Invoke-RestMethod "$baseUrl/benchmark/cpu/info" -TimeoutSec 1
                if ($info.processId -ne $server.Id) { throw 'Порт занят другим процессом.' }
                if ($info.affinityMask -ne $ServerAffinityMask -or $info.iterations -ne $Iterations) { throw 'Настройки сервера не соответствуют ожидаемым.' }
                $ioInfo = Invoke-RestMethod "$baseUrl/benchmark/telemetry" -TimeoutSec 2
                if ($ioInfo.capacity -ne $IoSlots -or $ioInfo.serviceMs -ne $IoServiceMs) { throw 'Настройки I/O-ресурса не соответствуют ожидаемым.' }
                return $server
            } catch { if ($attempt -eq 99) { throw }; Start-Sleep -Milliseconds 100 }
        }
    } catch { if (!$server.HasExited) { Stop-Process -Id $server.Id }; throw }
}

function Warm-Up([string]$Kind = $Workload) {
    # Последовательный прогрев завершается до подачи измеряемой нагрузки.
    1..$WarmupRequests | ForEach-Object {
        $route = if ($Kind -eq 'Mixed') { if ($_ % 2) { 'cpu' } else { 'io' } } else { $Kind.ToLowerInvariant() }
        Invoke-RestMethod "$baseUrl/benchmark/$route/critical" -TimeoutSec ([Math]::Max(15, [int][Math]::Ceiling($CriticalDelayMs / 1000.0) + 5)) | Out-Null
    }
}

$server = $null
$generator = $null
try {
    if ($Workload -ne 'Delay' -and !(Test-Path (Join-Path $output 'calibration.json'))) {
    $server = Start-Server $false 'calibration'
    Warm-Up 'Cpu'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $server.Refresh(); $cpuStart = $server.TotalProcessorTime.TotalSeconds
    $latencies = @(1..40 | ForEach-Object {
        $requestWatch = [Diagnostics.Stopwatch]::StartNew()
        $response = Invoke-RestMethod "$baseUrl/benchmark/cpu/critical" -TimeoutSec 10
        $requestWatch.Stop(); $requestWatch.Elapsed.TotalMilliseconds
    })
    $server.Refresh()
    $cpuPerRequest = ($server.TotalProcessorTime.TotalSeconds - $cpuStart) / 40
    if ($cpuPerRequest -le 0) { throw 'Вычислительная работа ниже точности учёта CPU; увеличьте Iterations.' }
    $watch.Stop()
    # Проверяем одинаковый объём работы и доступность всех конечных точек с метаданными приоритета.
    $checksums = @('critical', 'high', 'normal', 'low' | ForEach-Object { (Invoke-RestMethod "$baseUrl/benchmark/cpu/$_").checksum } | Select-Object -Unique)
    if ($checksums.Count -ne 1) { throw 'Вычислительная работа различается между приоритетами.' }
    @{
        cpuSecondsPerRequest = $cpuPerRequest
        estimatedSingleCpuCapacityRps = 1 / $cpuPerRequest
        meanSerialLatencyMs = ($latencies | Measure-Object -Average).Average
        checksum = $checksums[0]; samples = 40
        ioSlots = $IoSlots; ioServiceMs = $IoServiceMs; theoreticalIoCapacityRps = 1000 * $IoSlots / $IoServiceMs
    } | ConvertTo-Json | Set-Content (Join-Path $output 'calibration.json') -Encoding utf8
    Write-Host ('Калибровка: {0:N1} мс CPU на запрос; примерно {1:N1} запросов/с на одном логическом CPU' -f ($cpuPerRequest * 1000), (1 / $cpuPerRequest))
    Stop-Process -Id $server.Id; $server.WaitForExit(); $server = $null
    }

    foreach ($repetition in 1..$Repetitions) {
        foreach ($load in $loadLevels) {
            # Меняем порядок A/B через повтор.
            $modes = if ($repetition % 2) { @('off', 'on') } else { @('on', 'off') }
            foreach ($mode in $modes) {
                $name = if ($isSuccessDisaster) { "r$repetition-rate$load-$mode" } else { "r$repetition-low$load-$mode" }
                if ($Resume) {
                    $complete = @('.json', '.cpu.csv', '.run.json' | Where-Object { !(Test-Path (Join-Path $output ($name + $_))) }).Count -eq 0
                    if ($complete) { Write-Host "Пропущен завершённый прогон $name"; continue }
                    $partial = @(Get-ChildItem -LiteralPath $output -File | Where-Object { $_.Name.StartsWith($name + '.') })
                    if ($partial.Count) {
                        $backup = Join-Path $output ($snapshotPrefix + 'incomplete-' + $name)
                        New-Item -ItemType Directory -Path $backup | Out-Null
                        foreach ($file in $partial) { Move-Item -LiteralPath $file.FullName -Destination $backup }
                    }
                }
                Write-Host "Выполняется прогон $name"
                $server = Start-Server ($mode -eq 'on') $name
                Warm-Up
                $ioBefore = $null
                if ($Workload -ne 'Delay') { $ioBefore = Invoke-RestMethod "$baseUrl/benchmark/telemetry" -TimeoutSec 2 }
                $summary = Join-Path $output "$name.json"
                $args = @('run', '--quiet', '--address', '127.0.0.1:0',
                    '-e', "BASE_URL=$baseUrl", '-e', "CRITICAL_RATE=$CriticalRate", '-e', "LOW_RATE=$load", '-e', "RATE=$load",
                    '-e', "DURATION_SECONDS=$DurationSeconds", '-e', "SLO_MS=$SloMs", '-e', "HIGH_SLO_MS=$HighSloMs", '-e', "TIMEOUT_SECONDS=$TimeoutSeconds",
                    '-e', "WORKLOAD=$Workload", '-e', "CRITICAL_CPU_PERCENT=$CriticalCpuPercent", '-e', "HIGH_CPU_PERCENT=$HighCpuPercent",
                    '-e', "NORMAL_CPU_PERCENT=$NormalCpuPercent", '-e', "LOW_CPU_PERCENT=$LowCpuPercent",
                    '-e', "MODE=$mode", '-e', "STRATEGY=$Strategy", '-e', "REPETITION=$repetition",
                    '-e', ('SUMMARY_PATH="' + $summary + '"'), ('"' + (Join-Path $PSScriptRoot $k6Script) + '"'))
                $generator = Start-Process $k6 -ArgumentList $args -WindowStyle Hidden -PassThru `
                    -RedirectStandardOutput (Join-Path $output "$name.k6.log") -RedirectStandardError (Join-Path $output "$name.k6.err")
                $generator.ProcessorAffinity = [IntPtr]$GeneratorAffinityMask
                $actualGeneratorAffinity = $generator.ProcessorAffinity.ToInt64()
                if ($actualGeneratorAffinity -ne $GeneratorAffinityMask) { throw 'Не удалось применить привязку генератора к CPU.' }
                $samples = [Collections.Generic.List[object]]::new()
                $watch = [Diagnostics.Stopwatch]::StartNew()
                $server.Refresh(); $lastCpu = $server.TotalProcessorTime.TotalSeconds; $lastTime = 0.0
                $generator.Refresh(); $lastGeneratorCpu = $generator.TotalProcessorTime.TotalSeconds
                while (!$generator.HasExited) {
                    Start-Sleep -Milliseconds 500
                    $ioState = $null
                    if ($Workload -in @('Io', 'Mixed')) {
                        try { $ioState = Invoke-RestMethod "$baseUrl/benchmark/telemetry" -TimeoutSec 1 } catch { }
                    }
                    $server.Refresh(); $generator.Refresh()
                    if ($server.HasExited) { throw 'Сервер завершился во время нагрузочного теста.' }
                    $now = $watch.Elapsed.TotalSeconds; $cpu = $server.TotalProcessorTime.TotalSeconds
                    $generatorCpu = $generator.TotalProcessorTime.TotalSeconds
                    $samples.Add([pscustomobject]@{
                        elapsedSeconds = $now; serverCpuPercentOfOneLogicalProcessor = 100 * ($cpu - $lastCpu) / ($now - $lastTime)
                        serverWorkingSetBytes = $server.WorkingSet64; serverThreads = $server.Threads.Count
                        generatorCpuPercentOfOneLogicalProcessor = 100 * ($generatorCpu - $lastGeneratorCpu) / ($now - $lastTime)
                        ioActive = if ($null -ne $ioState) { $ioState.active } else { $null }
                        ioWaiting = if ($null -ne $ioState) { $ioState.waiting } else { $null }
                    })
                    $lastCpu = $cpu; $lastTime = $now
                    $lastGeneratorCpu = $generatorCpu
                    if ($now -gt $DurationSeconds + $TimeoutSeconds + 60) { throw 'Генератор нагрузки превысил допустимое время работы.' }
                }
                $generator.WaitForExit()
                $samples | Export-Csv (Join-Path $output "$name.cpu.csv") -NoTypeInformation -Encoding utf8
                $exitCode = $generator.ExitCode
                if ($exitCode -notin @(0, 99) -or !(Test-Path $summary)) { throw "k6 завершился с кодом $exitCode; проверьте $name.k6.err" }
                # Телеметрия тоже может задержаться при перегрузке. Клиентские измерения уже сохранены.
                $ioAfter = $null
                for ($attempt = 0; $Workload -ne 'Delay' -and $attempt -lt 3 -and $null -eq $ioAfter; $attempt++) {
                    try { $ioAfter = Invoke-RestMethod "$baseUrl/benchmark/telemetry" -TimeoutSec 2 } catch { }
                }
                if ($Workload -ne 'Delay' -and $null -eq $ioAfter) { Write-Warning "Нет итоговой телеметрии $name; число завершённых обслуживаний останется неизвестным." }
                @{ exitCode = $exitCode; serverAffinityMask = $server.ProcessorAffinity.ToInt64(); generatorAffinityMask = $actualGeneratorAffinity
                    ioBefore = $ioBefore; ioAfter = $ioAfter; ioTelemetryComplete = ($null -ne $ioAfter); sourceSnapshotPrefix = $snapshotPrefix } |
                    ConvertTo-Json -Depth 5 | Set-Content (Join-Path $output "$name.run.json") -Encoding utf8
                # Нарушение порогов SLO (код 99) — результат эксперимента, а не сбой скрипта запуска.
                $generator = $null
                Stop-Process -Id $server.Id; $server.WaitForExit(); $server = $null
            }
        }
    }
} finally {
    if ($generator -and !$generator.HasExited) { Stop-Process -Id $generator.Id }
    if ($server -and !$server.HasExited) { Stop-Process -Id $server.Id }
}
Write-Host "Результаты: $output"
if ($Analyze) {
    & $PythonExecutable (Join-Path $PSScriptRoot 'plotting/analyze-results.py') $output
    if ($LASTEXITCODE -ne 0) { throw "Измерения сохранены в $output, но построение графиков завершилось с ошибкой." }
}
