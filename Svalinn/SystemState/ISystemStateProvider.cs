using Microsoft.AspNetCore.Http;

namespace Svalinn.SystemState;

public interface ISystemStateProvider
{
    ValueTask CollectAsync(
        SystemState state,
        HttpContext context,
        CancellationToken cancellationToken);
}
