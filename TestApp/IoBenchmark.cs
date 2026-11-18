using System.Diagnostics;
using System.Globalization;
using Svalinn;
using Svalinn.Extensions;

// Модель ограниченной I/O-зависимости: общий пул мест и асинхронное обслуживание.
// Это не измерение диска или БД. Очередь ждёт асинхронно и учитывает отмену клиента.
internal sealed class IoBenchmark(int capacity, int serviceMs, int serviceJitterMs = 50)
{
    private readonly SemaphoreSlim slots = new(capacity, capacity);
    private int active, waiting;
    private long completed;

    public static IoBenchmark Configure(IConfiguration configuration)
    {
        var section = configuration.GetSection("IoBenchmark");
        var capacity = section.GetValue("Slots", 4);
        var serviceMs = section.GetValue("ServiceMs", 150);
        if (capacity is < 1 or > 256 || serviceMs is < 1 or > 10000)
            throw new InvalidOperationException("Нужны IoBenchmark:Slots от 1 до 256 и ServiceMs от 1 до 10000.");
        var jitterMs = section.GetValue("ServiceJitterMs", 50);
        if (jitterMs is < 0 or > 9999)
            throw new InvalidOperationException("IoBenchmark:ServiceJitterMs должен быть от 0 до 9999.");
        return new IoBenchmark(capacity, serviceMs, jitterMs);
    }

    public void MapTelemetry(WebApplication app)
    {
        // Ветка регистрируется перед Svalinn: опрос не увеличивает счётчик активных рабочих запросов.
        app.MapWhen(context => context.Request.Path == "/benchmark/telemetry", branch =>
            branch.Run(async context =>
            {
                if (!HttpMethods.IsGet(context.Request.Method))
                {
                    context.Response.StatusCode = StatusCodes.Status405MethodNotAllowed;
                    return;
                }
                await context.Response.WriteAsJsonAsync(Snapshot(), context.RequestAborted);
            }));
    }

    public void MapEndpoints(WebApplication app)
    {
        app.MapGet("/benchmark/io/critical", WorkAsync).WithPriority(RequestPriority.Critical);
        app.MapGet("/benchmark/io/high", WorkAsync).WithPriority(RequestPriority.High);
        app.MapGet("/benchmark/io/normal", WorkAsync).WithPriority(RequestPriority.Normal);
        app.MapGet("/benchmark/io/low", WorkAsync).WithPriority(RequestPriority.Low);
    }

    // Поля читаются независимо: это приблизительный снимок, а не атомарное состояние пула.
    private object Snapshot() => new
    {
        capacity,
        serviceMs,
        serviceJitterMs,
        active = Volatile.Read(ref active),
        waiting = Volatile.Read(ref waiting),
        completed = Interlocked.Read(ref completed)
    };

    private async Task<IResult> WorkAsync(HttpContext context, CancellationToken cancellationToken)
    {
        var requestServiceMs = serviceMs;
        if (context.Request.Headers.TryGetValue("X-Benchmark-Service-Ms", out var requested))
        {
            if (!int.TryParse(requested, out requestServiceMs) || requestServiceMs is < 1 or > 10000)
                return Results.BadRequest(new { error = "Service time must be 1..10000 ms." });
        }
        // Выбираем стоимость один раз при поступлении, до ожидания I/O-слота.
        requestServiceMs = Random.Shared.Next(
            Math.Max(1, requestServiceMs - serviceJitterMs),
            Math.Min(10000, requestServiceMs + serviceJitterMs) + 1);
        var started = Stopwatch.GetTimestamp();
        Interlocked.Increment(ref waiting);
        try { await slots.WaitAsync(cancellationToken); }
        finally { Interlocked.Decrement(ref waiting); }
        var waitMs = Stopwatch.GetElapsedTime(started).TotalMilliseconds;
        Interlocked.Increment(ref active);
        var serviceStarted = Stopwatch.GetTimestamp();
        try
        {
            await Task.Delay(requestServiceMs, cancellationToken);
            var elapsedServiceMs = Stopwatch.GetElapsedTime(serviceStarted).TotalMilliseconds;
            context.Response.Headers["X-Benchmark-Io-Wait-Ms"] = waitMs.ToString("F3", CultureInfo.InvariantCulture);
            context.Response.Headers["X-Benchmark-Io-Service-Ms"] = elapsedServiceMs.ToString("F3", CultureInfo.InvariantCulture);
            Interlocked.Increment(ref completed);
            return Results.Ok(new { resource = "io", serviceMs = requestServiceMs, capacity });
        }
        finally
        {
            Interlocked.Decrement(ref active);
            slots.Release();
        }
    }
}
