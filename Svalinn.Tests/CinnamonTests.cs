using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Options;
using Svalinn;
using Svalinn.Priority;
using Svalinn.Priority.PriorityResolvers;
using Svalinn.Strategy;
using Svalinn.Strategy.Cinnamon;
using Svalinn.SystemState;
using Svalinn.SystemState.Providers;
using Svalinn.Observers;

internal static class CinnamonTests
{
    private static void Check(bool value, string message) { if (!value) throw new Exception(message); }
    private static void Near(double a, double b, string message) => Check(Math.Abs(a - b) < .00001, message);
    private static SvalinnOptions Settings() => new() { Strategy = LoadSheddingStrategyKind.Cinnamon, Cinnamon = new()
    {
        InitialLimit = 1, MinLimit = 1, MaxLimit = 1, EnablePi = false, EnableAutoTuner = false,
        QueueCapacity = 3, QueueTimeoutMs = 100, PiIntervalMs = 10,
        LatencyMinIntervalMs = 20, LatencyMaxIntervalMs = 100, LatencyMinSamples = 1
    }};
    private static LoadSheddingContext Context(RequestPriority priority = RequestPriority.Critical, string? group = null)
    {
        var http = new DefaultHttpContext();
        if (group is not null) http.SetEndpoint(new Endpoint(_ => Task.CompletedTask,
            new EndpointMetadataCollection(new CinnamonGroupAttribute(group)), group));
        return new(http, priority, new Svalinn.SystemState.SystemState());
    }
    private static async Task Cancelled(Task<RequestAdmission> task)
    {
        try { var admission = await task; admission.Lease?.Dispose(); throw new Exception("Expected cancellation"); }
        catch (OperationCanceledException) { }
    }

