using Microsoft.Extensions.Logging;

namespace Svalinn.Observers;

public sealed class LoggingSvalinnObserver(ILogger<LoggingSvalinnObserver> logger) : ISvalinnObserver
{
    public ValueTask OnDecisionAsync(SvalinnDecisionTelemetry telemetry, CancellationToken cancellationToken)
    {
        if (telemetry.Allowed)
        {
            logger.LogDebug(
                "Svalinn allowed {Method} {Path} with {Priority} priority from {Source}: {Reason}",
                telemetry.Method,
                telemetry.Path,
                telemetry.Priority,
                telemetry.PrioritySource,
                telemetry.Reason);
        }
        else
        {
            logger.LogWarning(
                "Svalinn rejected {Method} {Path} with {Priority} priority from {Source}: {Reason}",
                telemetry.Method,
                telemetry.Path,
                telemetry.Priority,
                telemetry.PrioritySource,
                telemetry.Reason);
        }

        return ValueTask.CompletedTask;
    }
}
