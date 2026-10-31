namespace Svalinn.Strategy;

// Admission must reserve capacity atomically, before any asynchronous observer or handler work.
public interface IConcurrencyLoadSheddingStrategy : ILoadSheddingStrategy
{
    LoadSheddingDecision TryAcquire(LoadSheddingContext context, out IRequestExecutionLease? lease);
}

public interface IRequestExecutionLease : IDisposable
{
    void RecordCompletion(TimeSpan duration, bool succeeded);
}
