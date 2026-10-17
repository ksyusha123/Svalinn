namespace Svalinn;

public sealed class SvalinnOptions
{
    public LoadSheddingStrategyKind Strategy { get; set; } = LoadSheddingStrategyKind.Inflight;

    public CpuLoadOptions Cpu { get; set; } = new();

    public int MaxConcurrentRequests { get; set; } = 5;

    public RequestPriority MinimumPriorityWhenOverloaded { get; set; } = RequestPriority.High;

    public bool AlwaysAllowCriticalRequests { get; set; } = true;

    public int RetryAfterSeconds { get; set; } = 5;

    public bool ThrowOnObserverFailure { get; set; }
}
