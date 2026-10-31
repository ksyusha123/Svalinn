namespace Svalinn.Priority;

/// <summary>Endpoints sharing a bottleneck must share a scheduler and its two control loops.</summary>
[AttributeUsage(AttributeTargets.Method | AttributeTargets.Class)]
public sealed class CinnamonGroupAttribute : Attribute
{
    public string Name { get; }
    public CinnamonGroupAttribute(string name)
    {
        if (string.IsNullOrEmpty(name) || name.Length > 64
            || name.Any(c => !char.IsAsciiLetterOrDigit(c) && c is not '_' and not '-' and not '.'))
            throw new ArgumentException("Group names use 1..64 ASCII letters, digits, _, - or .", nameof(name));
        Name = name;
    }
}
