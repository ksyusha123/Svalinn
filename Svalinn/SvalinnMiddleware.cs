using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Options;
using Svalinn.Observers;
using Svalinn.Priority.PriorityResolvers;
using Svalinn.Strategy;
using Svalinn.SystemState;
using Svalinn.SystemState.Providers;

namespace Svalinn;

public sealed class SvalinnMiddleware(
    RequestDelegate next,
    IPriorityResolver priorityResolver,
    SystemStateAggregator stateAggregator,
    ILoadSheddingStrategy strategy,
    IInflightRequestCounter inflightRequestCounter,
    IEnumerable<ISvalinnObserver> observers,
    IOptions<SvalinnOptions> options)
{
    public async Task InvokeAsync(HttpContext context)
    {
        var started = TimeProvider.System.GetTimestamp();
        var priority = await priorityResolver.ResolveAsync(context, context.RequestAborted);
        var state = await stateAggregator.GetCurrentStateAsync(context, context.RequestAborted);
        var decision = await strategy.DecideAsync(
            new LoadSheddingContext(context, priority, state),
            context.RequestAborted);

        await NotifyObserversAsync(context, priority, state, decision, started);

        if (!decision.IsAllowed)
        {
            context.Response.StatusCode = StatusCodes.Status503ServiceUnavailable;
            context.Response.Headers.RetryAfter = decision.RetryAfterSeconds?.ToString();
            await context.Response.WriteAsync(decision.Reason, context.RequestAborted);
            return;
        }

        using (inflightRequestCounter.Track())
        {
            await next(context);
        }
    }

    private async Task NotifyObserversAsync(
        HttpContext context,
        RequestPriority priority,
        SystemState.SystemState state,
        LoadSheddingDecision decision,
        long started)
    {
        var telemetry = new SvalinnDecisionTelemetry(
            context.Request.Method,
            context.Request.Path,
            priority,
            "endpoint-metadata",
            decision.IsAllowed,
            decision.Reason,
            state,
            TimeProvider.System.GetElapsedTime(started));

        foreach (var observer in observers)
        {
            try
            {
                await observer.OnDecisionAsync(telemetry, context.RequestAborted);
            }
            catch when (!options.Value.ThrowOnObserverFailure)
            {
            }
        }
    }
}
