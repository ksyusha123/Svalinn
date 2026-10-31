namespace Svalinn;

public sealed class CinnamonOptions
{
    public int InitialLimit { get; set; } = 16;
    // Null uses CPU count capped by process affinity. Set explicitly for fractional CPU quotas.
    public int? MinLimit { get; set; }
    public int MaxLimit { get; set; } = 1024;
    public int QueueCapacity { get; set; } = 1024;
    public int QueueTimeoutMs { get; set; } = 1000;
    public int MaxGroups { get; set; } = 64;
    public bool EnablePi { get; set; } = true;
    public bool EnableAutoTuner { get; set; } = true;
    public int PiIntervalMs { get; set; } = 500;
    public int IntegralWindowMs { get; set; } = 30000;
    public double ProportionalGain { get; set; } = 0.1;
    public double IntegralGain { get; set; } = 1.4;
    public int PrioritySampleCapacity { get; set; } = 1000;
    public int LatencyMinIntervalMs { get; set; } = 2000;
    public int LatencyMaxIntervalMs { get; set; } = 30000;
    public int LatencyMinSamples { get; set; } = 250;
    public int LatencySampleCapacity { get; set; } = 4096;
    public double LatencyPercentile { get; set; } = 0.9;
    public double SmoothingFactor { get; set; } = 0.2;
    public int ReferenceResetIntervalMs { get; set; } = 60000;
    public int CovarianceWindow { get; set; } = 50;
    public int LowerBoundResetWindows { get; set; } = 3;
    public double InflightHeadroomMultiplier { get; set; } = 10;

    internal bool IsValid() => InitialLimit >= 1 && InitialLimit <= MaxLimit
        && (MinLimit is null || MinLimit >= 1 && MinLimit <= InitialLimit)
        && MaxLimit <= 100000 && QueueCapacity is >= 0 and <= 100000
        && QueueTimeoutMs is >= 1 and <= 60000 && MaxGroups is >= 1 and <= 1024
        && PiIntervalMs is >= 10 and <= 60000
        && IntegralWindowMs >= PiIntervalMs && IntegralWindowMs <= 600000
        && double.IsFinite(ProportionalGain) && ProportionalGain >= 0 && ProportionalGain <= 100
        && double.IsFinite(IntegralGain) && IntegralGain >= 0 && IntegralGain <= 100
        && PrioritySampleCapacity is >= 4 and <= 100000
        && LatencyMinIntervalMs >= PiIntervalMs && LatencyMinIntervalMs <= LatencyMaxIntervalMs
        && LatencyMaxIntervalMs <= 600000
        && LatencyMinSamples >= 1 && LatencyMinSamples <= LatencySampleCapacity
        && LatencySampleCapacity <= 65536
        && double.IsFinite(LatencyPercentile) && LatencyPercentile is > 0 and <= 1
        && double.IsFinite(SmoothingFactor) && SmoothingFactor is > 0 and <= 1
        && ReferenceResetIntervalMs >= LatencyMinIntervalMs && ReferenceResetIntervalMs <= 3600000
        && CovarianceWindow is >= 2 and <= 1000 && LowerBoundResetWindows is >= 1 and <= 1000
        && double.IsFinite(InflightHeadroomMultiplier) && InflightHeadroomMultiplier is >= 1 and <= 100;
}
