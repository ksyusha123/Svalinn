namespace Svalinn.Strategy;

public interface ILoadSheddingStrategy
{
    ValueTask<LoadSheddingDecision> DecideAsync(LoadSheddingContext context, CancellationToken cancellationToken);
}
