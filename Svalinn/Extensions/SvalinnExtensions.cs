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
            .Validate(value => value.Adaptive is not null && value.Adaptive.IsValid(), "Invalid adaptive settings.")
            .Validate(value => value.Cinnamon is not null && value.Cinnamon.IsValid(), "Invalid Cinnamon settings.")
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
                LoadSheddingStrategyKind.Adaptive => new AdaptiveLoadSheddingStrategy(settings),
                LoadSheddingStrategyKind.Cinnamon => new CinnamonLoadSheddingStrategy(settings),
                LoadSheddingStrategyKind.Inflight => new ThresholdLoadSheddingStrategy(settings),
                _ => throw new InvalidOperationException("Unknown Svalinn strategy.")
            };
        });
        services.AddHostedService<CinnamonLifetimeService>();
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

    public static TBuilder WithCinnamonGroup<TBuilder>(this TBuilder builder, string name)
        where TBuilder : IEndpointConventionBuilder
    {
        builder.WithMetadata(new CinnamonGroupAttribute(name));
        return builder;
    }

    public static IEndpointRouteBuilder MapSvalinnMetrics(
        this IEndpointRouteBuilder endpoints,
        string pattern = "/metrics")
    {
        endpoints.MapGet(pattern, async (SvalinnMetricsObserver metrics, SystemStateAggregator stateAggregator, ILoadSheddingStrategy strategy, HttpContext context) =>
        {
            var state = await stateAggregator.GetCurrentStateAsync(context, context.RequestAborted);
            var cpuMetrics = state.Metrics.Where(metric => metric.Key.StartsWith("cpu_", StringComparison.Ordinal)
                || metric.Key.StartsWith("process_cpu_", StringComparison.Ordinal));
            var text = metrics.ToPrometheusText();
            foreach (var metric in cpuMetrics)
                text += $"# TYPE svalinn_{metric.Key} gauge\nsvalinn_{metric.Key} {metric.Value.ToString(CultureInfo.InvariantCulture)}\n";
            if (strategy is AdaptiveLoadSheddingStrategy adaptive)
            {
                var snapshot = adaptive.Snapshot();
                var gauges = new Dictionary<string, double>
                {
                    ["adaptive_limit"] = snapshot.Limit,
                    ["adaptive_inflight"] = snapshot.Inflight,
                    ["adaptive_reference_latency_ms"] = snapshot.ReferenceLatencyMs,
                    ["adaptive_sample_latency_ms"] = snapshot.SampleLatencyMs,
                    ["adaptive_probing"] = snapshot.Probing ? 1 : 0
                };
                foreach (var metric in gauges)
                    text += $"# TYPE svalinn_{metric.Key} gauge\nsvalinn_{metric.Key} {metric.Value.ToString(CultureInfo.InvariantCulture)}\n";
            }
            if (strategy is CinnamonLoadSheddingStrategy cinnamon)
                text += cinnamon.ToPrometheusText();
            return Results.Text(text, "text/plain");
        }).WithMetadata(new SvalinnTelemetryEndpoint());

        return endpoints;
    }
}