    public static async Task RunAsync()
    {
        Controllers();
        var clock = new ManualClock();
        using (var strategy = new CinnamonLoadSheddingStrategy(Options.Create(Settings()), clock, () => .5))
        {
            var running = await strategy.AcquireAsync(Context(RequestPriority.Low), default);
            var low1 = strategy.AcquireAsync(Context(RequestPriority.Low), default).AsTask();
            var low2 = strategy.AcquireAsync(Context(RequestPriority.Low), default).AsTask();
            var critical = strategy.AcquireAsync(Context(), default).AsTask();
            var full = await strategy.AcquireAsync(Context(), default);
            Check(!full.Decision.IsAllowed && full.Decision.RetryAfterSeconds == 5, "Queue capacity must bound all priorities");
            Check(strategy.Snapshot()[0].Queued == 3 && !critical.IsCompleted, "Wait asynchronously at limit");
            running.Lease!.Dispose();
            var high = await critical;
            Check(!low1.IsCompleted && !low2.IsCompleted, "Priority scheduling must prefer Critical");
            high.Lease!.Dispose();
            var first = await low1;
            Check(!low2.IsCompleted, "Equal priority must preserve FIFO");
            first.Lease!.Dispose();
            first.Lease.Dispose();
            (await low2).Lease!.Dispose();
            Check(strategy.Snapshot()[0].Inflight == 0 && strategy.Snapshot()[0].Queued == 0, "Permits released exactly once");
        }
        using (var strategy = new CinnamonLoadSheddingStrategy(Options.Create(Settings()), clock, () => .5))
        {
            var running = await strategy.AcquireAsync(Context(), default);
            using var cancellation = new CancellationTokenSource();
            var waiting = strategy.AcquireAsync(Context(), cancellation.Token).AsTask();
            cancellation.Cancel();
            await Cancelled(waiting);
            Check(strategy.Snapshot()[0].Queued == 0 && strategy.Snapshot()[0].QueueCancelled == 1, "Cancel removes queued request");
            var timeout = strategy.AcquireAsync(Context(), default).AsTask();
            clock.Advance(101);
            var rejected = await timeout;
            Check(!rejected.Decision.IsAllowed && rejected.Decision.Reason.Contains("timeout"), "Queue deadline returns explicit rejection");
            Check(strategy.Snapshot()[0].Queued == 0 && strategy.Snapshot()[0].QueueTimeout == 1, "Timeout removes queue node");
            running.Lease!.Dispose();
            var next = await strategy.AcquireAsync(Context(), default);
            next.Lease!.Dispose();
            Check(strategy.Snapshot()[0].Inflight == 0, "Can admit after cancellation and expiry");
        }
        // Real contention between a grant and cancellation must never leak or double-release capacity.
        for (var i = 0; i < 100; i++)
        {
            using var strategy = new CinnamonLoadSheddingStrategy(Options.Create(Settings()), TimeProvider.System, () => .5);
            var running = await strategy.AcquireAsync(Context(), default);
            using var cancellation = new CancellationTokenSource();
            var pending = strategy.AcquireAsync(Context(), cancellation.Token).AsTask();
            await Task.WhenAll(Task.Run(() => cancellation.Cancel()), Task.Run(() => running.Lease!.Dispose()));
            try { (await pending).Lease?.Dispose(); } catch (OperationCanceledException) { }
            Check(strategy.Snapshot()[0].Inflight == 0 && strategy.Snapshot()[0].Queued == 0, "Cancellation/grant race leaked capacity");
        }
        using (var strategy = new CinnamonLoadSheddingStrategy(Options.Create(Settings()), clock, () => .5))
        {
            var running = await strategy.AcquireAsync(Context(), default);
            var waiting = strategy.AcquireAsync(Context(), default).AsTask();
            await strategy.StopAsync(default);
            Check(!(await waiting).Decision.IsAllowed, "Shutdown completes queued requests");
            Check(!(await strategy.AcquireAsync(Context(), default)).Decision.IsAllowed, "Shutdown refuses new arrivals");
            running.Lease!.Dispose();
            Check(strategy.Snapshot()[0].Inflight == 0, "Release in-flight request after shutdown");
        }
        var grouped = Settings(); grouped.Cinnamon.MaxGroups = 2;
        using (var strategy = new CinnamonLoadSheddingStrategy(Options.Create(grouped), clock, () => .5))
        {
            var a = await strategy.AcquireAsync(Context(group: "a"), default);
            var b = await strategy.AcquireAsync(Context(group: "b"), default);
            Check(a.Decision.IsAllowed && b.Decision.IsAllowed, "Independent groups have independent budgets");
            Check(!(await strategy.AcquireAsync(Context(group: "c"), default)).Decision.IsAllowed, "Group cardinality bounded");
            a.Lease!.Dispose(); b.Lease!.Dispose();
            var metrics = strategy.ToPrometheusText();
            Check(metrics.Split("# TYPE svalinn_cinnamon_limit gauge").Length == 2, "Prometheus TYPE emitted once");
            Check(metrics.Contains("group=\"a\"") && metrics.Contains("group=\"b\""), "Metrics identify control groups");
        }
        var tuningClock = new ManualClock();
        var dynamicOptions = Settings();
        dynamicOptions.Cinnamon.MaxLimit = 20;
        dynamicOptions.Cinnamon.EnableAutoTuner = true;
        dynamicOptions.Cinnamon.SmoothingFactor = 1;
        dynamicOptions.Cinnamon.QueueTimeoutMs = 1000;
        using (var strategy = new CinnamonLoadSheddingStrategy(Options.Create(dynamicOptions), tuningClock, () => .5))
        {
            var first = await strategy.AcquireAsync(Context(), default);
            var second = strategy.AcquireAsync(Context(), default).AsTask();
            var third = strategy.AcquireAsync(Context(), default).AsTask();
            first.Lease!.RecordCompletion(TimeSpan.FromMilliseconds(10), true);
            first.Lease.Dispose();
            var runningSecond = await second;
            tuningClock.Advance(20); strategy.Tick();
            var runningThird = await third;
            Check(strategy.Snapshot()[0].Limit > 1, "Raising limit must wake queued requests without waiting for another completion");
            runningSecond.Lease!.RecordCompletion(TimeSpan.FromMilliseconds(1000), true);
            runningSecond.Lease.Dispose();
            runningThird.Lease!.RecordCompletion(TimeSpan.FromMilliseconds(1000), true);
            runningThird.Lease.Dispose();
            var oldLimit = strategy.Snapshot()[0].Limit;
            var active = new List<IRequestExecutionLease>();
            for (var i = 0; i < oldLimit; i++) active.Add((await strategy.AcquireAsync(Context(), default)).Lease!);
            var waiting = strategy.AcquireAsync(Context(), default).AsTask();
            tuningClock.Advance(20); strategy.Tick();
            Check(strategy.Snapshot()[0].Limit < oldLimit && !waiting.IsCompleted,
                "Lowering limit must preserve running work and stop new grants");
            active[0].Dispose();
            Check(!waiting.IsCompleted, "Must wait until running count is strictly below lowered limit");
            for (var i = 1; i < active.Count; i++) active[i].Dispose();
            (await waiting).Lease!.Dispose();
            Check(strategy.Snapshot()[0].Inflight == 0, "Changing limits must not leak permits");
        }
        var feedbackClock = new ManualClock();
        var feedbackOptions = Settings(); feedbackOptions.Cinnamon.EnablePi = true;
        feedbackOptions.Cinnamon.QueueTimeoutMs = 2000;
        using (var strategy = new CinnamonLoadSheddingStrategy(Options.Create(feedbackOptions), feedbackClock, () => .5))
        {
            var running = await strategy.AcquireAsync(Context(), default);
            var pending = strategy.AcquireAsync(Context(RequestPriority.Low), default).AsTask();
            feedbackClock.Advance(500); strategy.Tick();
            Check(strategy.Snapshot()[0].RejectionRatio > 0, "Queued arrivals must feed the PI controller");
            running.Lease!.Dispose(); (await pending).Lease!.Dispose();
            feedbackClock.Advance(500); strategy.Tick();
            feedbackClock.Advance(500); strategy.Tick();
            Near(strategy.Snapshot()[0].RejectionRatio, 0, "Idle capacity must unwind rejection");
        }
        await Middleware();
        var queueClock = new ManualClock();
        using (var strategy = new CinnamonLoadSheddingStrategy(Options.Create(Settings()), queueClock, () => .5))
        {
            var held = await strategy.AcquireAsync(Context(), default);
            var observer = new RecordingObserver();
            var middleware = new SvalinnMiddleware(_ => { queueClock.Advance(25); return Task.CompletedTask; },
                new Resolver(), new SystemStateAggregator([]), strategy, new InflightRequestCounter(),
                [observer], Options.Create(Settings()), queueClock);
            var http = new DefaultHttpContext();
            var waiting = middleware.InvokeAsync(http);
            queueClock.Advance(50);
            held.Lease!.Dispose();
            await waiting;
            Near(observer.Last!.DecisionLatency.TotalMilliseconds, 0, "Decision overhead must exclude queue waiting");
            Check(observer.Last.State.TryGet("cinnamon_queue_wait_ms", out var wait) && wait == 50, "Queue waiting is recorded separately");
            queueClock.Advance(25); strategy.Tick();
            Near(strategy.Snapshot()[0].SampleLatencyMs, 25, "Execution latency must exclude prior queue time");
        }
        await Lifecycle();
        Console.WriteLine("PASS: Cinnamon priority FIFO, queue bounds, deadlines, cancellation races, shutdown, groups, PI, Vegas, covariance and middleware");
    }

