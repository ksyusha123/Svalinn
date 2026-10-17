using System.Diagnostics;
using System.Globalization;
using Svalinn;
using Svalinn.Extensions;

// Модель ограниченной I/O-зависимости: общий пул мест и асинхронное обслуживание.
// Это не измерение диска или БД. Очередь ждёт асинхронно и учитывает отмену клиента.
internal sealed class IoBenchmark(int capacity, int serviceMs)
{
    private readonly SemaphoreSlim slots = new(capacity, capacity);
    private int active, waiting;
    private long completed;

    public static IoBenchmark Configure(IConfiguration configuration)
    {
        var section = configuration.GetSection("IoBenchmark");
        var capacity = section.GetValue("Slots", 4);
        var serviceMs = section.GetValue("ServiceMs", 100);
        if (capacity is < 1 or > 256 || serviceMs is < 1 or > 10000)
            throw new InvalidOperationException("Нужны IoBenchmark:Slots от 1 до 256 и ServiceMs от 1 до 10000.");
        return new IoBenchmark(capacity, serviceMs);
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
        active = Volatile.Read(ref active),
        waiting = Volatile.Read(ref waiting),
        completed = Interlocked.Read(ref completed)
    };

    private async Task<IResult> WorkAsync(HttpContext context, CancellationToken cancellationToken)
    {
        var started = Stopwatch.GetTimestamp();
        Interlocked.Increment(ref waiting);
        try { await slots.WaitAsync(cancellationToken); }
        finally { Interlocked.Decrement(ref waiting); }
        var waitMs = Stopwatch.GetElapsedTime(started).TotalMilliseconds;
        Interlocked.Increment(ref active);
        var serviceStarted = Stopwatch.GetTimestamp();
        try
        {
            await Task.Delay(serviceMs, cancellationToken);
            var elapsedServiceMs = Stopwatch.GetElapsedTime(serviceStarted).TotalMilliseconds;
            context.Response.Headers["X-Benchmark-Io-Wait-Ms"] = waitMs.ToString("F3", CultureInfo.InvariantCulture);
            context.Response.Headers["X-Benchmark-Io-Service-Ms"] = elapsedServiceMs.ToString("F3", CultureInfo.InvariantCulture);
            Interlocked.Increment(ref completed);
            return Results.Ok(new { resource = "io", serviceMs, capacity });
        }
        finally
        {
            Interlocked.Decrement(ref active);
            slots.Release();
        }
    }
}
