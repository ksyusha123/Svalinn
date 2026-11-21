using Svalinn;
using Svalinn.Extensions;

var builder = WebApplication.CreateBuilder(args);

// Ресурсный эксперимент включается явно; delay-стенд доступен всегда.
var delayBenchmark = DelayBenchmark.Configure(builder.Configuration);
var cpuBenchmark = CpuBenchmark.Configure(builder.Configuration);
var ioBenchmark = cpuBenchmark is null ? null : IoBenchmark.Configure(builder.Configuration);

var svalinnEnabled = builder.Configuration.GetValue("Svalinn:Enabled", true);
if (svalinnEnabled)
{
    builder.Services.AddSvalinn(options => builder.Configuration.GetSection("Svalinn").Bind(options));
}

var app = builder.Build();

ioBenchmark?.MapTelemetry(app);
if (svalinnEnabled)
{
    app.UseSvalinn();
    app.MapSvalinnMetrics();
}

cpuBenchmark?.MapEndpoints(app);
ioBenchmark?.MapEndpoints(app);

delayBenchmark.MapEndpoints(app);

app.MapGet("/", () => Results.Ok(new
{
    service = "Svalinn benchmark API",
    svalinnEnabled,
    resourceBenchmarkEnabled = cpuBenchmark is not null,
    delayInfo = "/benchmark/delay/info",
    delaySlo = "/benchmark/delay/slo",
    cpuInfo = cpuBenchmark is not null ? "/benchmark/cpu/info" : null,
    telemetry = ioBenchmark is not null ? "/benchmark/telemetry" : null
}));

app.Run();
