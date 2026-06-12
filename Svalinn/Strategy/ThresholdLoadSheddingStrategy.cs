using Microsoft.Extensions.Options;

namespace Svalinn.Strategy;

public sealed class ThresholdLoadSheddingStrategy(IOptions<SvalinnOptions> options) : ILoadSheddingStrategy
{
    public ValueTask<LoadSheddingDecision> DecideAsync(LoadSheddingContext context, CancellationToken cancellationToken)
    {
        var settings = options.Value;
        var priority = context.Priority;
        var state = context.State;
        state.TryGet("inflight_requests", out var inflightRequests);

        if (priority == RequestPriority.Critical && settings.AlwaysAllowCriticalRequests)
        {
            return ValueTask.FromResult(LoadSheddingDecision.Allow("Critical request bypassed load shedding"));
        }

        if (inflightRequests < settings.MaxConcurrentRequests)
        {
            return ValueTask.FromResult(LoadSheddingDecision.Allow("Capacity is available"));
        }

        if (priority >= settings.MinimumPriorityWhenOverloaded)
        {
            return ValueTask.FromResult(LoadSheddingDecision.Allow("Priority is high enough during overload"));
        }

        var reason =
            $"Overloaded: {inflightRequests}/{settings.MaxConcurrentRequests} active requests; " +
            $"{priority} is below {settings.MinimumPriorityWhenOverloaded}";

        return ValueTask.FromResult(LoadSheddingDecision.Reject(reason, settings.RetryAfterSeconds));
    }
}
