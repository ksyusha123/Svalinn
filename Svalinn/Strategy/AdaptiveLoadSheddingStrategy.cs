using Microsoft.Extensions.Options;

namespace Svalinn.Strategy;

/// <summary>
/// Latency-based AIMD admission control. A bounded latency sample estimates queueing;
/// periodic low-concurrency probes refresh the reference. This is not a Cinnamon replica.
/// </summary>
public sealed class AdaptiveLoadSheddingStrategy : IConcurrencyLoadSheddingStrategy, IAsyncLoadSheddingStrategy, IDisposable
{
    private readonly object gate = new();
    private readonly AdaptiveLoadOptions settings;
    private readonly int retryAfterSeconds;
    private readonly TimeProvider clock;
    private readonly double[] samples;
    private readonly int[] activeByPriority = new int[4];
    private readonly long[] lastDemand = new long[4];
    private readonly bool[] demandSeen = new bool[4];
    private readonly Queue<double> referenceWindows = new();
    private readonly Queue<double> probeSamples = new();
    private readonly LinkedList<Waiter>[] queues = [new(), new(), new(), new()];
    private ITimer? queueTimer;
    private int queued;
    private bool disposed;
    private long queueFull, queueTimeouts, queueCancelled;
    private int slowWindows;
    private long latencyBackoffs, failureBackoffs;
    private int sampleCount, sampleIndex, failures, completed, inflight, limit, savedLimit, generation;
    private bool saturated, probing;
    private long windowStarted, lastProbe;
    private double referenceMs, sampleMs;

    public AdaptiveLoadSheddingStrategy(IOptions<SvalinnOptions> options) : this(options, TimeProvider.System) { }

    internal AdaptiveLoadSheddingStrategy(IOptions<SvalinnOptions> options, TimeProvider clock)
    {
        settings = options.Value.Adaptive;
        if (settings is null || !settings.IsValid()) throw new ArgumentException("Invalid adaptive settings.", nameof(options));
        retryAfterSeconds = options.Value.RetryAfterSeconds;
        this.clock = clock;
        samples = new double[settings.SampleCapacity];
        limit = settings.InitialLimit;
        windowStarted = lastProbe = clock.GetTimestamp();
    }

    public ValueTask<RequestAdmission> AcquireAsync(LoadSheddingContext context, CancellationToken cancellationToken)
    {
        context.HttpContext.RequestAborted.ThrowIfCancellationRequested();
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            if (disposed) return ValueTask.FromResult(Reject("Adaptive is stopping"));
            Update();
            ObserveDemand(context.Priority);
            ExpireWaiters();
            // A new higher-priority request may use a reserved free slot even when
            // lower-priority waiters fill the queue. Same/higher tiers retain FIFO.
            if (inflight < Ceiling(context.Priority) && !HasQueuedAtOrAbove(context.Priority))
            {
                var admission = Grant(context.Priority, clock.GetTimestamp());
                Drain();
                return ValueTask.FromResult(admission);
            }
            Drain();
            saturated = true;
            if (queued >= settings.QueueCapacity)
            {
                queueFull++;
                return ValueTask.FromResult(Reject("Adaptive queue is full"));
            }
            var waiter = new Waiter(context.Priority, clock.GetTimestamp());
            waiter.Node = queues[(int)context.Priority].AddLast(waiter);
            queued++;
            // A single timer exists only to wake pending requests: deadlines, reserve expiry,
            // and probe/limit updates must also progress when no handler completes.
            queueTimer ??= clock.CreateTimer(_ => TickQueue(), null, Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
            if (queued == 1) queueTimer.Change(TimeSpan.FromMilliseconds(10), TimeSpan.FromMilliseconds(10));
            Drain();
            return WaitAsync(waiter, cancellationToken, context.HttpContext.RequestAborted);
        }
    }

