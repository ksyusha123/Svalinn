using System.Diagnostics;
using System.Security.Cryptography;
using Svalinn;
using Svalinn.Extensions;

// Искусственная работа создаёт реальную конкуренцию за CPU. Нельзя заменять её циклом до истечения времени:
// при перегрузке это уменьшило бы объём полезной работы на запрос.
internal sealed class CpuBenchmark(int iterations, int minWorkerThreads)
{
    public static CpuBenchmark? Configure(IConfiguration configuration)
    {
        var section = configuration.GetSection("CpuBenchmark");
        if (!section.GetValue("Enabled", false)) return null;

        var iterations = section.GetValue("Iterations", 100_000);
        var workers = section.GetValue("MinWorkerThreads", 16);
        var affinity = section.GetValue<long>("AffinityMask", 0);
        if (iterations is < 1_000 or > 10_000_000 || workers is < 1 or > 256 || affinity <= 0)
            throw new InvalidOperationException("Для CPU-эксперимента нужны Iterations от 1000 до 10000000, MinWorkerThreads от 1 до 256 и положительная AffinityMask.");
        if (!OperatingSystem.IsWindows())
            throw new PlatformNotSupportedException("Первый вариант стенда использует привязку к процессорам Windows.");

        using var process = Process.GetCurrentProcess();
        process.ProcessorAffinity = (nint)affinity;
        ThreadPool.GetMinThreads(out _, out var completionPortThreads);
        if (!ThreadPool.SetMinThreads(workers, completionPortThreads))
            throw new InvalidOperationException("Не удалось настроить рабочие потоки стенда.");
        return new CpuBenchmark(iterations, workers);
    }

    public void MapEndpoints(WebApplication app)
    {
        app.MapGet("/benchmark/cpu/info", () =>
        {
            using var process = Process.GetCurrentProcess();
            return Results.Ok(new
            {
                iterations, minWorkerThreads,
                affinityMask = OperatingSystem.IsWindows() ? process.ProcessorAffinity.ToInt64() : 0,
                algorithm = "PBKDF2-SHA256, результат 32 байта, одинаковые входные данные и объём работы для всех приоритетов",
                processId = process.Id
            });
        }).WithPriority(RequestPriority.Critical);

        app.MapGet("/benchmark/cpu/critical", Work).WithPriority(RequestPriority.Critical);
        app.MapGet("/benchmark/cpu/high", Work).WithPriority(RequestPriority.High);
        app.MapGet("/benchmark/cpu/normal", Work).WithPriority(RequestPriority.Normal);
        app.MapGet("/benchmark/cpu/low", Work).WithPriority(RequestPriority.Low);
    }

    private IResult Work(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Span<byte> result = stackalloc byte[32];
        Rfc2898DeriveBytes.Pbkdf2("svalinn-cpu-benchmark"u8, "fixed-benchmark-salt"u8,
            result, iterations, HashAlgorithmName.SHA256);
        return Results.Ok(new { checksum = Convert.ToHexString(result), iterations });
    }
}
