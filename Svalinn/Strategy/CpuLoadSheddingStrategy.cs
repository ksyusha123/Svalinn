using Microsoft.Extensions.Options;

namespace Svalinn.Strategy;

public sealed class CpuLoadSheddingStrategy(IOptions<SvalinnOptions> options) : ILoadSheddingStrategy
{
    private readonly ThresholdLoadSheddingStrategy fallback = new(options);

    public async ValueTask<LoadSheddingDecision> DecideAsync(LoadSheddingContext context, CancellationToken cancellationToken)
    {
        if (!context.State.TryGet("cpu_sample_fresh", out var fresh) || fresh != 1
            || !context.State.TryGet("cpu_overloaded", out var overloaded))
        {
            var decision = await fallback.DecideAsync(context, cancellationToken);
            return decision with { Reason = "CPU sample unavailable or stale; inflight fallback: " + decision.Reason };
        }
        var settings = options.Value;
        if (context.Priority == RequestPriority.Critical && settings.AlwaysAllowCriticalRequests)
            return LoadSheddingDecision.Allow("Critical request bypassed CPU load shedding");
        if (overloaded == 0) return LoadSheddingDecision.Allow("CPU capacity is available");
        if (context.Priority >= settings.MinimumPriorityWhenOverloaded)
            return LoadSheddingDecision.Allow("Priority is high enough during CPU overload");
        context.State.TryGet("process_cpu_smoothed_percent", out var cpu);
        return LoadSheddingDecision.Reject($"CPU overloaded: smoothed utilization {cpu:F1}%; {context.Priority} is below {settings.MinimumPriorityWhenOverloaded}", settings.RetryAfterSeconds);
    }
}
