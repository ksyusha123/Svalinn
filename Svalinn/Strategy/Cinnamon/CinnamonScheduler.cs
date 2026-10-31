namespace Svalinn.Strategy.Cinnamon;

internal sealed class CinnamonScheduler
{
    private readonly object gate = new();
    private readonly CinnamonOptions options;
    private readonly int retryAfter;
    private readonly TimeProvider clock;
    private readonly Func<double> random;
    private readonly LinkedList<Waiter>[] queues = [new(), new(), new(), new()];
    private readonly PriorityDistribution priorities;
    private readonly CinnamonPiController pi;
    private readonly CinnamonAutoTuner tuner;
    private readonly double[] samples;
    private int queued, active, peak, sampleCount;
    private long samplesSeen, entered, dispatched, piEntered, piDispatched;
    private long lastPi, latencyStarted, rejected, full, expired, cancelled, succeeded, failed;
    private double waitTotalMs;
    private long waitCount;
    private bool stopped;

    public CinnamonScheduler(string name, CinnamonOptions options, int minimum, int retryAfter, TimeProvider clock, Func<double> random)
    {
        Name = name;
        this.options = options;
        this.retryAfter = retryAfter;
        this.clock = clock;
        this.random = random;
        priorities = new(options.PrioritySampleCapacity);
        pi = new(options);
        tuner = new(options, minimum, Seconds(clock.GetTimestamp()));
        samples = new double[options.LatencySampleCapacity];
        lastPi = latencyStarted = clock.GetTimestamp();
    }

    public string Name { get; }
    private double Seconds(long timestamp) => timestamp / (double)clock.TimestampFrequency;
    private RequestAdmission Reject(string reason, TimeSpan wait = default) => new(LoadSheddingDecision.Reject(reason, retryAfter), null, wait);

    public ValueTask<RequestAdmission> AcquireAsync(RequestPriority priority, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        lock (gate)
        {
            if (stopped) return ValueTask.FromResult(Reject("Cinnamon is stopping"));
            priorities.Observe(priority);
            var probability = priorities.Probability(priority, options.EnablePi ? pi.Ratio : 0);
            if (probability >= 1 || probability > 0 && random() < probability)
            {
                rejected++;
                return ValueTask.FromResult(Reject("Cinnamon priority rejection"));
            }
            Drain();
            if (active < tuner.Limit && queued == 0)
            {
                entered++;
                return ValueTask.FromResult(Grant(clock.GetTimestamp()));
            }
            if (queued >= options.QueueCapacity)
            {
                full++;
                return ValueTask.FromResult(Reject("Cinnamon queue is full"));
            }
            var waiter = new Waiter(priority, clock.GetTimestamp());
            waiter.Node = queues[(int)priority].AddLast(waiter);
            queued++;
            entered++;
            return WaitAsync(waiter, token);
        }
    }

    private async ValueTask<RequestAdmission> WaitAsync(Waiter waiter, CancellationToken token)
    {
        using var timer = clock.CreateTimer(_ => Remove(waiter, false, default), null,
            TimeSpan.FromMilliseconds(options.QueueTimeoutMs), Timeout.InfiniteTimeSpan);
        using var registration = token.Register(() => Remove(waiter, true, token));
        var admission = await waiter.Completion.Task.ConfigureAwait(false);
        // Cancellation can race with granting a permit; do not run an abandoned handler.
        if (token.IsCancellationRequested)
        {
            admission.Lease?.Dispose();
            token.ThrowIfCancellationRequested();
        }
        return admission;
    }

    private void Remove(Waiter waiter, bool clientCancelled, CancellationToken token)
    {
        lock (gate)
        {
            if (waiter.Node is null) return;
            queues[(int)waiter.Priority].Remove(waiter.Node);
            waiter.Node = null;
            queued--;
            if (clientCancelled)
            {
                cancelled++;
                waiter.Completion.TrySetCanceled(token);
            }
            else
            {
                expired++;
                waiter.Completion.TrySetResult(Reject("Cinnamon queue timeout", clock.GetElapsedTime(waiter.Enqueued)));
            }
        }
    }

    private RequestAdmission Grant(long enqueued)
    {
        active++;
        peak = Math.Max(peak, active);
        dispatched++;
        var wait = clock.GetElapsedTime(enqueued);
        waitTotalMs += wait.TotalMilliseconds;
        waitCount++;
        return new(LoadSheddingDecision.Allow("Cinnamon execution permit granted"), new Lease(this), wait);
    }

