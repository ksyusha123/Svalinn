namespace Svalinn;

public sealed record LoadSheddingDecision(
    bool IsAllowed,
    string Reason,
    int? RetryAfterSeconds = null)
{
    public static LoadSheddingDecision Allow(string reason = "Request accepted")
    {
        return new LoadSheddingDecision(true, reason);
    }

    public static LoadSheddingDecision Reject(string reason, int? retryAfterSeconds = null)
    {
        return new LoadSheddingDecision(false, reason, retryAfterSeconds);
    }
}
