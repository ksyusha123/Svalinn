using Microsoft.AspNetCore.Http;

namespace Svalinn.SystemState;

public sealed class SystemStateAggregator(IEnumerable<ISystemStateProvider> providers)
{
    private readonly IEnumerable<ISystemStateProvider> _providers = providers;

    public async ValueTask<SystemState> GetCurrentStateAsync(
        HttpContext context,
        CancellationToken cancellationToken)
    {
        var state = new SystemState();

        foreach (var provider in _providers)
        {
            await provider.CollectAsync(state, context, cancellationToken);
        }

        return state;
    }
}
