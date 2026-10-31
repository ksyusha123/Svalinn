namespace Svalinn.Strategy.Cinnamon;

internal sealed class CinnamonPiController(CinnamonOptions options)
{
    private readonly LinkedList<(double End, double Duration, double Error)> history = new();
    public double Ratio { get; private set; }
    public double Error { get; private set; }
    public double Integral { get; private set; }

    public double Update(long incoming, long outgoing, int free, int limit, double nowSeconds, double elapsedSeconds)
    {
        Error = ((double)incoming - outgoing - Math.Max(0, free)) / (outgoing > 0 ? outgoing : Math.Max(1, limit));
        var window = options.IntegralWindowMs / 1000.0;
        var duration = Math.Min(elapsedSeconds, window);
        var cutoff = nowSeconds - window;
        while (history.First is { } first && first.Value.End <= cutoff) history.RemoveFirst();
        var integral = 0.0;
        foreach (var sample in history)
            integral += sample.Error * Math.Max(0, sample.End - Math.Max(sample.End - sample.Duration, cutoff));
        // Bounded integral is our anti-windup adaptation; store the applied increment in history.
        var maximum = options.IntegralGain > 0 ? 1 / options.IntegralGain : 0;
        integral = Math.Clamp(integral, 0, maximum);
        Integral = Math.Clamp(integral + Error * duration, 0, maximum);
        if (duration > 0) history.AddLast((nowSeconds, duration, (Integral - integral) / duration));
        Ratio = Math.Clamp(options.ProportionalGain * Error + options.IntegralGain * Integral, 0, 1);
        return Ratio;
    }
}
