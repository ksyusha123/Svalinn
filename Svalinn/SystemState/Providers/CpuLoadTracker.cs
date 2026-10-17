namespace Svalinn.SystemState.Providers;

internal sealed class CpuLoadTracker(CpuLoadOptions options, double processorCapacity)
{
    private bool initialized, overloaded;
    private double smoothed;

    public (double Raw, double Smoothed, bool Overloaded) Sample(double cpuSeconds, double elapsedSeconds)
    {
        if (!double.IsFinite(cpuSeconds) || cpuSeconds < 0 || !double.IsFinite(elapsedSeconds) || elapsedSeconds <= 0)
            throw new ArgumentOutOfRangeException(nameof(elapsedSeconds));
        var raw = Math.Clamp(100 * cpuSeconds / elapsedSeconds / processorCapacity, 0, 100);
        smoothed = initialized ? smoothed + options.SmoothingFactor * (raw - smoothed) : raw;
        initialized = true;
        if (!overloaded && smoothed >= options.OverloadThresholdPercent) overloaded = true;
        else if (overloaded && smoothed <= options.RecoveryThresholdPercent) overloaded = false;
        return (raw, smoothed, overloaded);
    }
}
