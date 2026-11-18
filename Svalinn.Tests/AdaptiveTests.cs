using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Options;
using Svalinn;
using Svalinn.Observers;
using Svalinn.Priority.PriorityResolvers;
using Svalinn.Strategy;
using Svalinn.SystemState;
using Svalinn.SystemState.Providers;

internal static class AdaptiveTests
{
    private static void Check(bool value, string message)
    {
        if (!value) throw new Exception(message);
    }

    private static LoadSheddingContext Context(RequestPriority priority = RequestPriority.Critical) =>
        new(new DefaultHttpContext(), priority, new Svalinn.SystemState.SystemState());

    public static async Task RunAsync()
    {
        await BorrowingChecks();
        await RobustLatencyChecks();
        var time = new ManualClock();
        var options = new SvalinnOptions { Adaptive = new AdaptiveLoadOptions
        {
            InitialLimit = 8, MaxLimit = 12, MinSamples = 1, UpdateIntervalMs = 100,
            EnablePriorityBorrowing = false,
            ReferenceWindowCount = 1, ConsecutiveSlowWindows = 1, MinLatencyIncreaseMs = 0,
            ProbeMinSamples = 1, ProbeMaxDurationMs = 500,
            MaxSampleAgeMs = 500, ProbeIntervalMs = 1000
        }};
        var adaptive = new AdaptiveLoadSheddingStrategy(Options.Create(options), time);
        var held = new List<IRequestExecutionLease>();
        for (var i = 0; i < 4; i++)
        {
            Check(adaptive.TryAcquire(Context(RequestPriority.Low), out var lease).IsAllowed, "Low must fit below reserve");
            held.Add(lease!);
        }
        Check(!adaptive.TryAcquire(Context(RequestPriority.Low), out _).IsAllowed, "Low reserve");
        Check(adaptive.TryAcquire(Context(RequestPriority.Normal), out var normal).IsAllowed, "Normal can use reserve");
        held.Add(normal!);
        Check(adaptive.TryAcquire(Context(), out var critical).IsAllowed, "Critical can use reserve");
        held.Add(critical!);
        foreach (var lease in held) { lease.RecordCompletion(TimeSpan.FromMilliseconds(10), true); lease.Dispose(); lease.Dispose(); }
        Check(adaptive.Snapshot().Inflight == 0, "Dispose must be idempotent");
        time.Advance(100);
        _ = await adaptive.DecideAsync(Context(), default);
        Check(adaptive.Snapshot().Limit == 9, "Healthy saturated window must grow");
        Check(adaptive.Snapshot().ReferenceLatencyMs == 10, "Reference latency");
        adaptive.TryAcquire(Context(), out var slow);
        slow!.RecordCompletion(TimeSpan.FromMilliseconds(100), true);
        slow.Dispose();
        time.Advance(100);
        _ = await adaptive.DecideAsync(Context(), default);
        Check(adaptive.Snapshot().Limit == 7, "Queueing delay must reduce limit");
        adaptive.TryAcquire(Context(), out var failed);
        failed!.RecordCompletion(TimeSpan.FromMilliseconds(1), false);
        failed.Dispose();
        time.Advance(100);
        _ = await adaptive.DecideAsync(Context(), default);
        Check(adaptive.Snapshot().Limit == 5, "Failure-only window must reduce limit");
        Check(adaptive.Snapshot().SampleLatencyMs == 100, "Failures must not become fast latency samples");
        time.Advance(700);
        adaptive.TryAcquire(Context(), out var probe);
        Check(adaptive.Snapshot().Probing && adaptive.Snapshot().Limit == 1, "Periodic probe must limit concurrency");
        Check(!adaptive.TryAcquire(Context(), out _).IsAllowed, "Critical must obey hard limit even with legacy bypass enabled");
        probe!.RecordCompletion(TimeSpan.FromMilliseconds(30), true);
        probe.Dispose();
        time.Advance(100);
        _ = await adaptive.DecideAsync(Context(), default);
        Check(!adaptive.Snapshot().Probing && adaptive.Snapshot().ReferenceLatencyMs == 30 && adaptive.Snapshot().Limit == 5,
            "Probe must refresh baseline and restore bounded limit");
        time.Advance(1000);
        _ = await adaptive.DecideAsync(Context(), default);
        time.Advance(500);
        _ = await adaptive.DecideAsync(Context(), default);
        Check(!adaptive.Snapshot().Probing && adaptive.Snapshot().ReferenceLatencyMs == 30, "Empty probe preserves reference");

        var parallel = new AdaptiveLoadSheddingStrategy(Options.Create(options), new ManualClock());
        var leases = new System.Collections.Concurrent.ConcurrentBag<IRequestExecutionLease>();
        Parallel.For(0, 200, _ =>
        {
            if (parallel.TryAcquire(Context(), out var lease).IsAllowed) leases.Add(lease!);
        });
        Check(leases.Count == 8 && parallel.Snapshot().Inflight == 8, "Parallel admission must not oversubscribe");
        foreach (var lease in leases) lease.Dispose();
        Check(parallel.Snapshot().Inflight == 0, "All parallel permits released");

        foreach (var invalid in new AdaptiveLoadOptions[]
        {
            new() { InitialLimit = 0 }, new() { MinLimit = 5 }, new() { MaxLimit = 2 },
            new() { LatencyTolerance = double.NaN }, new() { DecreaseFactor = 1 },
            new() { LowPriorityFraction = .95 }, new() { SampleCapacity = 2 },
            new() { UpdateIntervalMs = 0 }, new() { ProbeIntervalMs = 1 },
            new() { PriorityDemandWindowMs = 0 }, new() { PriorityDemandWindowMs = 60001 },
            new() { ReferenceWindowCount = 0 }, new() { ConsecutiveSlowWindows = 0 },
            new() { MinLatencyIncreaseMs = double.NaN }, new() { ProbeMinSamples = 0 },
            new() { ProbeMaxDurationMs = 1 }
        }) Check(!invalid.IsValid(), "Invalid adaptive options accepted");

        var boundsClock = new ManualClock();
        var boundedOptions = new SvalinnOptions { Adaptive = new AdaptiveLoadOptions
        {
            InitialLimit = 2, MaxLimit = 3, MinSamples = 1, UpdateIntervalMs = 10,
            MaxSampleAgeMs = 10, ProbeIntervalMs = 1000
        }};
        var bounded = new AdaptiveLoadSheddingStrategy(Options.Create(boundedOptions), boundsClock);
        for (var window = 0; window < 8; window++)
        {
            var batch = new List<IRequestExecutionLease>();
            while (bounded.TryAcquire(Context(), out var lease).IsAllowed) batch.Add(lease!);
            foreach (var lease in batch) { lease.RecordCompletion(TimeSpan.FromMilliseconds(10), true); lease.Dispose(); }
            boundsClock.Advance(10);
            _ = await bounded.DecideAsync(Context(), default);
        }
        Check(bounded.Snapshot().Limit == 3, "Growth must respect MaxLimit");
        for (var window = 0; window < 8; window++)
        {
            bounded.TryAcquire(Context(), out var lease);
            lease!.RecordCompletion(TimeSpan.FromMilliseconds(100), true);
            lease.Dispose();
            boundsClock.Advance(10);
            _ = await bounded.DecideAsync(Context(), default);
        }
        Check(bounded.Snapshot().Limit == 1, "Backoff must respect MinLimit");
        boundsClock.Advance(100);
        _ = await bounded.DecideAsync(Context(), default);
        Check(bounded.Snapshot().Limit == 1, "Empty windows must not grow");

        var probeClock = new ManualClock();
        var isolated = new AdaptiveLoadSheddingStrategy(Options.Create(options), probeClock);
        isolated.TryAcquire(Context(), out var oldRequest);
        probeClock.Advance(1000);
        _ = await isolated.DecideAsync(Context(), default);
        oldRequest!.RecordCompletion(TimeSpan.FromMilliseconds(999), true);
        oldRequest.Dispose();
        isolated.TryAcquire(Context(), out var newRequest);
        newRequest!.RecordCompletion(TimeSpan.FromMilliseconds(20), true);
        newRequest.Dispose();
        probeClock.Advance(100);
        _ = await isolated.DecideAsync(Context(), default);
        Check(isolated.Snapshot().ReferenceLatencyMs == 20, "Pre-probe completions must not contaminate reference");

        await MiddlewareChecks(options);
        Console.WriteLine("PASS: adaptive growth, backoff, failures, probes, priority reserves, atomic admission and middleware cleanup");
    }

