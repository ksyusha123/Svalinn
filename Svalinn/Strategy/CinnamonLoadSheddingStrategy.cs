using System.Diagnostics;
using System.Globalization;
using System.Numerics;
using System.Text;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Options;
using Svalinn.Priority;
using Svalinn.Strategy.Cinnamon;

namespace Svalinn.Strategy;

public sealed class CinnamonLoadSheddingStrategy : BackgroundService, IAsyncLoadSheddingStrategy
{
    private readonly object gate = new();
    private readonly Dictionary<string, CinnamonScheduler> groups = new(StringComparer.Ordinal);
    private readonly CinnamonOptions settings;
    private readonly int retryAfter, minimum;
    private readonly TimeProvider clock;
    private readonly Func<double> random;
    private bool stopped;

    public CinnamonLoadSheddingStrategy(IOptions<SvalinnOptions> options) : this(options, TimeProvider.System, Random.Shared.NextDouble) { }

    internal CinnamonLoadSheddingStrategy(IOptions<SvalinnOptions> options, TimeProvider clock, Func<double> random)
    {
        settings = options.Value.Cinnamon;
        if (settings is null || !settings.IsValid()) throw new ArgumentException("Invalid Cinnamon settings.", nameof(options));
        retryAfter = options.Value.RetryAfterSeconds;
        minimum = settings.MinLimit ?? AvailableProcessors();
        this.clock = clock;
        this.random = random;
    }

    private static int AvailableProcessors()
    {
        var count = Environment.ProcessorCount;
        if (OperatingSystem.IsWindows())
        {
            using var process = Process.GetCurrentProcess();
            count = Math.Min(count, BitOperations.PopCount(unchecked((ulong)process.ProcessorAffinity.ToInt64())));
        }
        return Math.Max(1, count);
    }

    private CinnamonScheduler? GetGroup(LoadSheddingContext context)
    {
        var name = context.HttpContext.GetEndpoint()?.Metadata.GetMetadata<CinnamonGroupAttribute>()?.Name ?? "default";
        lock (gate)
        {
            if (stopped) return null;
            if (groups.TryGetValue(name, out var existing)) return existing;
            if (groups.Count >= settings.MaxGroups) return null;
            var group = new CinnamonScheduler(name, settings, minimum, retryAfter, clock, random);
            groups.Add(name, group);
            return group;
        }
    }

    public async ValueTask<RequestAdmission> AcquireAsync(LoadSheddingContext context, CancellationToken cancellationToken)
    {
        if (!Enum.IsDefined(context.Priority)) throw new ArgumentOutOfRangeException(nameof(context));
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, context.HttpContext.RequestAborted);
        linked.Token.ThrowIfCancellationRequested();
        var group = GetGroup(context);
        if (group is null) return new(LoadSheddingDecision.Reject("Cinnamon stopped or group limit reached", retryAfter), null, TimeSpan.Zero);
        return await group.AcquireAsync(context.Priority, linked.Token).ConfigureAwait(false);
    }

    public ValueTask<LoadSheddingDecision> DecideAsync(LoadSheddingContext context, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var group = GetGroup(context);
        return ValueTask.FromResult(group?.Preview(context.Priority)
            ?? LoadSheddingDecision.Reject("Cinnamon stopped or group limit reached", retryAfter));
    }

    internal void Tick()
    {
        CinnamonScheduler[] current;
        lock (gate) current = groups.Values.ToArray();
        foreach (var group in current) group.Tick();
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(settings.PiIntervalMs), clock);
        try { while (await timer.WaitForNextTickAsync(stoppingToken)) Tick(); }
        catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { }
        finally { StopAdmissions(); }
    }

    private void StopAdmissions()
    {
        lock (gate)
        {
            stopped = true;
            foreach (var group in groups.Values) group.Stop();
        }
    }

    public override Task StopAsync(CancellationToken cancellationToken)
    {
        StopAdmissions();
        return base.StopAsync(cancellationToken);
    }

    public override void Dispose() { StopAdmissions(); base.Dispose(); }

    public IReadOnlyList<CinnamonSnapshot> Snapshot()
    {
        lock (gate) return groups.Values.Select(group => group.Snapshot()).ToArray();
    }

    public string ToPrometheusText()
    {
        var text = new StringBuilder();
        var first = true;
        foreach (var s in Snapshot())
        {
            var gauges = new Dictionary<string, double>
            {
                ["limit"] = s.Limit, ["inflight"] = s.Inflight, ["queued"] = s.Queued,
                ["rejection_ratio"] = s.RejectionRatio, ["pi_error"] = s.PiError, ["pi_integral"] = s.PiIntegral,
                ["reference_latency_ms"] = s.ReferenceLatencyMs, ["sample_latency_ms"] = s.SampleLatencyMs,
                ["estimated_queue"] = s.EstimatedQueue, ["covariance"] = s.Covariance
            };
            var counters = new Dictionary<string, double>
            {
                ["reference_resets_total"] = s.ReferenceResets, ["queue_entered_total"] = s.Entered,
                ["dispatched_total"] = s.Dispatched, ["priority_rejected_total"] = s.PriorityRejected,
                ["queue_full_total"] = s.QueueFull, ["queue_timeout_total"] = s.QueueTimeout,
                ["queue_cancelled_total"] = s.QueueCancelled, ["successful_total"] = s.Successful,
                ["failed_total"] = s.Failed, ["queue_wait_ms_sum"] = s.QueueWaitMsSum, ["queue_wait_ms_count"] = s.QueueWaitCount
            };
            foreach (var metric in gauges.Concat(counters))
            {
                var type = gauges.ContainsKey(metric.Key) ? "gauge" : "counter";
                // TYPE is emitted once, even when multiple groups are present.
                if (first) text.Append($"# TYPE svalinn_cinnamon_{metric.Key} {type}\n");
                text.Append($"svalinn_cinnamon_{metric.Key}{{group=\"{s.Group}\"}} {metric.Value.ToString(CultureInfo.InvariantCulture)}\n");
            }
            if (first) text.Append("# TYPE svalinn_cinnamon_priority_rejection_probability gauge\n");
            first = false;
            for (var i = 0; i < 4; i++)
                text.Append($"svalinn_cinnamon_priority_rejection_probability{{group=\"{s.Group}\",priority=\"{(RequestPriority)i}\"}} {s.PriorityRejectionProbabilities[i].ToString(CultureInfo.InvariantCulture)}\n");
        }
        return text.ToString();
    }

}

// Existing strategies remain registered and behave as before; only Cinnamon needs a timer.
internal sealed class CinnamonLifetimeService(ILoadSheddingStrategy strategy) : IHostedService
{
    public Task StartAsync(CancellationToken cancellationToken) => strategy is CinnamonLoadSheddingStrategy cinnamon
        ? cinnamon.StartAsync(cancellationToken) : Task.CompletedTask;
    public Task StopAsync(CancellationToken cancellationToken) => strategy is CinnamonLoadSheddingStrategy cinnamon
        ? cinnamon.StopAsync(cancellationToken) : Task.CompletedTask;
}

public sealed record CinnamonSnapshot(string Group, int Limit, int Inflight, int Queued,
    double RejectionRatio, double PiError, double PiIntegral, double ReferenceLatencyMs, double SampleLatencyMs,
    double EstimatedQueue, double Covariance, long ReferenceResets, long Entered, long Dispatched,
    long PriorityRejected, long QueueFull, long QueueTimeout, long QueueCancelled, long Successful, long Failed,
    double QueueWaitMsSum, long QueueWaitCount, double[] PriorityRejectionProbabilities);
