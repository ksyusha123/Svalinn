namespace Svalinn;

public sealed class AdaptiveLoadOptions
{
    public int InitialLimit { get; set; } = 4;
    public int MinLimit { get; set; } = 1;
    public int MaxLimit { get; set; } = 256;
    public int UpdateIntervalMs { get; set; } = 500;
    public int MinSamples { get; set; } = 10;
    public int MaxSampleAgeMs { get; set; } = 5000;
    public int SampleCapacity { get; set; } = 1024;
    public double LatencyTolerance { get; set; } = 0.2;
    public double DecreaseFactor { get; set; } = 0.8;
    public int ProbeIntervalMs { get; set; } = 60000;
    // Fractions of the total limit accessible to each priority; the remainder is reserved.
    public double LowPriorityFraction { get; set; } = 0.5;
    public double NormalPriorityFraction { get; set; } = 0.75;
    public double HighPriorityFraction { get; set; } = 0.9;

    internal bool IsValid() => MinLimit >= 1 && MinLimit <= InitialLimit && InitialLimit <= MaxLimit
        && MaxLimit <= 100000 && UpdateIntervalMs is >= 10 and <= 60000
        && MaxSampleAgeMs >= UpdateIntervalMs && MaxSampleAgeMs <= 60000
        && MinSamples >= 1 && MinSamples <= SampleCapacity && SampleCapacity <= 65536
        && double.IsFinite(LatencyTolerance) && LatencyTolerance is >= 0 and <= 1
        && double.IsFinite(DecreaseFactor) && DecreaseFactor is > 0 and < 1
        && ProbeIntervalMs >= MaxSampleAgeMs && ProbeIntervalMs <= 3600000
        && double.IsFinite(LowPriorityFraction) && double.IsFinite(NormalPriorityFraction)
        && double.IsFinite(HighPriorityFraction) && LowPriorityFraction > 0
        && LowPriorityFraction <= NormalPriorityFraction && NormalPriorityFraction <= HighPriorityFraction
        && HighPriorityFraction <= 1;
}
