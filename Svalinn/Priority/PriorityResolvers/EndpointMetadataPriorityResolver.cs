using Microsoft.AspNetCore.Http;

namespace Svalinn.Priority.PriorityResolvers;

public sealed class EndpointMetadataPriorityResolver : IPriorityResolver
{
    public ValueTask<RequestPriority> ResolveAsync(HttpContext context, CancellationToken cancellationToken)
    {
        var endpoint = context.GetEndpoint();
        var attr = endpoint?.Metadata.GetMetadata<PriorityAttribute>();
        var priority = attr?.Priority ?? RequestPriority.Normal;

        return ValueTask.FromResult(priority);
    }
}