    private static void Controllers()
    {
        var distribution = new PriorityDistribution(400);
        for (var i = 0; i < 100; i++) foreach (var priority in Enum.GetValues<RequestPriority>()) distribution.Observe(priority);
        Near(distribution.Probability(RequestPriority.Low, .1), .4, "Partial rejection within lowest tier");
        Near(distribution.Probability(RequestPriority.Low, .3), 1, "Lower tier rejected first");
        Near(distribution.Probability(RequestPriority.Normal, .3), .2, "Boundary tier probability");
        Near(distribution.Probability(RequestPriority.Critical, .3), 0, "Protect higher tiers before lower tiers");
        for (var i = 0; i < 400; i++) distribution.Observe(RequestPriority.High);
        Near(distribution.Probability(RequestPriority.High, .25), .25, "Arrival history expires and reflects new traffic");
        var o = new CinnamonOptions();
        var pi = new CinnamonPiController(o);
        Near(pi.Update(20, 10, 0, 4, .5, .5), .8, "PI normalized queue growth");
        Check(pi.Update(10, 10, 0, 4, 1, .5) > 0, "Integral retains steady-state rejection");
        Near(pi.Update(0, 0, 4, 4, 2, 1), 0, "Unused execution slots restore admission");
        Check(pi.Update(1000, 0, 0, 1, 3, 1) == 1, "PI output bounded at one");
        Near(pi.Update(0, 0, 0, 4, 40, .5), 0, "Integral history expires");
        Check(double.IsFinite(pi.Error) && double.IsFinite(pi.Integral), "Zero throughput must not divide by zero");

        var tuneOptions = new CinnamonOptions { InitialLimit = 100, MinLimit = 1, MaxLimit = 200, SmoothingFactor = 1 };
        var tuner = new CinnamonAutoTuner(tuneOptions, 1, 0);
        var grown = tuner.Update(10, 100, 1);
        Check(grown > 100, "Vegas grows without estimated queueing");
        tuner.Update(100, 100, 2);
        var reduced = tuner.Update(100, 100, 3);
        Check(reduced < grown && tuner.EstimatedQueue > 0, "Vegas responds to service latency growth");
        var bounded = new CinnamonAutoTuner(tuneOptions, 1, 0);
        Check(bounded.Update(10, 1, 1) <= 10, "App-limited upper bound uses actual concurrency");
        var resetOptions = new CinnamonOptions { InitialLimit = 100, MinLimit = 1, MaxLimit = 200,
            SmoothingFactor = 1, ReferenceResetIntervalMs = 2000, LatencyMinIntervalMs = 100 };
        var reset = new CinnamonAutoTuner(resetOptions, 1, 0);
        reset.Update(10, 10, 1);
        var oldLimit = reset.Limit;
        reset.Update(100, 20, 3);
        Check(reset.Covariance < 0 && reset.Resets == 1 && reset.Limit < oldLimit, "Negative covariance forces decrease on reference reset");
        Near(reset.ReferenceMs, 100, "Reference reset follows observed service latency");
        var floorOptions = new CinnamonOptions { InitialLimit = 2, MinLimit = 2, MaxLimit = 2 };
        var floor = new CinnamonAutoTuner(floorOptions, 2, 0);
        for (var i = 1; i <= 4; i++) floor.Update(10, 2, i);
        Check(floor.Resets == 1 && floor.Limit == 2, "Repeated lower-bound windows reset reference and preserve bounds");
        foreach (var bad in new CinnamonOptions[] { new() { QueueCapacity = -1 }, new() { QueueTimeoutMs = 0 },
            new() { MinLimit = 20 }, new() { IntegralGain = double.NaN }, new() { SmoothingFactor = double.NaN },
            new() { LatencySampleCapacity = 10 }, new() { LatencyPercentile = 0 }, new() { CovarianceWindow = 1 } })
            Check(!bad.IsValid(), "Invalid Cinnamon options accepted");
        try { _ = new CinnamonGroupAttribute("invalid\"group"); throw new Exception("Invalid group accepted"); }
        catch (ArgumentException) { }
    }

