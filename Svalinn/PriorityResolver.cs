using Microsoft.AspNetCore.Http;

namespace Svalinn;

public static class PriorityResolver
{
    public static RequestPriority Resolve(HttpContext context)
    {
        var endpoint = context.GetEndpoint();
        var attr = endpoint?.Metadata.GetMetadata<PriorityAttribute>();

        return attr?.Priority ?? RequestPriority.Normal;
    }
}