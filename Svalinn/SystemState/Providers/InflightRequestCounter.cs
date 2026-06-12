namespace Svalinn.SystemState.Providers;

public sealed class InflightRequestCounter : IInflightRequestCounter
{
    private int current;

    public int Current => Volatile.Read(ref current);

    public IDisposable Track()
    {
        Interlocked.Increment(ref current);
        return new Lease(this);
    }

    private sealed class Lease(InflightRequestCounter owner) : IDisposable
    {
        private int disposed;

        public void Dispose()
        {
            if (Interlocked.Exchange(ref disposed, 1) == 0)
            {
                Interlocked.Decrement(ref owner.current);
            }
        }
    }
}