    private static async Task Middleware()
    {
        foreach (var mode in new[] { "ok", "error", "throw", "observer", "timeout" })
        {
            var time = new ManualClock();
            var settings = Settings();
            using var strategy = new CinnamonLoadSheddingStrategy(Options.Create(settings), time, () => .5);
            RequestAdmission? held = mode == "timeout" ? await strategy.AcquireAsync(Context(), default) : null;
            var context = new DefaultHttpContext();
            context.Response.Body = new MemoryStream();
            var invoked = false;
            settings.ThrowOnObserverFailure = mode == "observer";
            var middleware = new SvalinnMiddleware(ctx =>
            {
                invoked = true;
                time.Advance(25);
                if (mode == "throw") throw new InvalidOperationException();
                if (mode == "error") ctx.Response.StatusCode = 500;
                return Task.CompletedTask;
            }, new Resolver(), new SystemStateAggregator([]), strategy, new InflightRequestCounter(),
                mode == "observer" ? [new ThrowingObserver()] : [], Options.Create(settings), time);
            var task = middleware.InvokeAsync(context);
            if (mode == "timeout") time.Advance(101);
            try { await task; } catch (InvalidOperationException) when (mode is "throw" or "observer") { }
            held?.Lease?.Dispose();
            Check(strategy.Snapshot()[0].Inflight == 0, "Middleware permit leak: " + mode);
            Check(invoked == (mode is not "observer" and not "timeout"), "Unexpected handler invocation");
            if (mode == "timeout") Check(context.Response.StatusCode == 503 && context.Response.Headers.RetryAfter == "5", "Timeout HTTP response");
            if (mode == "ok")
            {
                time.Advance(25); strategy.Tick();
                Near(strategy.Snapshot()[0].SampleLatencyMs, 25, "Handler time excludes queue waiting");
            }
        }
    }

