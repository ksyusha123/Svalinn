namespace Svalinn.SystemState.Providers;

public interface IInflightRequestCounter
{
    int Current { get; }

    IDisposable Track();
}
