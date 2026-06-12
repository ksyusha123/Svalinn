using Microsoft.AspNetCore.Http;

namespace Svalinn.Priority.PriorityResolvers;

public interface IPriorityResolver
{
    ValueTask<RequestPriority> ResolveAsync(HttpContext context, CancellationToken cancellationToken);
}