    private static async Task Lifecycle()
    {
        var settings = Settings(); settings.Cinnamon.EnablePi = true;
        using var strategy = new CinnamonLoadSheddingStrategy(Options.Create(settings));
        await strategy.StartAsync(default);
        var held = await strategy.AcquireAsync(Context(), default);
        var queued = strategy.AcquireAsync(Context(), default).AsTask();
        var updated = false;
        for (var i = 0; i < 50; i++)
        {
            await Task.Delay(5);
            if (strategy.Snapshot()[0].RejectionRatio > 0) { updated = true; break; }
        }
        Check(updated, "Background PI timer did not update");
        await strategy.StopAsync(default);
        Check(!(await queued).Decision.IsAllowed, "Host stop drains waiters");
        held.Lease!.Dispose();
    }

    private sealed class RecordingObserver : ISvalinnObserver
    {
        public SvalinnDecisionTelemetry? Last { get; private set; }
        public ValueTask OnDecisionAsync(SvalinnDecisionTelemetry telemetry, CancellationToken cancellationToken)
        { Last = telemetry; return ValueTask.CompletedTask; }
    }
    private sealed class Resolver : IPriorityResolver
    {
        public ValueTask<RequestPriority> ResolveAsync(HttpContext context, CancellationToken cancellationToken) => ValueTask.FromResult(RequestPriority.Critical);
    }
    private sealed class ThrowingObserver : ISvalinnObserver
    {
        public ValueTask OnDecisionAsync(SvalinnDecisionTelemetry telemetry, CancellationToken cancellationToken) => throw new InvalidOperationException();
    }
    private sealed class ManualClock : TimeProvider
    {
        private long ticks;
        private readonly List<ManualTimer> timers = new();
        public override long TimestampFrequency => 1000;
        public override long GetTimestamp() => ticks;
        public override ITimer CreateTimer(TimerCallback callback, object? state, TimeSpan dueTime, TimeSpan period)
        {
            var timer = new ManualTimer(this, callback, state); timers.Add(timer); timer.Change(dueTime, period); return timer;
        }
        public void Advance(int milliseconds)
        {
            ticks += milliseconds;
            foreach (var timer in timers.ToArray()) timer.Fire(ticks);
        }
        private sealed class ManualTimer(ManualClock clock, TimerCallback callback, object? state) : ITimer
        {
            private long due = long.MaxValue;
            private TimeSpan period;
            private bool disposed;
            public bool Change(TimeSpan dueTime, TimeSpan repeat)
            {
                if (disposed) return false;
                due = dueTime == Timeout.InfiniteTimeSpan ? long.MaxValue : clock.ticks + (long)dueTime.TotalMilliseconds;
                period = repeat; return true;
            }
            public void Fire(long now)
            {
                if (disposed || due > now) return;
                due = period == Timeout.InfiniteTimeSpan ? long.MaxValue : now + (long)period.TotalMilliseconds;
                callback(state);
            }
            public void Dispose() { disposed = true; clock.timers.Remove(this); }
            public ValueTask DisposeAsync() { Dispose(); return ValueTask.CompletedTask; }
        }
    }
}
