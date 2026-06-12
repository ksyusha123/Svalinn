using Scalar.AspNetCore;
using Svalinn;
using Svalinn.Extensions;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddOpenApi();

var svalinnEnabled = builder.Configuration.GetValue("Svalinn:Enabled", true);
if (svalinnEnabled)
{
    builder.Services.AddSvalinn(options =>
    {
        var section = builder.Configuration.GetSection("Svalinn");
        options.MaxConcurrentRequests = section.GetValue("MaxConcurrentRequests", options.MaxConcurrentRequests);
        options.MinimumPriorityWhenOverloaded = section.GetValue(
            "MinimumPriorityWhenOverloaded",
            options.MinimumPriorityWhenOverloaded);
        options.AlwaysAllowCriticalRequests = section.GetValue(
            "AlwaysAllowCriticalRequests",
            options.AlwaysAllowCriticalRequests);
        options.RetryAfterSeconds = section.GetValue("RetryAfterSeconds", options.RetryAfterSeconds);
    });
}

var app = builder.Build();

if (app.Environment.IsDevelopment())
{
    app.MapOpenApi();
    app.MapScalarApiReference(options =>
    {
        options.Title = "Svalinn Hospital Benchmark API";
        options.OpenApiRoutePattern = "/openapi/v1.json";
    });
}

if (svalinnEnabled)
{
    app.UseSvalinn();
    app.MapSvalinnMetrics();
}

var benchmark = app.Configuration.GetSection("HospitalBenchmark");
var defaultDelayMs = benchmark.GetValue("DefaultDelayMs", 25);
var reportsDelayMs = benchmark.GetValue("ReportsDelayMs", 250);
var emergencyDelayMs = benchmark.GetValue("EmergencyDelayMs", 40);

var modes = new[]
{
    "Svalinn enabled: set Svalinn:Enabled=true",
    "Svalinn disabled: set Svalinn:Enabled=false"
};

app.MapGet("/", () => Results.Ok(new
{
    service = "Svalinn hospital benchmark API",
    svalinnEnabled,
    modes,
    docs = "/scalar/v1",
    openApi = "/openapi/v1.json",
    benchmarkEndpoints = "/hospital/benchmark/endpoints"
}));

app.MapGet("/hospital/benchmark/endpoints", () => Results.Ok(new[]
{
    new HospitalEndpoint("POST", "/hospital/emergency/admissions", "Critical", "Emergency registration must survive overload"),
    new HospitalEndpoint("GET", "/hospital/patients/42/allergies", "Critical", "Medication safety lookup"),
    new HospitalEndpoint("POST", "/hospital/appointments", "High", "Operational appointment booking"),
    new HospitalEndpoint("GET", "/hospital/patients/42", "Normal", "Routine patient profile read"),
    new HospitalEndpoint("GET", "/hospital/reports/daily", "Low", "Analytical report suitable for shedding")
}));

app.MapPost("/hospital/emergency/admissions", async (EmergencyAdmissionRequest request, CancellationToken cancellationToken) =>
    {
        await SimulateWorkAsync(emergencyDelayMs, cancellationToken);

        return Results.Accepted($"/hospital/emergency/admissions/{Guid.NewGuid():N}", new
        {
            triageId = Guid.NewGuid(),
            request.PatientName,
            request.Severity,
            status = "triaged"
        });
    })
    .WithPriority(RequestPriority.Critical);

app.MapGet("/hospital/patients/{patientId:int}/allergies", async (int patientId, CancellationToken cancellationToken) =>
    {
        await SimulateWorkAsync(defaultDelayMs, cancellationToken);

        return Results.Ok(new
        {
            patientId,
            allergies = new[] { "penicillin", "latex" },
            checkedAt = DateTimeOffset.UtcNow
        });
    })
    .WithPriority(RequestPriority.Critical);

app.MapPost("/hospital/appointments", async (AppointmentRequest request, CancellationToken cancellationToken) =>
    {
        await SimulateWorkAsync(defaultDelayMs * 2, cancellationToken);

        return Results.Ok(new
        {
            appointmentId = Guid.NewGuid(),
            request.PatientId,
            request.Department,
            scheduledFor = DateTimeOffset.UtcNow.AddDays(3)
        });
    })
    .WithPriority(RequestPriority.High);

app.MapGet("/hospital/patients/{patientId:int}", async (int patientId, CancellationToken cancellationToken) =>
    {
        await SimulateWorkAsync(defaultDelayMs, cancellationToken);

        return Results.Ok(new
        {
            patientId,
            name = $"Patient {patientId}",
            ward = "General medicine",
            updatedAt = DateTimeOffset.UtcNow
        });
    })
    .WithPriority(RequestPriority.Normal);

app.MapGet("/hospital/reports/daily", async (CancellationToken cancellationToken) =>
    {
        await SimulateWorkAsync(reportsDelayMs, cancellationToken);

        return Results.Ok(new
        {
            reportDate = DateOnly.FromDateTime(DateTime.UtcNow),
            admissions = 128,
            discharges = 119,
            averageWaitMinutes = 34,
            generatedAt = DateTimeOffset.UtcNow
        });
    })
    .WithPriority(RequestPriority.Low);

app.Run();

static async Task SimulateWorkAsync(int milliseconds, CancellationToken cancellationToken)
{
    await Task.Delay(Math.Clamp(milliseconds, 0, 30_000), cancellationToken);
}

public sealed record HospitalEndpoint(string Method, string Path, string Priority, string BenchmarkRole);

public sealed record EmergencyAdmissionRequest(string PatientName, int Severity);

public sealed record AppointmentRequest(int PatientId, string Department);
