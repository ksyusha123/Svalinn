namespace Svalinn.Strategy.Cinnamon;

// All arrivals, including early rejections, enter this bounded distribution.
internal sealed class PriorityDistribution(int capacity)
{
    private readonly RequestPriority[] history = new RequestPriority[capacity];
    private readonly int[] counts = new int[4];
    private int next, size;

    public void Observe(RequestPriority priority)
    {
        if (size == history.Length) counts[(int)history[next]]--;
        else size++;
        history[next] = priority;
        counts[(int)priority]++;
        next = (next + 1) % history.Length;
    }

    public double Probability(RequestPriority priority, double ratio)
    {
        if (size == 0 || ratio <= 0) return 0;
        if (ratio >= 1) return 1;
        var remaining = ratio * size;
        for (var tier = 0; tier < counts.Length; tier++)
        {
            if (tier == (int)priority)
                return counts[tier] == 0 ? (remaining > 0 ? 1 : 0) : Math.Clamp(remaining / counts[tier], 0, 1);
            remaining = Math.Max(0, remaining - counts[tier]);
        }
        return 0;
    }
}
