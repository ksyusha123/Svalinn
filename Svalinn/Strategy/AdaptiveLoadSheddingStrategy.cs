using Microsoft.Extensions.Options;

namespace Svalinn.Strategy;

/// <summary>
/// Latency-based AIMD admission control. A bounded latency sample estimates queueing;
/// periodic low-concurrency probes refresh the reference. This is not a Cinnamon replica.
/// </summary>
public sealed class AdaptiveLoadSheddingStrategy : IConcurrencyLoadSheddingStrategy
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

    // A decision preview only. Middleware uses TryAcquire to reserve an actual slot.
    public ValueTask<LoadSheddingDecision> DecideAsync(LoadSheddingContext context, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            Update();
            return ValueTask.FromResult(Decision(context.Priority));
        }
    }

    public LoadSheddingDecision TryAcquire(LoadSheddingContext context, out IRequestExecutionLease? lease)
    {
        context.HttpContext.RequestAborted.ThrowIfCancellationRequested();
        lock (gate)
        {
            Update();
            // Record rejected arrivals too, so borrowers yield newly freed slots.
            var priorityIndex = (int)context.Priority;
            lastDemand[priorityIndex] = clock.GetTimestamp();
            demandSeen[priorityIndex] = true;
            var decision = Decision(context.Priority);
            lease = null;
            if (!decision.IsAllowed) { saturated = true; return decision; }
            inflight++;
            activeByPriority[priorityIndex]++;
            if (inflight >= Ceiling(context.Priority)) saturated = true;
            lease = new Lease(this, generation, context.Priority);
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
            if (activeByPriority[index] > 0 || (demandSeen[index]
                && clock.GetElapsedTime(lastDemand[index], now).TotalMilliseconds < settings.PriorityDemandWindowMs))
                return true;
        return false;
    }

    private LoadSheddingDecision Decision(RequestPriority priority) => inflight < Ceiling(priority)
        ? LoadSheddingDecision.Allow("Adaptive capacity is available")
        : LoadSheddingDecision.Reject($"Adaptive limit: {inflight}/{limit} active; priority ceiling {Ceiling(priority)}", retryAfterSeconds);

    private void Release(int requestGeneration, RequestPriority priority, TimeSpan? duration, bool succeeded)
    {
        lock (gate)
        {
            inflight--;
            activeByPriority[(int)priority]--;
            // Requests admitted before a probe must not contaminate its reference measurement.
            if (requestGeneration != generation || duration is null) return;
            completed++;
            if (!succeeded) failures++;
            else
            {
                samples[sampleIndex] = Math.Max(0.001, duration.Value.TotalMilliseconds);
                sampleIndex = (sampleIndex + 1) % samples.Length;
                sampleCount = Math.Min(sampleCount + 1, samples.Length);
            }
            Update();
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
            LatencyThresholdMs = LatencyThresholdMs, SlowWindows = slowWindows,
            WindowSamples = sampleCount, ReferenceWindows = referenceWindows.Count,
            LatencyBackoffs = latencyBackoffs, FailureBackoffs = failureBackoffs
        };
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
    public double LatencyThresholdMs { get; init; }
    public int SlowWindows { get; init; }
    public int WindowSamples { get; init; }
    public int ReferenceWindows { get; init; }
    public long LatencyBackoffs { get; init; }
    public long FailureBackoffs { get; init; }
}
