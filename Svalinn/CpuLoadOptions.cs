namespace Svalinn;

public sealed class CpuLoadOptions
{
    public int SampleIntervalMs { get; set; } = 250;
    public double SmoothingFactor { get; set; } = 0.3;
    public double OverloadThresholdPercent { get; set; } = 90;
    public double RecoveryThresholdPercent { get; set; } = 75;
    // CPU capacity available to this process; fractions support fractional quotas.
    // Null uses Environment.ProcessorCount, capped by Windows process affinity.
    public double? ProcessorCapacity { get; set; }

    internal bool IsValid() => SampleIntervalMs is >= 50 and <= 60_000
        && double.IsFinite(SmoothingFactor) && SmoothingFactor is > 0 and <= 1
        && double.IsFinite(OverloadThresholdPercent) && OverloadThresholdPercent is > 0 and <= 100
        && double.IsFinite(RecoveryThresholdPercent) && RecoveryThresholdPercent >= 0
        && RecoveryThresholdPercent < OverloadThresholdPercent
        && (ProcessorCapacity is null || (double.IsFinite(ProcessorCapacity.Value) && ProcessorCapacity > 0));
}
