#if API_DOCS
using Scalar.AspNetCore;
#endif
using Svalinn;
using Svalinn.Extensions;

var builder = WebApplication.CreateBuilder(args);

// Ресурсный эксперимент включается явно; delay-стенд доступен всегда.
var delayBenchmark = DelayBenchmark.Configure(builder.Configuration);
var cpuBenchmark = CpuBenchmark.Configure(builder.Configuration);
var ioBenchmark = cpuBenchmark is null ? null : IoBenchmark.Configure(builder.Configuration);

#if API_DOCS
builder.Services.AddOpenApi();
#endif

var svalinnEnabled = builder.Configuration.GetValue("Svalinn:Enabled", true);
if (svalinnEnabled)
{
    builder.Services.AddSvalinn(options => builder.Configuration.GetSection("Svalinn").Bind(options));
}

var app = builder.Build();

#if API_DOCS
if (app.Environment.IsDevelopment())
{
    app.MapOpenApi();
    app.MapScalarApiReference(options =>
    {
        options.Title = "Svalinn Benchmark API";
        options.OpenApiRoutePattern = "/openapi/v1.json";
    });
}
#endif

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
#if API_DOCS
    docs = app.Environment.IsDevelopment() ? "/scalar/v1" : null,
    openApi = app.Environment.IsDevelopment() ? "/openapi/v1.json" : null,
#endif
    delayInfo = "/benchmark/delay/info",
    delaySlo = "/benchmark/delay/slo",
    cpuInfo = cpuBenchmark is not null ? "/benchmark/cpu/info" : null,
    telemetry = ioBenchmark is not null ? "/benchmark/telemetry" : null
}));

app.Run();
