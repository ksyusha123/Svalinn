using System.Diagnostics;
using System.Numerics;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Options;

namespace Svalinn.SystemState.Providers;

// A single sampler publishes an immutable snapshot; request handlers only read it.
public sealed class ProcessCpuStateProvider(IOptions<SvalinnOptions> options) : BackgroundService, ISystemStateProvider
{
    private sealed record Snapshot(double Raw, double Smoothed, bool Overloaded, double Capacity, long Timestamp);
    private Snapshot? snapshot;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        var settings = options.Value;
        if (settings.Strategy != LoadSheddingStrategyKind.Cpu) return;
        using var process = Process.GetCurrentProcess();
        var capacity = settings.Cpu.ProcessorCapacity ?? Environment.ProcessorCount;
        if (settings.Cpu.ProcessorCapacity is null && OperatingSystem.IsWindows())
            capacity = Math.Min(capacity, BitOperations.PopCount(unchecked((ulong)process.ProcessorAffinity.ToInt64())));
        if (capacity <= 0) throw new InvalidOperationException("Available CPU capacity must be positive.");
        var tracker = new CpuLoadTracker(settings.Cpu, capacity);
        var previousCpu = process.TotalProcessorTime.TotalSeconds;
        var previousTime = TimeProvider.System.GetTimestamp();
        using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(settings.Cpu.SampleIntervalMs));
        while (await timer.WaitForNextTickAsync(stoppingToken))
        {
            process.Refresh();
            var currentCpu = process.TotalProcessorTime.TotalSeconds;
            var currentTime = TimeProvider.System.GetTimestamp();
            var sample = tracker.Sample(Math.Max(0, currentCpu - previousCpu),
                TimeProvider.System.GetElapsedTime(previousTime, currentTime).TotalSeconds);
            Volatile.Write(ref snapshot, new Snapshot(sample.Raw, sample.Smoothed, sample.Overloaded, capacity, currentTime));
            previousCpu = currentCpu;
            previousTime = currentTime;
        }
    }

    public ValueTask CollectAsync(SystemState state, HttpContext context, CancellationToken cancellationToken)
    {
        if (options.Value.Strategy != LoadSheddingStrategyKind.Cpu) return ValueTask.CompletedTask;
        var current = Volatile.Read(ref snapshot);
        var ageMs = current is null ? double.PositiveInfinity
            : TimeProvider.System.GetElapsedTime(current.Timestamp).TotalMilliseconds;
        var fresh = current is not null && ageMs <= Math.Max(2000, 4 * options.Value.Cpu.SampleIntervalMs);
        state.Set("cpu_sample_fresh", fresh ? 1 : 0);
        if (current is not null)
        {
            state.Set("process_cpu_percent", current.Raw);
            state.Set("process_cpu_smoothed_percent", current.Smoothed);
            state.Set("cpu_overloaded", current.Overloaded ? 1 : 0);
            state.Set("cpu_processor_capacity", current.Capacity);
            state.Set("cpu_sample_age_ms", ageMs);
        }
        return ValueTask.CompletedTask;
    }
}
