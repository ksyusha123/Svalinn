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
    IOptions<SvalinnOptions> options,
    TimeProvider? timeProvider = null)
{
    private readonly TimeProvider clock = timeProvider ?? TimeProvider.System;

    public async Task InvokeAsync(HttpContext context)
    {
        if (context.GetEndpoint()?.Metadata.GetMetadata<SvalinnTelemetryEndpoint>() is not null)
        {
            await next(context);
            return;
        }
        var started = clock.GetTimestamp();
        var priority = await priorityResolver.ResolveAsync(context, context.RequestAborted);
        var state = await stateAggregator.GetCurrentStateAsync(context, context.RequestAborted);
        var loadContext = new LoadSheddingContext(context, priority, state);
        IRequestExecutionLease? executionLease = null;
        var queueWait = TimeSpan.Zero;
        LoadSheddingDecision decision;
        if (strategy is IAsyncLoadSheddingStrategy asyncStrategy)
        {
            var result = await asyncStrategy.AcquireAsync(loadContext, context.RequestAborted);
            decision = result.Decision;
            executionLease = result.Lease;
            queueWait = result.QueueWait;
            state.Set("cinnamon_queue_wait_ms", result.QueueWait.TotalMilliseconds);
        }
        else
        {
            decision = strategy is IConcurrencyLoadSheddingStrategy concurrencyStrategy
                ? concurrencyStrategy.TryAcquire(loadContext, out executionLease)
                : await strategy.DecideAsync(loadContext, context.RequestAborted);
        }
        using var admission = executionLease;

        await NotifyObserversAsync(context, priority, state, decision, started, queueWait);

        if (!decision.IsAllowed)
        {
            context.Response.StatusCode = StatusCodes.Status503ServiceUnavailable;
            context.Response.Headers.RetryAfter = decision.RetryAfterSeconds?.ToString();
            await context.Response.WriteAsync(decision.Reason, context.RequestAborted);
            return;
        }

        context.RequestAborted.ThrowIfCancellationRequested();
        using (inflightRequestCounter.Track())
        {
            var executionStarted = clock.GetTimestamp();
            var completed = false;
            try
            {
                await next(context);
                completed = true;
            }
            finally
            {
                // Ordinary client errors are not evidence of server overload.
                if (!completed || context.RequestAborted.IsCancellationRequested
                    || context.Response.StatusCode < 400 || context.Response.StatusCode >= 500
                    || context.Response.StatusCode == StatusCodes.Status429TooManyRequests)
                {
                    executionLease?.RecordCompletion(
                        clock.GetElapsedTime(executionStarted),
                        completed && !context.RequestAborted.IsCancellationRequested
                            && context.Response.StatusCode is >= 200 and < 400);
                }
            }
        }
    }

    private async Task NotifyObserversAsync(
        HttpContext context,
        RequestPriority priority,
        SystemState.SystemState state,
        LoadSheddingDecision decision,
        long started,
        TimeSpan queueWait)
    {
        var telemetry = new SvalinnDecisionTelemetry(
            context.Request.Method,
            context.Request.Path,
            priority,
            "endpoint-metadata",
            decision.IsAllowed,
            decision.Reason,
            state,
            TimeSpan.FromTicks(Math.Max(0, (clock.GetElapsedTime(started) - queueWait).Ticks)));

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
