namespace Svalinn.SystemState;

public sealed class SystemState
{
    private readonly Dictionary<string, double> _metrics = [];

    public IReadOnlyDictionary<string, double> Metrics => _metrics;

    public void Set(string name, double value)
    {
        _metrics[name] = value;
    }

    public bool TryGet(string name, out double value)
    {
        return _metrics.TryGetValue(name, out value);
    }
}
