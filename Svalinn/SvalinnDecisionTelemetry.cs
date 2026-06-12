namespace Svalinn;

public sealed record SvalinnDecisionTelemetry(
    string Method,
    string Path,
    RequestPriority Priority,
    string PrioritySource,
    bool Allowed,
    string Reason,
    SystemState.SystemState State,
    TimeSpan DecisionLatency);
