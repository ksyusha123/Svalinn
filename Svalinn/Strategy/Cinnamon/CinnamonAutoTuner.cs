namespace Svalinn.Strategy.Cinnamon;

// Windowed Vegas with smoothed latency and covariance-guarded reference resets.
internal sealed class CinnamonAutoTuner
{
    private readonly CinnamonOptions options;
    private readonly int minimum;
    private readonly Queue<double> medianSamples = new();
    private readonly Queue<(double Inflight, double Throughput)> history = new();
    private double lastResetSeconds;
    private int lowerBoundWindows;
    public int Limit { get; private set; }
    public double ReferenceMs { get; private set; }
    public double SampleMs { get; private set; }
    public double EstimatedQueue { get; private set; }
    public double Covariance { get; private set; }
    public long Resets { get; private set; }

    public CinnamonAutoTuner(CinnamonOptions options, int minimum, double nowSeconds)
    {
        this.options = options;
        this.minimum = Math.Min(minimum, options.MaxLimit);
        Limit = Math.Clamp(options.InitialLimit, this.minimum, options.MaxLimit);
        lastResetSeconds = nowSeconds;
    }

    public int Update(double percentileMs, int peakInflight, double nowSeconds)
    {
        medianSamples.Enqueue(Math.Max(.001, percentileMs));
        if (medianSamples.Count > 3) medianSamples.Dequeue();
        var sorted = medianSamples.Order().ToArray();
        var median = sorted[sorted.Length / 2];
        SampleMs = SampleMs == 0 ? median : options.SmoothingFactor * median + (1 - options.SmoothingFactor) * SampleMs;
        history.Enqueue((peakInflight, peakInflight * 1000.0 / SampleMs));
        if (history.Count > options.CovarianceWindow) history.Dequeue();
        var meanX = history.Average(x => x.Inflight);
        var meanY = history.Average(x => x.Throughput);
        Covariance = history.Average(x => (x.Inflight - meanX) * (x.Throughput - meanY));
        if (ReferenceMs == 0 || SampleMs < ReferenceMs) ReferenceMs = SampleMs;
        if (!options.EnableAutoTuner) return Limit;

        var step = Math.Max(1, Math.Log10(Math.Max(2, Limit)));
        var reset = nowSeconds - lastResetSeconds >= options.ReferenceResetIntervalMs / 1000.0
            || lowerBoundWindows >= options.LowerBoundResetWindows;
        var forceDecrease = false;
        if (reset)
        {
            ReferenceMs = SampleMs;
            lastResetSeconds = nowSeconds;
            lowerBoundWindows = 0;
            Resets++;
            forceDecrease = history.Count >= 2 && Covariance < 0;
        }
        EstimatedQueue = Math.Max(0, Limit * (1 - ReferenceMs / SampleMs));
        double proposed = Limit;
        if (forceDecrease || EstimatedQueue > 6 * step) proposed -= step;
        else if (EstimatedQueue <= step) proposed += 6 * step;
        else if (EstimatedQueue < 3 * step) proposed += step;
        var upper = Math.Clamp((int)Math.Ceiling(Math.Max(1, peakInflight) * options.InflightHeadroomMultiplier), minimum, options.MaxLimit);
        Limit = Math.Clamp((int)Math.Floor(proposed), minimum, upper);
        lowerBoundWindows = Limit == minimum ? lowerBoundWindows + 1 : 0;
        return Limit;
    }
}
