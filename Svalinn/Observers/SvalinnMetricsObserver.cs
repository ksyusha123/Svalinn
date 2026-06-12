using System.Collections.Concurrent;

namespace Svalinn.Observers;

public sealed class SvalinnMetricsObserver : ISvalinnObserver
{
    private long allowed;
    private long rejected;
    private readonly ConcurrentDictionary<RequestPriority, long> rejectedByPriority = new();

    public long Allowed => Volatile.Read(ref allowed);

    public long Rejected => Volatile.Read(ref rejected);

    public IReadOnlyDictionary<RequestPriority, long> RejectedByPriority => rejectedByPriority;

    public ValueTask OnDecisionAsync(SvalinnDecisionTelemetry telemetry, CancellationToken cancellationToken)
    {
        if (telemetry.Allowed)
        {
            Interlocked.Increment(ref allowed);
        }
        else
        {
            Interlocked.Increment(ref rejected);
            rejectedByPriority.AddOrUpdate(telemetry.Priority, 1, (_, value) => value + 1);
        }

        return ValueTask.CompletedTask;
    }

    public string ToPrometheusText()
    {
        var lines = new List<string>
        {
            "# HELP svalinn_requests_allowed_total Total accepted requests.",
            "# TYPE svalinn_requests_allowed_total counter",
            $"svalinn_requests_allowed_total {Allowed}",
            "# HELP svalinn_requests_rejected_total Total shed requests.",
            "# TYPE svalinn_requests_rejected_total counter",
            $"svalinn_requests_rejected_total {Rejected}",
            "# HELP svalinn_requests_rejected_by_priority_total Total shed requests by priority.",
            "# TYPE svalinn_requests_rejected_by_priority_total counter"
        };

        foreach (var priority in Enum.GetValues<RequestPriority>())
        {
            rejectedByPriority.TryGetValue(priority, out var count);
            lines.Add($"svalinn_requests_rejected_by_priority_total{{priority=\"{priority}\"}} {count}");
        }

        return string.Join(Environment.NewLine, lines) + Environment.NewLine;
    }
}
