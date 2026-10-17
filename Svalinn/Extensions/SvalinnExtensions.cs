using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using System.Globalization;
using Svalinn.Observers;
using Svalinn.Priority;
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

        services.AddOptions<SvalinnOptions>()
            .Validate(value => Enum.IsDefined(value.Strategy), "Unknown Svalinn strategy.")
            .Validate(value => value.Cpu is not null && value.Cpu.IsValid(),
                "Invalid CPU settings: interval 50..60000 ms, alpha (0,1], 0 <= recovery < overload <= 100, positive CPU capacity.")
            .ValidateOnStart();
        services.AddSingleton<ProcessCpuStateProvider>();
        services.AddHostedService(sp => sp.GetRequiredService<ProcessCpuStateProvider>());
        services.AddSingleton<ISystemStateProvider>(sp => sp.GetRequiredService<ProcessCpuStateProvider>());
        services.AddSingleton<IInflightRequestCounter, InflightRequestCounter>();
        services.AddSingleton<ISystemStateProvider, InflightRequestsStateProvider>();
        services.AddSingleton<SystemStateAggregator>();
        services.AddSingleton<IPriorityResolver, EndpointMetadataPriorityResolver>();
        services.AddSingleton<ILoadSheddingStrategy>(sp =>
        {
            var settings = sp.GetRequiredService<IOptions<SvalinnOptions>>();
            return settings.Value.Strategy switch
            {
                LoadSheddingStrategyKind.Cpu => new CpuLoadSheddingStrategy(settings),
                LoadSheddingStrategyKind.Inflight => new ThresholdLoadSheddingStrategy(settings),
                _ => throw new InvalidOperationException("Unknown Svalinn strategy.")
            };
        });
        services.AddSingleton<SvalinnMetricsObserver>();
        services.AddSingleton<ISvalinnObserver>(sp => sp.GetRequiredService<SvalinnMetricsObserver>());
        services.AddSingleton<ISvalinnObserver, LoggingSvalinnObserver>();

        return services;
    }

    public static IApplicationBuilder UseSvalinn(this IApplicationBuilder app)
    {
        return app.UseMiddleware<SvalinnMiddleware>();
    }

    public static TBuilder WithPriority<TBuilder>(this TBuilder builder, RequestPriority priority)
        where TBuilder : IEndpointConventionBuilder
    {
        builder.WithMetadata(new PriorityAttribute(priority));
        return builder;
    }

    public static IEndpointRouteBuilder MapSvalinnMetrics(
        this IEndpointRouteBuilder endpoints,
        string pattern = "/metrics")
    {
        endpoints.MapGet(pattern, async (SvalinnMetricsObserver metrics, SystemStateAggregator stateAggregator, HttpContext context) =>
        {
            var state = await stateAggregator.GetCurrentStateAsync(context, context.RequestAborted);
            var cpuMetrics = state.Metrics.Where(metric => metric.Key.StartsWith("cpu_", StringComparison.Ordinal)
                || metric.Key.StartsWith("process_cpu_", StringComparison.Ordinal));
            var text = metrics.ToPrometheusText();
            foreach (var metric in cpuMetrics)
                text += $"# TYPE svalinn_{metric.Key} gauge\nsvalinn_{metric.Key} {metric.Value.ToString(CultureInfo.InvariantCulture)}\n";
            return Results.Text(text, "text/plain");
        });

        return endpoints;
    }
}
