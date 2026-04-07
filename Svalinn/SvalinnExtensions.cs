using Microsoft.AspNetCore.Builder;

namespace Svalinn;

public static class SvalinnExtensions
{
    public static IApplicationBuilder UseSvalinn(this IApplicationBuilder app)
    {
        return app.UseMiddleware<SvalinnMiddleware>();
    }
}