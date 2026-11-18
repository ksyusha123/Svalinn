using System.Diagnostics.Tracing;
using System.Text.Json;
using Microsoft.Diagnostics.NETCore.Client;
using Microsoft.Diagnostics.Tracing;
using Microsoft.Diagnostics.Tracing.Parsers;
using Microsoft.Diagnostics.Tracing.Etlx;

if (args[0] == "record")
{
    var pid = int.Parse(args[1]);
    var target = args[2];
    var client = new DiagnosticsClient(pid);
    var providers = new[] {
        new EventPipeProvider("Microsoft-Windows-DotNETRuntime", EventLevel.Verbose, 0x14001),
        new EventPipeProvider("Microsoft-DotNETCore-SampleProfiler", EventLevel.Informational),
        new EventPipeProvider("System.Runtime", EventLevel.Informational, 0,
            new Dictionary<string,string> { ["EventCounterIntervalSec"] = "1" })
    };
    using var session = client.StartEventPipeSession(providers, requestRundown: true, circularBufferMB: 64);
    using var stream = File.Create(target);
    var copy = session.EventStream.CopyToAsync(stream);
    File.WriteAllText(target + ".ready", DateTimeOffset.UtcNow.ToString("O"));
    while (!File.Exists(target + ".stop") && !copy.IsCompleted) await Task.Delay(100);
    try { session.Stop(); } catch (ServerNotAvailableException) { }
    await copy;
    Console.WriteLine($"Recorded {stream.Length} bytes");
}
else if (args[0] == "decode")
{
    using var source = new EventPipeEventSource(args[1]);
    using var output = new StreamWriter(args[2]);
    var counts = new Dictionary<string,int>();
    void Save(TraceEvent data) {
        var key = data.ProviderName + "/" + data.EventName;
        counts[key] = counts.GetValueOrDefault(key) + 1;
        if (key.Contains("SampleProfiler")) return;
        var payload = new Dictionary<string,object?>();
        foreach(var name in data.PayloadNames) {
            try { payload[name] = data.PayloadByName(name); } catch { }
        }
        output.WriteLine(JsonSerializer.Serialize(new { timeMs=data.TimeStampRelativeMSec,
            utc=data.TimeStamp.ToUniversalTime(), tid=data.ThreadID, provider=data.ProviderName,
            name=data.EventName, payload }));
    }
    source.Clr.All += Save;
    source.Dynamic.All += Save;
    source.Process();
    File.WriteAllText(args[2] + ".counts.json", JsonSerializer.Serialize(counts, new JsonSerializerOptions {WriteIndented=true}));
    var meta = JsonSerializer.Serialize(new { source.EventsLost, source.SessionStartTime, source.SessionEndTime });
    File.WriteAllText(args[2] + ".meta.json", meta);
    Console.WriteLine(meta);
}
else if (args[0] == "stacks")
{
    var etlx = TraceLog.CreateFromEventPipeDataFile(args[1], args[1] + ".etlx",
        new TraceLogOptions { LocalSymbolsOnly=true, ShouldResolveSymbols=delegate { return false; } });
    using var log = new TraceLog(etlx);
    using var output = new StreamWriter(args[2]);
    foreach (var data in log.Events)
    {
        if (!data.ProviderName.Contains("SampleProfiler") && !data.EventName.Contains("Contention")) continue;
        var frames = new List<string>();
        var frame = data.CallStack();
        while (frame is not null && frames.Count < 80)
        {
            var address = frame.CodeAddress;
            frames.Add(string.IsNullOrEmpty(address.FullMethodName)
                ? $"{address.ModuleName}!0x{address.Address:x}" : address.FullMethodName);
            frame = frame.Caller;
        }
        output.WriteLine(JsonSerializer.Serialize(new { timeMs=data.TimeStampRelativeMSec,
            utc=data.TimeStamp.ToUniversalTime(), tid=data.ThreadID, provider=data.ProviderName,
            name=data.EventName, frames }));
    }
}