    private async ValueTask<RequestAdmission> WaitAsync(Waiter waiter, CancellationToken token, CancellationToken aborted)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(token, aborted);
        using var registration = linked.Token.Register(() => Cancel(waiter, linked.Token));
        var admission = await waiter.Completion.Task.ConfigureAwait(false);
        if (linked.IsCancellationRequested)
        {
            admission.Lease?.Dispose();
            linked.Token.ThrowIfCancellationRequested();
        }
        return admission;
    }

    private void ObserveDemand(RequestPriority priority)
    {
        var index = (int)priority;
        lastDemand[index] = clock.GetTimestamp();
        demandSeen[index] = true;
    }

    private RequestAdmission Reject(string reason, TimeSpan wait = default) =>
        new(LoadSheddingDecision.Reject(reason, retryAfterSeconds), null, wait);

    private RequestAdmission Grant(RequestPriority priority, long started)
    {
        inflight++;
        activeByPriority[(int)priority]++;
        if (inflight >= Ceiling(priority)) saturated = true;
        return new(LoadSheddingDecision.Allow("Adaptive capacity is available"),
            new Lease(this, generation, priority), clock.GetElapsedTime(started));
    }

    private void Remove(Waiter waiter)
    {
        queues[(int)waiter.Priority].Remove(waiter.Node!);
        waiter.Node = null;
        queued--;
    }

    private void Cancel(Waiter waiter, CancellationToken token)
    {
        lock (gate)
        {
            if (waiter.Node is null) return;
            Remove(waiter);
            queueCancelled++;
            waiter.Completion.TrySetCanceled(token);
            Drain();
        }
    }

    private void TickQueue()
    {
        lock (gate)
        {
            if (disposed || queued == 0) return;
            Update();
            Drain();
        }
    }

    private bool HasQueuedAtOrAbove(RequestPriority priority)
    {
        for (var tier = (int)priority; tier < queues.Length; tier++)
            if (queues[tier].Count > 0) return true;
        return false;
    }

    private void ExpireWaiters()
    {
        // Expire every tier, even when all execution slots remain occupied.
        foreach (var queue in queues)
        {
            while (queue.First is { } node
                && clock.GetElapsedTime(node.Value.Started).TotalMilliseconds >= settings.QueueTimeoutMs)
            {
                var waiter = node.Value;
                Remove(waiter);
                queueTimeouts++;
                waiter.Completion.TrySetResult(Reject("Adaptive queue timeout", clock.GetElapsedTime(waiter.Started)));
            }
        }
    }

    private void Drain()
    {
        ExpireWaiters();
        for (var tier = queues.Length - 1; tier >= 0; tier--)
        {
            while (queues[tier].First is { } node)
            {
                if (inflight >= Ceiling(node.Value.Priority)) return;
                var waiter = node.Value;
                Remove(waiter);
                waiter.Completion.TrySetResult(Grant(waiter.Priority, waiter.Started));
            }
        }
        if (queued == 0) queueTimer?.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
    }

    public void Dispose()
    {
        lock (gate)
        {
            if (disposed) return;
            disposed = true;
            queueTimer?.Dispose();
            foreach (var queue in queues)
                while (queue.First is { } node)
                {
                    var waiter = node.Value;
                    Remove(waiter);
                    waiter.Completion.TrySetResult(Reject("Adaptive is stopping", clock.GetElapsedTime(waiter.Started)));
                }
        }
    }

    // Preview only; middleware uses AcquireAsync to reserve capacity or wait.
    public ValueTask<LoadSheddingDecision> DecideAsync(LoadSheddingContext context, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            if (!disposed) { Update(); Drain(); }
            return ValueTask.FromResult(Decision(context.Priority));
        }
    }

    public LoadSheddingDecision TryAcquire(LoadSheddingContext context, out IRequestExecutionLease? lease)
    {
        context.HttpContext.RequestAborted.ThrowIfCancellationRequested();
        lock (gate)
        {
            if (disposed) { lease = null; return LoadSheddingDecision.Reject("Adaptive is stopping", retryAfterSeconds); }
            Update();
            ObserveDemand(context.Priority);
            ExpireWaiters();
            var priorityIndex = (int)context.Priority;
            var decision = Decision(context.Priority);
            lease = null;
            if (!decision.IsAllowed) { saturated = true; Drain(); return decision; }
            inflight++;
            activeByPriority[priorityIndex]++;
            if (inflight >= Ceiling(context.Priority)) saturated = true;
            lease = new Lease(this, generation, context.Priority);
            Drain();
            return decision;
        }
    }

    private int Ceiling(RequestPriority priority)
    {
        if (settings.EnablePriorityBorrowing && !HasHigherPriorityDemand(priority)) return limit;
        var fraction = priority switch
        {
            RequestPriority.Low => settings.LowPriorityFraction,
            RequestPriority.Normal => settings.NormalPriorityFraction,
            RequestPriority.High => settings.HighPriorityFraction,
            _ => 1.0
        };
        return Math.Max(1, (int)Math.Floor(limit * fraction));
    }

    private bool HasHigherPriorityDemand(RequestPriority priority)
    {
        var now = clock.GetTimestamp();
        for (var index = (int)priority + 1; index < activeByPriority.Length; index++)
            if (queues[index].Count > 0 || activeByPriority[index] > 0 || (demandSeen[index]
                && clock.GetElapsedTime(lastDemand[index], now).TotalMilliseconds < settings.PriorityDemandWindowMs))
                return true;
        return false;
    }

    private LoadSheddingDecision Decision(RequestPriority priority) => !disposed && !HasQueuedAtOrAbove(priority) && inflight < Ceiling(priority)
        ? LoadSheddingDecision.Allow("Adaptive capacity is available")
        : LoadSheddingDecision.Reject($"Adaptive limit: {inflight}/{limit} active; priority ceiling {Ceiling(priority)}", retryAfterSeconds);

    private void Release(int requestGeneration, RequestPriority priority, TimeSpan? duration, bool succeeded)
    {
        lock (gate)
        {
            inflight--;
            activeByPriority[(int)priority]--;
            // Requests admitted before a probe must not contaminate its reference measurement.
            if (requestGeneration != generation || duration is null)
            {
                if (!disposed) { Update(); Drain(); }
                return;
            }
            completed++;
            if (!succeeded) failures++;
            else
            {
                samples[sampleIndex] = Math.Max(0.001, duration.Value.TotalMilliseconds);
                sampleIndex = (sampleIndex + 1) % samples.Length;
                sampleCount = Math.Min(sampleCount + 1, samples.Length);
            }
            if (!disposed) { Update(); Drain(); }
        }
    }

    private void ResetWindow(long now)
    {
        windowStarted = now;
        sampleCount = sampleIndex = failures = completed = 0;
        saturated = false;
    }

    private void Update()
    {
        var now = clock.GetTimestamp();
        if (!probing && clock.GetElapsedTime(lastProbe, now).TotalMilliseconds >= settings.ProbeIntervalMs)
        {
            savedLimit = limit;
            limit = settings.MinLimit;
            probing = true;
            generation++;
            slowWindows = 0;
            ResetWindow(now);
            return;
        }
        var age = clock.GetElapsedTime(windowStarted, now).TotalMilliseconds;
        if (age < settings.UpdateIntervalMs) return;
        if (probing)
        {
            if (sampleCount < settings.ProbeMinSamples && age < settings.ProbeMaxDurationMs) return;
        }
        else if (failures == 0 && sampleCount < settings.MinSamples && age < settings.MaxSampleAgeMs) return;
        if (sampleCount > 0)
        {
            var sorted = samples.AsSpan(0, sampleCount).ToArray();
            Array.Sort(sorted);
            sampleMs = sorted[(int)Math.Ceiling(sorted.Length * 0.9) - 1];
        }
        if (probing)
        {
            // Pool short probes until they provide a full-sized reference estimate.
            for (var index = 0; index < sampleCount; index++) probeSamples.Enqueue(samples[index]);
            if (probeSamples.Count >= settings.MinSamples)
            {
                var sorted = probeSamples.Order().ToArray();
                AddReference(sorted[(int)Math.Ceiling(sorted.Length * .9) - 1]);
                probeSamples.Clear();
            }
            limit = savedLimit;
            probing = false;
            lastProbe = now;
            generation++;
        }
        else if (failures > 0)
        {
            if (Decrease()) failureBackoffs++;
            slowWindows = 0;
        }
        else if (sampleCount >= settings.MinSamples)
        {
            // Bootstrap from several full windows, never from a lucky minimum.
            var initializing = referenceMs == 0;
            if (initializing) AddReference(sampleMs);
            var slow = referenceMs > 0 && sampleMs > LatencyThresholdMs;
            if (slow)
            {
                slowWindows++;
                if (slowWindows >= settings.ConsecutiveSlowWindows)
                {
                    if (Decrease()) latencyBackoffs++;
                    slowWindows = 0;
                }
            }
            else
            {
                slowWindows = 0;
                // Saturated windows must not teach the baseline to follow a queue.
                if (!initializing && referenceMs > 0 && !saturated) AddReference(sampleMs);
                if (saturated)
                    limit = Math.Min(settings.MaxLimit, limit + 1);
            }
        }
        else slowWindows = 0; // Sparse, partial windows are not evidence of sustained latency.
        ResetWindow(now);
    }

    private double LatencyThresholdMs => referenceMs == 0 ? 0
        : referenceMs + Math.Max(referenceMs * settings.LatencyTolerance, settings.MinLatencyIncreaseMs);

    private void AddReference(double percentileMs)
    {
        referenceWindows.Enqueue(percentileMs);
        if (referenceWindows.Count > settings.ReferenceWindowCount) referenceWindows.Dequeue();
        if (referenceWindows.Count < settings.ReferenceWindowCount) return;
        var ordered = referenceWindows.Order().ToArray();
        referenceMs = ordered.Length % 2 == 1 ? ordered[ordered.Length / 2]
            : (ordered[ordered.Length / 2 - 1] + ordered[ordered.Length / 2]) / 2;
    }

    private bool Decrease()
    {
        var previous = limit;
        limit = Math.Max(settings.MinLimit, Math.Min(limit - 1, (int)Math.Floor(limit * settings.DecreaseFactor)));
        return limit < previous;
    }

    public AdaptiveLoadSnapshot Snapshot()
    {
        lock (gate) return new(limit, inflight, referenceMs, sampleMs, probing)
        {
            Queued = queued, QueueFull = queueFull, QueueTimeouts = queueTimeouts, QueueCancelled = queueCancelled,
            LatencyThresholdMs = LatencyThresholdMs, SlowWindows = slowWindows,
            WindowSamples = sampleCount, ReferenceWindows = referenceWindows.Count,
            LatencyBackoffs = latencyBackoffs, FailureBackoffs = failureBackoffs
        };
    }

    private sealed class Waiter(RequestPriority priority, long started)
    {
        public RequestPriority Priority { get; } = priority;
        public long Started { get; } = started;
        public LinkedListNode<Waiter>? Node { get; set; }
        public TaskCompletionSource<RequestAdmission> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed class Lease(AdaptiveLoadSheddingStrategy owner, int generation, RequestPriority priority) : IRequestExecutionLease
    {
        private int released;
        private TimeSpan? duration;
        private bool succeeded;
        public void RecordCompletion(TimeSpan elapsed, bool success) { duration = elapsed; succeeded = success; }
        public void Dispose()
        {
            if (Interlocked.Exchange(ref released, 1) == 0) owner.Release(generation, priority, duration, succeeded);
        }
    }
}

public sealed record AdaptiveLoadSnapshot(int Limit, int Inflight, double ReferenceLatencyMs, double SampleLatencyMs, bool Probing)
{
    public int Queued { get; init; }
    public long QueueFull { get; init; }
    public long QueueTimeouts { get; init; }
    public long QueueCancelled { get; init; }
    public double LatencyThresholdMs { get; init; }
    public int SlowWindows { get; init; }
    public int WindowSamples { get; init; }
    public int ReferenceWindows { get; init; }
    public long LatencyBackoffs { get; init; }
    public long FailureBackoffs { get; init; }
}
