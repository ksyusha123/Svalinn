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
            var decision = Decision(context.Priority);
            lease = null;
            if (!decision.IsAllowed) { saturated = true; return decision; }
            inflight++;
            if (inflight >= Ceiling(context.Priority)) saturated = true;
            lease = new Lease(this, generation);
            return decision;
        }
    }

    private int Ceiling(RequestPriority priority)
    {
        var fraction = priority switch
        {
            RequestPriority.Low => settings.LowPriorityFraction,
            RequestPriority.Normal => settings.NormalPriorityFraction,
            RequestPriority.High => settings.HighPriorityFraction,
            _ => 1.0
        };
        return Math.Max(1, (int)Math.Floor(limit * fraction));
    }

    private LoadSheddingDecision Decision(RequestPriority priority) => inflight < Ceiling(priority)
        ? LoadSheddingDecision.Allow("Adaptive capacity is available")
        : LoadSheddingDecision.Reject($"Adaptive limit: {inflight}/{limit} active; priority ceiling {Ceiling(priority)}", retryAfterSeconds);

    private void Release(int requestGeneration, TimeSpan? duration, bool succeeded)
    {
        lock (gate)
        {
            inflight--;
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
            ResetWindow(now);
            return;
        }
        var age = clock.GetElapsedTime(windowStarted, now).TotalMilliseconds;
        if (age < settings.UpdateIntervalMs || (completed < settings.MinSamples && age < settings.MaxSampleAgeMs)) return;
        if (sampleCount > 0)
        {
            var sorted = samples.AsSpan(0, sampleCount).ToArray();
            Array.Sort(sorted);
            sampleMs = sorted[(int)Math.Ceiling(sorted.Length * 0.9) - 1];
        }
        if (probing)
        {
            if (sampleCount > 0) referenceMs = sampleMs;
            limit = savedLimit;
            probing = false;
            lastProbe = now;
            generation++;
        }
        else if (completed > 0)
        {
            if (sampleCount > 0 && (referenceMs == 0 || sampleMs < referenceMs)) referenceMs = sampleMs;
            if (failures > 0 || (sampleCount > 0 && sampleMs > referenceMs * (1 + settings.LatencyTolerance)))
                limit = Math.Max(settings.MinLimit, Math.Min(limit - 1, (int)Math.Floor(limit * settings.DecreaseFactor)));
            else if (sampleCount > 0 && saturated)
                limit = Math.Min(settings.MaxLimit, limit + 1);
        }
        ResetWindow(now);
    }

    public AdaptiveLoadSnapshot Snapshot()
    {
        lock (gate) return new(limit, inflight, referenceMs, sampleMs, probing);
    }

    private sealed class Lease(AdaptiveLoadSheddingStrategy owner, int generation) : IRequestExecutionLease
    {
        private int released;
        private TimeSpan? duration;
        private bool succeeded;
        public void RecordCompletion(TimeSpan elapsed, bool success) { duration = elapsed; succeeded = success; }
        public void Dispose()
        {
            if (Interlocked.Exchange(ref released, 1) == 0) owner.Release(generation, duration, succeeded);
        }
    }
}

public sealed record AdaptiveLoadSnapshot(int Limit, int Inflight, double ReferenceLatencyMs, double SampleLatencyMs, bool Probing);
