using Microsoft.AspNetCore.Http;

namespace Svalinn.SystemState.Providers;

public sealed class InflightRequestsStateProvider(IInflightRequestCounter counter) : ISystemStateProvider
{
    public ValueTask CollectAsync(
        SystemState state,
        HttpContext context,
        CancellationToken cancellationToken)
    {
        state.Set("inflight_requests", counter.Current);
        return ValueTask.CompletedTask;
    }
}
