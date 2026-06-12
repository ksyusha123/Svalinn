namespace Svalinn.Observers;

public interface ISvalinnObserver
{
    ValueTask OnDecisionAsync(SvalinnDecisionTelemetry telemetry, CancellationToken cancellationToken);
}
