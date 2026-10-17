param(
    [ValidateSet('Inflight', 'Cpu')][string]$Strategy = 'Inflight',
    [ValidateRange(50, 60000)][int]$CpuSampleIntervalMs = 250,
    [ValidateRange(0.000001, 1)][double]$CpuSmoothingFactor = 0.3,
    [ValidateRange(0.000001, 100)][double]$CpuOverloadPercent = 90,
    [ValidateRange(0, 99.999999)][double]$CpuRecoveryPercent = 75,
    [Parameter(Mandatory)][string]$OutputDirectory,
    [ValidateRange(5, 600)][int]$DurationSeconds = 30,
    [ValidateRange(1, 20)][int]$Repetitions = 5,
    [int[]]$CpuRates = @(60, 80, 100, 120, 130),
    [int[]]$IoRates = @(40, 50, 60, 70, 80),
    [int[]]$MixedRates = @(100, 120, 140, 160),
    [switch]$Resume,
    [string]$PythonExecutable = (Join-Path $PSScriptRoot 'plotting/.venv/Scripts/python.exe')
)
$ErrorActionPreference = 'Stop'
if ($CpuRecoveryPercent -ge $CpuOverloadPercent) { throw 'CpuRecoveryPercent должен быть ниже CpuOverloadPercent.' }
$root = Split-Path $PSScriptRoot -Parent
$output = [IO.Path]::GetFullPath($OutputDirectory)
if ((Test-Path $output) -and !$Resume) { throw 'Для новой матрицы нужна новая папка; для продолжения используйте Resume.' }
if ($Resume -and !(Test-Path (Join-Path $output 'matrix.json'))) { throw 'Матрица для продолжения не найдена.' }
$matrix = @{
    strategy = $Strategy; cpuSampleIntervalMs = $CpuSampleIntervalMs; cpuSmoothingFactor = $CpuSmoothingFactor
    cpuOverloadPercent = $CpuOverloadPercent; cpuRecoveryPercent = $CpuRecoveryPercent
    scenario = 'SuccessDisaster'; durationSeconds = $DurationSeconds; repetitions = $Repetitions
    purpose = 'Подтверждение границ фиксированной стратегии; параметры выбраны до измерений.'
    series = @(
        @{ workload = 'Cpu'; directory = 'cpu'; rates = $CpuRates },
        @{ workload = 'Io'; directory = 'io'; rates = $IoRates },
        @{ workload = 'Mixed'; directory = 'mixed'; rates = $MixedRates }
    )
}
foreach ($series in $matrix.series) {
    if (!$series.rates.Count -or @($series.rates | Where-Object { $_ -le 0 }).Count -or
        @($series.rates | Select-Object -Unique).Count -ne $series.rates.Count) {
        throw "Уровни $($series.workload) должны быть положительными, непустыми и без повторов."
    }
}
$totalRuns = ($CpuRates.Count + $IoRates.Count + $MixedRates.Count) * $Repetitions * 2
Write-Host "Матрица: $totalRuns прогонов по $DurationSeconds с; $Repetitions повторов OFF/ON."
if (!$Resume) {
    New-Item -ItemType Directory -Path $output | Out-Null
    $matrix | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $output 'matrix.json') -Encoding utf8
    Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $output 'runner-source.ps1')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'summarize-confirmatory-results.py') -Destination (Join-Path $output 'summary-source.py')
} else {
    $saved = Get-Content (Join-Path $output 'matrix.json') -Raw | ConvertFrom-Json
    if ($saved.scenario -ne $matrix.scenario -or $saved.durationSeconds -ne $DurationSeconds -or $saved.repetitions -ne $Repetitions -or $saved.series.Count -ne 3) {
        throw 'Сохранённая матрица отличается от протокола.'
    }
    foreach ($key in @('strategy', 'cpuSampleIntervalMs', 'cpuSmoothingFactor', 'cpuOverloadPercent', 'cpuRecoveryPercent')) {
        if ($saved.$key -ne $matrix[$key]) { throw "Изменён параметр стратегии $key." }
    }
    for ($i = 0; $i -lt 3; $i++) {
        if ($saved.series[$i].workload -ne $matrix.series[$i].workload -or $saved.series[$i].directory -ne $matrix.series[$i].directory -or
            ($saved.series[$i].rates -join ',') -ne ($matrix.series[$i].rates -join ',')) { throw 'Изменён состав матрицы.' }
    }
}
# Блокировка не позволяет запустить две матрицы в одной папке одновременно.
$lock = [IO.File]::Open((Join-Path $output 'running.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
$status = @{ state = 'running'; processId = $PID; updatedUtc = [DateTime]::UtcNow.ToString('o'); completedSeries = @(); totalRuns = $totalRuns; durationSeconds = $DurationSeconds; repetitions = $Repetitions }
function Save-Status {
    $status.updatedUtc = [DateTime]::UtcNow.ToString('o')
    $status | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $output 'status.json') -Encoding utf8
}
try {
    Save-Status
    # Заранее проверяем обработчик графиков, чтобы не обнаружить отсутствие matplotlib после завершения измерений.
    & $PythonExecutable (Join-Path $PSScriptRoot 'plotting/analyze-results.py') --help
    if ($LASTEXITCODE -ne 0) { throw 'Недоступен анализатор или matplotlib.' }
    & dotnet build (Join-Path $root 'TestApp/TestApp.csproj') -c Release --no-restore -p:IncludeApiDocs=false
    if ($LASTEXITCODE -ne 0) { throw 'Сборка завершилась с ошибкой.' }
    foreach ($series in $matrix.series) {
        $status.currentWorkload = $series.workload
        Save-Status
        $directory = Join-Path $output $series.directory
        $arguments = @{
            Strategy = $Strategy; CpuSampleIntervalMs = $CpuSampleIntervalMs; CpuSmoothingFactor = $CpuSmoothingFactor
            CpuOverloadPercent = $CpuOverloadPercent; CpuRecoveryPercent = $CpuRecoveryPercent
            Scenario = 'SuccessDisaster'; Workload = $series.workload; Rates = $series.rates
            DurationSeconds = $DurationSeconds; Repetitions = $Repetitions; WarmupRequests = 20
            IoSlots = 4; IoServiceMs = 100; Iterations = 100000; MinWorkerThreads = 16
            CriticalCpuPercent = 50; HighCpuPercent = 50; NormalCpuPercent = 50; LowCpuPercent = 50
            MaxConcurrentRequests = 4; SloMs = 250; TimeoutSeconds = 5
            ServerAffinityMask = 1; GeneratorAffinityMask = 4
            SkipBuild = $true; Analyze = $true; PythonExecutable = $PythonExecutable; OutputDirectory = $directory
        }
        if ($Resume -and (Test-Path $directory)) { $arguments.Resume = $true }
        & (Join-Path $PSScriptRoot 'run-experiment.ps1') @arguments
        $status.completedSeries += $series.workload
        Save-Status
    }
    & $PythonExecutable (Join-Path $output 'summary-source.py') $output
    if ($LASTEXITCODE -ne 0) { throw 'Данные сохранены, но матрица не прошла проверку полноты; см. report.txt.' }
    $status.state = 'completed'
    Save-Status
} catch {
    $status.state = 'failed'
    $status.error = $_.Exception.Message
    Save-Status
    throw
} finally { $lock.Dispose() }
