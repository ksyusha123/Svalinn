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
        await QueueChecks();
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
            new() { ProbeMaxDurationMs = 1 }, new() { QueueCapacity = -1 },
            new() { QueueCapacity = 65537 }, new() { QueueTimeoutMs = 0 }
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

    private static async Task QueueChecks()
    {
        SvalinnOptions Settings() => new() { Adaptive = new()
        {
            InitialLimit = 1, MaxLimit = 1, QueueCapacity = 3, QueueTimeoutMs = 100
        }};
        var clock = new ManualClock();
        using (var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(Settings()), clock))
        {
            var running = await strategy.AcquireAsync(Context(RequestPriority.Low), default);
            var low1 = strategy.AcquireAsync(Context(RequestPriority.Low), default).AsTask();
            var low2 = strategy.AcquireAsync(Context(RequestPriority.Low), default).AsTask();
            var critical = strategy.AcquireAsync(Context(), default).AsTask();
            Check(!(await strategy.AcquireAsync(Context(), default)).Decision.IsAllowed, "Bound queue capacity");
            Check(strategy.Snapshot().Queued == 3, "Asynchronous waiting at limit");
            Check(!strategy.TryAcquire(Context(), out _).IsAllowed, "Synchronous admission cannot bypass queue");
            running.Lease!.Dispose();
            var important = await critical.WaitAsync(TimeSpan.FromSeconds(2));
            Check(!low1.IsCompleted && !low2.IsCompleted, "Critical must be scheduled before Low");
            important.Lease!.Dispose();
            var first = await low1.WaitAsync(TimeSpan.FromSeconds(2));
            Check(!low2.IsCompleted, "FIFO within priority");
            first.Lease!.Dispose(); first.Lease.Dispose();
            (await low2.WaitAsync(TimeSpan.FromSeconds(2))).Lease!.Dispose();
            Check(strategy.Snapshot().Inflight == 0 && strategy.Snapshot().Queued == 0, "Queue permits released once");

            running = await strategy.AcquireAsync(Context(), default);
            using var cancellation = new CancellationTokenSource();
            var pending = strategy.AcquireAsync(Context(), cancellation.Token).AsTask();
            cancellation.Cancel();
            try { await pending; throw new Exception("Expected cancellation"); } catch (OperationCanceledException) { }
            Check(strategy.Snapshot().QueueCancelled == 1 && strategy.Snapshot().Queued == 0, "Cancellation removes waiter");
            var timeout = strategy.AcquireAsync(Context(), default).AsTask();
            clock.Advance(101);
            Check(!(await timeout.WaitAsync(TimeSpan.FromSeconds(2))).Decision.IsAllowed, "Queue deadline without completions");
            Check(strategy.Snapshot().QueueTimeouts == 1 && strategy.Snapshot().Queued == 0, "Count timeout");
            running.Lease!.Dispose();
            running = await strategy.AcquireAsync(Context(), default);
            var shutdown = strategy.AcquireAsync(Context(), default).AsTask();
            strategy.Dispose();
            Check(!(await shutdown).Decision.IsAllowed, "Shutdown drains queue");
            running.Lease!.Dispose();
            Check(!(await strategy.AcquireAsync(Context(), default)).Decision.IsAllowed, "No admission after disposal");
        }
        for (var i = 0; i < 100; i++)
        {
            using var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(Settings()));
            var running = await strategy.AcquireAsync(Context(), default);
            using var cancellation = new CancellationTokenSource();
            var pending = strategy.AcquireAsync(Context(), cancellation.Token).AsTask();
            await Task.WhenAll(Task.Run(() => cancellation.Cancel()), Task.Run(() => running.Lease!.Dispose()));
            try { (await pending.WaitAsync(TimeSpan.FromSeconds(2))).Lease?.Dispose(); } catch (OperationCanceledException) { }
            Check(strategy.Snapshot().Inflight == 0 && strategy.Snapshot().Queued == 0, "Cancellation/grant race leaks no permits");
        }
        var disabled = Settings(); disabled.Adaptive.QueueCapacity = 0;
        using (var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(disabled)))
        {
            var running = await strategy.AcquireAsync(Context(), default);
            Check(!(await strategy.AcquireAsync(Context(), default)).Decision.IsAllowed, "Zero capacity disables waiting");
            running.Lease!.Dispose();
        }
        // Reserve expiry must wake pending requests without a new arrival or completion.
        var reserved = Settings(); reserved.Adaptive.InitialLimit = reserved.Adaptive.MaxLimit = 4;
        reserved.Adaptive.PriorityDemandWindowMs = 20;
        using (var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(reserved), clock))
        {
            var critical = await strategy.AcquireAsync(Context(), default);
            critical.Lease!.Dispose();
            var held = new List<IRequestExecutionLease>();
            for (var i = 0; i < 3; i++) held.Add((await strategy.AcquireAsync(Context(RequestPriority.Normal), default)).Lease!);
            var pending = strategy.AcquireAsync(Context(RequestPriority.Normal), default).AsTask();
            Check(!pending.IsCompleted, "Recent Critical demand retains reserve");
            clock.Advance(21);
            (await pending.WaitAsync(TimeSpan.FromSeconds(2))).Lease!.Dispose();
            foreach (var lease in held) lease.Dispose();
        }
        // A full queue of reserve-blocked Low requests must not deny a free slot to Critical.
        foreach (var synchronous in new[] { false, true })
        {
            var settings = Settings();
            settings.Adaptive.InitialLimit = settings.Adaptive.MaxLimit = 4;
            settings.Adaptive.QueueCapacity = 4;
            var time = new ManualClock();
            using var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(settings), time);
            var first = await strategy.AcquireAsync(Context(), default);
            first.Lease!.Dispose();
            var lowRunning = new List<IRequestExecutionLease>();
            for (var i = 0; i < 2; i++) lowRunning.Add((await strategy.AcquireAsync(Context(RequestPriority.Low), default)).Lease!);
            var waiting = Enumerable.Range(0, 4).Select(_ => strategy.AcquireAsync(Context(RequestPriority.Low), default).AsTask()).ToArray();
            Check(strategy.Snapshot().Queued == 4 && strategy.Snapshot().Inflight == 2, "Low queue must be blocked by reserved capacity");
            IRequestExecutionLease? important;
            if (synchronous)
                Check(strategy.TryAcquire(Context(), out important).IsAllowed, "Critical synchronous admission uses its free reserve despite full Low queue");
            else
            {
                var admission = await strategy.AcquireAsync(Context(), default);
                Check(admission.Decision.IsAllowed, "Critical async admission uses its free reserve despite full Low queue");
                important = admission.Lease;
            }
            Check(strategy.Snapshot().Inflight == 3 && strategy.Snapshot().Queued == 4 && waiting.All(task => !task.IsCompleted),
                "Higher-priority admission preserves Low queue and execution bound");
            important!.Dispose();
            foreach (var lease in lowRunning) lease.Dispose();
            foreach (var task in waiting) (await task.WaitAsync(TimeSpan.FromSeconds(2))).Lease?.Dispose();
            Check(strategy.Snapshot().Inflight == 0, "Full Low queue cleanup");
        }
        var ordering = Settings(); ordering.Adaptive.QueueCapacity = 4;
        using (var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(ordering), new ManualClock()))
        {
            var running = await strategy.AcquireAsync(Context(), default);
            var waiting = new Dictionary<RequestPriority, Task<RequestAdmission>>();
            foreach (var priority in new[] { RequestPriority.Low, RequestPriority.Normal, RequestPriority.High, RequestPriority.Critical })
                waiting[priority] = strategy.AcquireAsync(Context(priority), default).AsTask();
            running.Lease!.Dispose();
            foreach (var priority in new[] { RequestPriority.Critical, RequestPriority.High, RequestPriority.Normal, RequestPriority.Low })
            {
                var admission = await waiting[priority].WaitAsync(TimeSpan.FromSeconds(2));
                Check(waiting.Where(pair => pair.Key < priority).All(pair => !pair.Value.IsCompleted), "All four priority tiers must be ordered");
                admission.Lease!.Dispose();
            }
        }
        // A probe lowering the limit must hold waiters; its end wakes them without arrivals.
        var probing = Settings(); probing.Adaptive.InitialLimit = probing.Adaptive.MaxLimit = 4;
        var probeClock = new ManualClock();
        using (var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(probing), probeClock))
        {
            probing.Adaptive.QueueTimeoutMs = 10000;
            var held = new List<IRequestExecutionLease>();
            for (var i = 0; i < 4; i++) held.Add((await strategy.AcquireAsync(Context(), default)).Lease!);
            var waiting = strategy.AcquireAsync(Context(), default).AsTask();
            probeClock.Advance(60001);
            // The old waiter expires; enqueue a fresh waiter during the probe.
            Check(!(await waiting).Decision.IsAllowed, "Expired waiter cannot acquire during probe");
            waiting = strategy.AcquireAsync(Context(), default).AsTask();
            for (var i = 0; i < 3; i++) held[i].Dispose();
            Check(!waiting.IsCompleted && strategy.Snapshot().Limit == 1, "Probe respects reduced limit with older active requests");
            probeClock.Advance(2001);
            (await waiting.WaitAsync(TimeSpan.FromSeconds(2))).Lease!.Dispose();
            held[3].Dispose();
            Check(strategy.Snapshot().Limit == 4 && strategy.Snapshot().Inflight == 0, "Probe restoration wakes queued requests");
        }
        var latencyOptions = Settings(); latencyOptions.Adaptive.MinSamples = 1;
        latencyOptions.Adaptive.ReferenceWindowCount = 1; latencyOptions.Adaptive.UpdateIntervalMs = 10;
        var latencyClock = new ManualClock();
        using (var strategy = new AdaptiveLoadSheddingStrategy(Options.Create(latencyOptions), latencyClock))
        {
            var running = await strategy.AcquireAsync(Context(), default);
            var invoked = false;
            var middleware = new SvalinnMiddleware(_ => { invoked = true; latencyClock.Advance(10); return Task.CompletedTask; },
                new Resolver(), new SystemStateAggregator([]), strategy, new InflightRequestCounter(), [],
                Options.Create(latencyOptions), latencyClock);
            var context = new DefaultHttpContext();
            var waiting = middleware.InvokeAsync(context);
            latencyClock.Advance(80);
            Check(!invoked, "Queued middleware must not run handler before grant");
            running.Lease!.Dispose();
            await waiting.WaitAsync(TimeSpan.FromSeconds(2));
            Check(invoked && strategy.Snapshot().ReferenceLatencyMs == 10, "AIMD samples handler duration excluding queue wait");
            running = await strategy.AcquireAsync(Context(), default);
            context = new DefaultHttpContext();
            waiting = middleware.InvokeAsync(context);
            latencyClock.Advance(101);
            await waiting.WaitAsync(TimeSpan.FromSeconds(2));
            Check(context.Response.StatusCode == 503 && context.Response.Headers.RetryAfter == "5", "Adaptive middleware queue timeout returns 503 and Retry-After");
            running.Lease!.Dispose();
        }
        Console.WriteLine("PASS: Adaptive queue priorities, FIFO, bounds, timeout, cancellation races, reserve expiry and shutdown");
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
