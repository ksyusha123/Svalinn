using Microsoft.AspNetCore.Http;

namespace Svalinn;

public class SvalinnMiddleware(RequestDelegate next)
{
    private static int currentRequests;
    private const int maxConcurrentRequests = 5;

    public async Task InvokeAsync(HttpContext context)
    {
        var priority = PriorityResolver.Resolve(context);

        if (currentRequests >= maxConcurrentRequests)
        {
            if (priority < RequestPriority.High)
            {
                context.Response.StatusCode = StatusCodes.Status503ServiceUnavailable;
                await context.Response.WriteAsync("Svalinn: request shed due to overload");
                return;
            }
        }

        try
        {
            Interlocked.Increment(ref currentRequests);
            await next(context);
        }
        finally
        {
            Interlocked.Decrement(ref currentRequests);
        }
    }
}