using JetBrains.Annotations;

namespace Svalinn.Priority;

[AttributeUsage(AttributeTargets.Method | AttributeTargets.Class)]
[UsedImplicitly]
public class PriorityAttribute(RequestPriority priority) : Attribute
{
    public RequestPriority Priority { get; } = priority;
}