    private static async Task RobustLatencyChecks()
    {
        var time = new ManualClock();
        var options = new SvalinnOptions { Adaptive = new AdaptiveLoadOptions { MaxLimit = 4 } };
        var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(options), time);
        async Task Window(double ms, int count = 30, bool saturate = false, bool success = true, int age = 500)
        {
            var remaining = count;
            while (remaining > 0)
            {
                var batch = new List<IRequestExecutionLease>();
                var size = Math.Min(remaining, saturate ? strategy.Snapshot().Limit : 1);
                for (var index = 0; index < size; index++)
                {
                    Check(strategy.TryAcquire(Context(), out var lease).IsAllowed, "Controlled window must acquire capacity");
                    batch.Add(lease!);
                }
                foreach (var lease in batch) { lease.RecordCompletion(TimeSpan.FromMilliseconds(ms), success); lease.Dispose(); }
                remaining -= size;
            }
            time.Advance(age);
            _ = await strategy.DecideAsync(Context(), default);
        }
        await Window(200);
        Check(strategy.Snapshot().ReferenceLatencyMs == 0, "One window cannot initialize reference");
        await Window(160);
        await Window(200);
        Check(strategy.Snapshot().ReferenceLatencyMs == 200 && strategy.Snapshot().LatencyThresholdMs == 240,
            "Median must reject one lucky low window and retain absolute headroom");
        await Window(205);
        Check(strategy.Snapshot().Limit == 4 && strategy.Snapshot().LatencyBackoffs == 0,
            "Ordinary service jitter must not cause a reduction");
        var reference = strategy.Snapshot().ReferenceLatencyMs;
        await Window(400, saturate: true);
        Check(strategy.Snapshot().Limit == 4 && strategy.Snapshot().SlowWindows == 1,
            "One slow full window must not reduce limit");
        Check(strategy.Snapshot().ReferenceLatencyMs == reference, "Saturated slow windows must not raise reference");
        await Window(400, saturate: true);
        Check(strategy.Snapshot().Limit == 3 && strategy.Snapshot().LatencyBackoffs == 1,
            "Two consecutive slow windows must back off");
        await Window(200, saturate: true);
        Check(strategy.Snapshot().Limit == 4 && strategy.Snapshot().SlowWindows == 0, "Healthy saturation must recover");
        await Window(900, count: 10, age: 5000);
        Check(strategy.Snapshot().Limit == 4 && strategy.Snapshot().SlowWindows == 0,
            "Sparse partial windows must not become evidence of persistent latency");
        await Window(1, count: 1, success: false);
        Check(strategy.Snapshot().Limit == 3 && strategy.Snapshot().FailureBackoffs == 1,
            "Failures must not wait for 30 successes or two slow windows");
        time.Advance(60000);
        _ = await strategy.DecideAsync(Context(), default);
        Check(strategy.Snapshot().Probing, "Periodic probe must still run");
        await Window(900, count: 10);
        Check(!strategy.Snapshot().Probing && strategy.Snapshot().ReferenceLatencyMs == reference,
            "One short probe must not overwrite baseline");
        time.Advance(60000);
        _ = await strategy.DecideAsync(Context(), default);
        time.Advance(2000);
        _ = await strategy.DecideAsync(Context(), default);
        Check(!strategy.Snapshot().Probing && strategy.Snapshot().ReferenceLatencyMs == reference,
            "Empty short probe must stop and preserve baseline");
    }

    private static async Task BorrowingChecks()
    {
        foreach (var priority in new[] { RequestPriority.Low, RequestPriority.Normal, RequestPriority.High })
        {
            var time = new ManualClock();
            var options = new SvalinnOptions { Adaptive = new AdaptiveLoadOptions
            {
                InitialLimit = 4, MaxLimit = 4, PriorityDemandWindowMs = 500
            }};
            var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(options), time);
            // Preview must not consume capacity or register actual demand.
            _ = await strategy.DecideAsync(Context(), default);
            var held = new List<IRequestExecutionLease>();
            Parallel.For(0, 40, _ =>
            {
                if (strategy.TryAcquire(Context(priority), out var lease).IsAllowed)
                    lock (held) held.Add(lease!);
            });
            Check(held.Count == 4 && strategy.Snapshot().Inflight == 4, "Idle reservations must be borrowable atomically");
            Check(!strategy.TryAcquire(Context(), out var rejected).IsAllowed && rejected is null,
                "Borrowing cannot oversubscribe or preempt existing requests");
            held[0].Dispose();
            held[0].Dispose();
            Check(!strategy.TryAcquire(Context(priority), out _).IsAllowed,
                "Rejected Critical arrival must stop lower-priority refills");
            Check(strategy.TryAcquire(Context(), out var critical).IsAllowed, "Critical must use freed capacity immediately");
            time.Advance(501);
            held[1].Dispose();
            held[2].Dispose();
            // Keep enough occupancy to reach this class's reserved threshold.
            if (priority == RequestPriority.Low)
                Check(!strategy.TryAcquire(Context(priority), out _).IsAllowed, "Active Critical must keep reserve after demand expiry");
            critical!.Dispose();
            Check(strategy.TryAcquire(Context(priority), out var resumed).IsAllowed, "Borrowing resumes when higher demand expires");
            resumed!.Dispose();
            foreach (var lease in held) lease.Dispose();
            Check(strategy.Snapshot().Inflight == 0, "Borrowed leases must clean up idempotently");

            var expiry = new AdaptiveLoadSheddingStrategy(Options.Create(options), time);
            expiry.TryAcquire(Context(), out var transient);
            transient!.Dispose();
            time.Advance(499);
            held.Clear();
            while (expiry.TryAcquire(Context(priority), out var lease).IsAllowed) held.Add(lease!);
            Check(held.Count == Math.Max(1, (int)Math.Floor(4 * (priority == RequestPriority.Low ? .5 : priority == RequestPriority.Normal ? .75 : .9))),
                "Recent completed higher demand must retain its reserve");
            time.Advance(1);
            Check(expiry.TryAcquire(Context(priority), out var borrowed).IsAllowed, "Reservation must expire at the configured boundary");
            borrowed!.Dispose();
            foreach (var lease in held) lease.Dispose();
        }
    }

    private static async Task MiddlewareChecks(SvalinnOptions options)
    {
        foreach (var mode in new[] { "ok", "client", "error", "throw", "cancel", "observer" })
        {
            var time = new ManualClock();
            var adaptive = new AdaptiveLoadSheddingStrategy(Options.Create(options), time);
            var context = new DefaultHttpContext();
            using var cancellation = new CancellationTokenSource();
            context.RequestAborted = cancellation.Token;
            var invoked = false;
            var counter = new InflightRequestCounter();
            var middlewareOptions = new SvalinnOptions { ThrowOnObserverFailure = mode == "observer" };
            var middleware = new SvalinnMiddleware(async ctx =>
            {
                invoked = true;
                await Task.Yield();
                if (mode == "throw") throw new InvalidOperationException("handler");
                if (mode == "cancel") { cancellation.Cancel(); throw new OperationCanceledException(); }
                if (mode == "error") ctx.Response.StatusCode = 500;
                if (mode == "client") ctx.Response.StatusCode = 404;
            }, new Resolver(), new SystemStateAggregator(Array.Empty<ISystemStateProvider>()), adaptive,
                counter, mode == "observer" ? new ISvalinnObserver[] { new ThrowingObserver() } : Array.Empty<ISvalinnObserver>(),
                Options.Create(middlewareOptions));
            try { await middleware.InvokeAsync(context); }
            catch (Exception e) when ((mode == "throw" || mode == "observer") && e is InvalidOperationException
                || mode == "cancel" && e is OperationCanceledException) { }
            Check(adaptive.Snapshot().Inflight == 0, "Middleware leaked a permit: " + mode);
            Check(invoked == (mode != "observer"), "Observer failure must not run handler");
            time.Advance(100);
            _ = await adaptive.DecideAsync(Context(), default);
            Check(mode is "error" or "throw" or "cancel" ? adaptive.Snapshot().Limit < 8 : adaptive.Snapshot().Limit == 8,
                "Middleware completion classification: " + mode);
        }
    }

    private sealed class ManualClock : TimeProvider
    {
        private long ticks;
        public override long TimestampFrequency => 1000;
        public override long GetTimestamp() => ticks;
        public void Advance(int milliseconds) => ticks += milliseconds;
    }
    private sealed class Resolver : IPriorityResolver
    {
        public ValueTask<RequestPriority> ResolveAsync(HttpContext context, CancellationToken cancellationToken) =>
            ValueTask.FromResult(RequestPriority.Critical);
    }
    private sealed class ThrowingObserver : ISvalinnObserver
    {
        public ValueTask OnDecisionAsync(SvalinnDecisionTelemetry telemetry, CancellationToken cancellationToken) =>
            throw new InvalidOperationException("observer");
    }
}
