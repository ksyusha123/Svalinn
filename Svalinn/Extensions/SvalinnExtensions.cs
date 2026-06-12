using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.DependencyInjection;
using Svalinn.Observers;
using Svalinn.Priority.PriorityResolvers;
using Svalinn.Strategy;
using Svalinn.SystemState;
using Svalinn.SystemState.Providers;

namespace Svalinn.Extensions;

public static class SvalinnExtensions
{
    public static IServiceCollection AddSvalinn(
        this IServiceCollection services,
        Action<SvalinnOptions>? configure = null)
    {
        if (configure is not null)
        {
            services.Configure(configure);
        }

        services.AddSingleton<IInflightRequestCounter, InflightRequestCounter>();
        services.AddSingleton<ISystemStateProvider, InflightRequestsStateProvider>();
        services.AddSingleton<SystemStateAggregator>();
        services.AddSingleton<IPriorityResolver, EndpointMetadataPriorityResolver>();
        services.AddSingleton<ILoadSheddingStrategy, ThresholdLoadSheddingStrategy>();
        services.AddSingleton<ISvalinnObserver, SvalinnMetricsObserver>();
        services.AddSingleton<ISvalinnObserver, LoggingSvalinnObserver>();

        return services;
    }

    public static IApplicationBuilder UseSvalinn(this IApplicationBuilder app)
    {
        return app.UseMiddleware<SvalinnMiddleware>();
    }
}
