using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using Svalinn;
using Svalinn.Extensions;
using Svalinn.Strategy;
using Svalinn.SystemState;
using Svalinn.SystemState.Providers;

static void Check(bool condition, string message)
{
    if (!condition) throw new Exception(message);
}

var cpu = new CpuLoadOptions { SmoothingFactor = 1 };
var tracker = new CpuLoadTracker(cpu, 1);
Check(!tracker.Sample(.8, 1).Overloaded, "Below entry threshold");
Check(tracker.Sample(.9, 1).Overloaded, "Enter at threshold");
Check(tracker.Sample(.8, 1).Overloaded, "Hysteresis must hold overload");
Check(!tracker.Sample(.75, 1).Overloaded, "Recover at threshold");
Check(Math.Abs(new CpuLoadTracker(cpu, 4).Sample(2, 1).Raw - 50) < .001, "Normalize by CPU capacity");
Check(new CpuLoadTracker(cpu, .5).Sample(.5, 1).Raw == 100, "Fractional CPU capacity");
tracker = new CpuLoadTracker(new CpuLoadOptions { SmoothingFactor = .5 }, 1);
tracker.Sample(0, 1);
Check(tracker.Sample(1, 1).Smoothed == 50, "EWMA must smooth a spike");
Check(!tracker.Sample(1, 1).Overloaded, "Do not enter overload at 75%");
Check(!tracker.Sample(1, 1).Overloaded, "Do not enter overload at 87.5%");
Check(tracker.Sample(1, 1).Overloaded, "Enter on sustained pressure");
Check(!tracker.Sample(0, 1).Overloaded, "Recover after pressure falls");

var settings = new SvalinnOptions { Strategy = LoadSheddingStrategyKind.Cpu, MaxConcurrentRequests = 4 };
var strategy = new CpuLoadSheddingStrategy(Options.Create(settings));
async Task<LoadSheddingDecision> Decide(RequestPriority priority, double? fresh, double overload, int inflight)
{
    var state = new SystemState();
    state.Set("inflight_requests", inflight);
    if (fresh is not null) state.Set("cpu_sample_fresh", fresh.Value);
    state.Set("cpu_overloaded", overload);
    state.Set("process_cpu_smoothed_percent", 95);
    return await strategy.DecideAsync(new LoadSheddingContext(new DefaultHttpContext(), priority, state), default);
}
foreach (var priority in new[] { RequestPriority.Low, RequestPriority.Normal })
{
    var decision = await Decide(priority, 1, 1, 0);
    Check(!decision.IsAllowed && decision.RetryAfterSeconds == settings.RetryAfterSeconds, "CPU overload must reject low priorities with Retry-After");
    Check((await Decide(priority, 1, 0, 100)).IsAllowed, "Healthy CPU must not use the inflight threshold");
    Check(!(await Decide(priority, null, 0, 4)).IsAllowed, "Missing sample must use fallback");
    Check(!(await Decide(priority, 0, 0, 4)).IsAllowed, "Stale sample must use fallback");
    Check((await Decide(priority, 0, 0, 0)).IsAllowed, "Fallback must allow available capacity");
}
Check((await Decide(RequestPriority.Critical, 1, 1, 100)).IsAllowed, "Critical bypass");
Check((await Decide(RequestPriority.High, 1, 1, 100)).IsAllowed, "Default High admission");
settings.MinimumPriorityWhenOverloaded = RequestPriority.Critical;
Check(!(await Decide(RequestPriority.High, 1, 1, 0)).IsAllowed, "Respect configured priority floor");

foreach (var kind in Enum.GetValues<LoadSheddingStrategyKind>())
{
    var services = new ServiceCollection();
    services.AddLogging();
    services.AddSvalinn(o => o.Strategy = kind);
    using var container = services.BuildServiceProvider();
    var selected = container.GetRequiredService<ILoadSheddingStrategy>();
    Check(kind switch { LoadSheddingStrategyKind.Cpu => selected is CpuLoadSheddingStrategy, LoadSheddingStrategyKind.Adaptive => selected is AdaptiveLoadSheddingStrategy, LoadSheddingStrategyKind.Cinnamon => selected is CinnamonLoadSheddingStrategy, _ => selected is ThresholdLoadSheddingStrategy }, "DI strategy selection");
}
foreach (var invalid in new Action<SvalinnOptions>[] {
    o => o.Cpu.RecoveryThresholdPercent = 95,
    o => o.Cpu.SmoothingFactor = double.NaN,
    o => o.Cpu.ProcessorCapacity = 0,
    o => o.Cpu.SampleIntervalMs = 1,
    o => o.Adaptive.InitialLimit = 0,
    o => o.Adaptive.LatencyTolerance = double.NaN,
    o => o.Cinnamon.QueueTimeoutMs = 0,
    o => o.Cinnamon.ProportionalGain = double.NaN,
    o => o.Strategy = (LoadSheddingStrategyKind)99 })
{
    var services = new ServiceCollection();
    services.AddSvalinn(invalid);
    using var container = services.BuildServiceProvider();
    try { _ = container.GetRequiredService<IOptions<SvalinnOptions>>().Value; throw new Exception("Invalid options accepted"); }
    catch (OptionsValidationException) { }
}
var monitorSettings = new SvalinnOptions { Strategy = LoadSheddingStrategyKind.Cpu,
    Cpu = new CpuLoadOptions { SampleIntervalMs = 50, ProcessorCapacity = 1 } };
using var monitor = new ProcessCpuStateProvider(Options.Create(monitorSettings));
var initial = new SystemState();
await monitor.CollectAsync(initial, new DefaultHttpContext(), default);
Check(initial.TryGet("cpu_sample_fresh", out var freshValue) && freshValue == 0, "No sample before start");
await monitor.StartAsync(default);
var received = false;
for (var i = 0; i < 100; i++)
{
    await Task.Delay(20);
    var state = new SystemState();
    await monitor.CollectAsync(state, new DefaultHttpContext(), default);
    if (state.TryGet("cpu_sample_fresh", out freshValue) && freshValue == 1)
    {
        Check(state.TryGet("cpu_processor_capacity", out var capacity) && capacity == 1, "Explicit capacity");
        Check(state.TryGet("process_cpu_percent", out var usage) && usage >= 0 && usage <= 100, "CPU range");
        received = true;
        break;
    }
}
await monitor.StopAsync(default);
Check(received, "Background sample not received");
await Task.Delay(2100);
var stale = new SystemState();
await monitor.CollectAsync(stale, new DefaultHttpContext(), default);
Check(stale.TryGet("cpu_sample_fresh", out freshValue) && freshValue == 0, "Old sample must become stale");
await AdaptiveTests.RunAsync();
await CinnamonTests.RunAsync();
Console.WriteLine("PASS: CPU normalization, EWMA, hysteresis, priority admission, fallback, DI, validation and sampler lifecycle");
