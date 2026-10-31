namespace Svalinn.Strategy;

public interface IAsyncLoadSheddingStrategy : ILoadSheddingStrategy
{
    ValueTask<RequestAdmission> AcquireAsync(LoadSheddingContext context, CancellationToken cancellationToken);
}

public sealed record RequestAdmission(LoadSheddingDecision Decision, IRequestExecutionLease? Lease, TimeSpan QueueWait);