    private void Drain()
    {
        if (stopped) return;
        for (var tier = queues.Length - 1; tier >= 0 && active < tuner.Limit; tier--)
        {
            while (queues[tier].First is { } node && active < tuner.Limit)
            {
                var waiter = node.Value;
                queues[tier].RemoveFirst();
                waiter.Node = null;
                queued--;
                if (clock.GetElapsedTime(waiter.Enqueued).TotalMilliseconds >= options.QueueTimeoutMs)
                {
                    expired++;
                    waiter.Completion.TrySetResult(Reject("Cinnamon queue timeout", clock.GetElapsedTime(waiter.Enqueued)));
                    continue;
                }
                waiter.Completion.TrySetResult(Grant(waiter.Enqueued));
            }
        }
    }

    private void Release(TimeSpan? duration, bool success)
    {
        lock (gate)
        {
            active--;
            if (duration is not null)
            {
                if (success)
                {
                    succeeded++;
                    var sample = Math.Max(.001, duration.Value.TotalMilliseconds);
                    samplesSeen++;
                    // Uniform reservoir sampling keeps memory bounded without preferring the window tail.
                    if (sampleCount < samples.Length) samples[sampleCount++] = sample;
                    else
                    {
                        var index = (long)(random() * samplesSeen);
                        if (index < samples.Length) samples[index] = sample;
                    }
                }
                else failed++;
            }
            Drain();
        }
    }

    public void Tick()
    {
        lock (gate)
        {
            if (stopped) return;
            var now = clock.GetTimestamp();
            var piAge = clock.GetElapsedTime(lastPi, now).TotalMilliseconds;
            if (piAge >= options.PiIntervalMs)
            {
                if (options.EnablePi)
                    pi.Update(entered - piEntered, dispatched - piDispatched, Math.Max(0, tuner.Limit - active),
                        tuner.Limit, Seconds(now), piAge / 1000);
                piEntered = entered;
                piDispatched = dispatched;
                lastPi = now;
            }
            var sampleAge = clock.GetElapsedTime(latencyStarted, now).TotalMilliseconds;
            if (sampleAge >= options.LatencyMaxIntervalMs
                || sampleAge >= options.LatencyMinIntervalMs && samplesSeen >= options.LatencyMinSamples)
            {
                if (sampleCount > 0)
                {
                    var sorted = samples.AsSpan(0, sampleCount).ToArray();
                    Array.Sort(sorted);
                    tuner.Update(sorted[(int)Math.Ceiling(sorted.Length * options.LatencyPercentile) - 1], peak, Seconds(now));
                }
                sampleCount = 0;
                samplesSeen = 0;
                peak = active;
                latencyStarted = now;
            }
            Drain();
        }
    }

    public LoadSheddingDecision Preview(RequestPriority priority)
    {
        lock (gate)
        {
            if (stopped) return LoadSheddingDecision.Reject("Cinnamon is stopping", retryAfter);
            if (active >= tuner.Limit && queued >= options.QueueCapacity)
                return LoadSheddingDecision.Reject("Cinnamon queue is full", retryAfter);
            // Admission probabilities are applied only by AcquireAsync, which also records the arrival.
            return LoadSheddingDecision.Allow("Cinnamon admission requires AcquireAsync");
        }
    }

    public void Stop()
    {
        lock (gate)
        {
            stopped = true;
            foreach (var queue in queues)
            {
                foreach (var waiter in queue)
                {
                    waiter.Node = null;
                    waiter.Completion.TrySetResult(Reject("Cinnamon is stopping", clock.GetElapsedTime(waiter.Enqueued)));
                }
                queue.Clear();
            }
            queued = 0;
        }
    }

    public CinnamonSnapshot Snapshot()
    {
        lock (gate) return new(Name, tuner.Limit, active, queued, pi.Ratio, pi.Error, pi.Integral,
            tuner.ReferenceMs, tuner.SampleMs, tuner.EstimatedQueue, tuner.Covariance, tuner.Resets,
            entered, dispatched, rejected, full, expired, cancelled, succeeded, failed,
            waitTotalMs, waitCount, Enumerable.Range(0, 4).Select(i => priorities.Probability((RequestPriority)i, pi.Ratio)).ToArray());
    }

    private sealed class Waiter(RequestPriority priority, long enqueued)
    {
        public RequestPriority Priority { get; } = priority;
        public long Enqueued { get; } = enqueued;
        public LinkedListNode<Waiter>? Node { get; set; }
        public TaskCompletionSource<RequestAdmission> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed class Lease(CinnamonScheduler owner) : IRequestExecutionLease
    {
        private int released;
        private TimeSpan? duration;
        private bool success;
        public void RecordCompletion(TimeSpan elapsed, bool succeeded) { duration = elapsed; success = succeeded; }
        public void Dispose()
        {
            if (Interlocked.Exchange(ref released, 1) == 0) owner.Release(duration, success);
        }
    }
}
