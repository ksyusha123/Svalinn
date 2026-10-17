using Svalinn;
using Svalinn.Extensions;

// Независимая асинхронная задержка без общего ограничения ресурса.
internal sealed class DelayBenchmark(DelayBenchmark.DelayEndpoint[] endpoints, IConfigurationSection slo)
{
    public static DelayBenchmark Configure(IConfiguration configuration)
    {
        var section = configuration.GetSection("DelayBenchmark");
        return new DelayBenchmark(
        [
            Create("critical", RequestPriority.Critical, "CriticalDelayMs", 800),
            Create("high", RequestPriority.High, "HighDelayMs", 1600),
            Create("normal", RequestPriority.Normal, "NormalDelayMs", 800),
            Create("low", RequestPriority.Low, "LowDelayMs", 3000)
        ], section.GetSection("Slo"));

        DelayEndpoint Create(string name, RequestPriority priority, string key, int fallback)
        {
            var delayMs = section.GetValue(key, fallback);
            if (delayMs is < 0 or > 30_000)
                throw new InvalidOperationException($"DelayBenchmark:{key} должен быть от 0 до 30000 мс.");
            return new DelayEndpoint($"/benchmark/delay/{name}", priority, delayMs);
        }
    }

    public void MapEndpoints(WebApplication app)
    {
        app.MapGet("/benchmark/delay/info", () => Results.Ok(new
        {
            workload = "delay",
            processId = Environment.ProcessId,
            endpoints = endpoints.Select(endpoint => new
            {
                method = "GET", path = endpoint.Path,
                priority = endpoint.Priority.ToString(), delayMs = endpoint.DelayMs
            })
        }));

        app.MapGet("/benchmark/delay/slo", () => Results.Ok(new
        {
            objective = "Protect Critical and High requests during overload in delay scenarios.",
            overloadSignal = "inflight_requests >= Svalinn:MaxConcurrentRequests",
            svalinn = new
            {
                enabled = app.Configuration.GetValue("Svalinn:Enabled", true),
                maxConcurrentRequests = app.Configuration.GetValue("Svalinn:MaxConcurrentRequests", new SvalinnOptions().MaxConcurrentRequests),
                minimumPriorityWhenOverloaded = app.Configuration.GetValue("Svalinn:MinimumPriorityWhenOverloaded", "High"),
                alwaysAllowCriticalRequests = app.Configuration.GetValue("Svalinn:AlwaysAllowCriticalRequests", true)
            },
            targets = new
            {
                criticalAvailability = slo.GetValue("CriticalAvailability", "99.9%"),
                highAvailability = slo.GetValue("HighAvailability", "99%"),
                normalAvailability = slo.GetValue("NormalAvailability", "best effort during overload"),
                lowAvailability = slo.GetValue("LowAvailability", "may be shed first during overload"),
                criticalP95LatencyMs = slo.GetValue("CriticalP95LatencyMs", 1200),
                highP95LatencyMs = slo.GetValue("HighP95LatencyMs", 2500)
            }
        }));

        foreach (var endpoint in endpoints)
        {
            app.MapGet(endpoint.Path, async (CancellationToken cancellationToken) =>
            {
                await Task.Delay(endpoint.DelayMs, cancellationToken);
                return Results.Ok(new { resource = "delay", delayMs = endpoint.DelayMs });
            }).WithPriority(endpoint.Priority);
        }
    }

    internal sealed record DelayEndpoint(string Path, RequestPriority Priority, int DelayMs);
}
