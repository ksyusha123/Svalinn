using JetBrains.Annotations;

namespace Svalinn;

[AttributeUsage(AttributeTargets.Method | AttributeTargets.Class)]
[UsedImplicitly]
public class PriorityAttribute(RequestPriority priority) : Attribute
{
    public RequestPriority Priority { get; } = priority;
}