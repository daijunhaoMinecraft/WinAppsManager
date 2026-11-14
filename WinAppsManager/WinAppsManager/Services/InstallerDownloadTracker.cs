using System.Diagnostics;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

internal sealed class InstallerDownloadTracker : IAsyncDisposable
{
    private readonly object _gate = new();
    private readonly IProgress<InstallerDownloadProgress>? _progress;
    private readonly int _configuredConnections;
    private readonly Stopwatch _clock = Stopwatch.StartNew();
    private readonly Queue<(double Time, long Bytes)> _samples = new();
    private readonly CancellationTokenSource _tickerCancellation = new();
    private readonly Task _ticker;
    private InstallerDownloadSegmentProgress[] _segments = [];
    private InstallerDownloadPhase _phase = InstallerDownloadPhase.Connecting;
    private long? _totalBytes;
    private string? _statusMessage;

    public InstallerDownloadTracker(IProgress<InstallerDownloadProgress>? progress, int configuredConnections, long? totalBytes)
    {
        _progress = progress;
        _configuredConnections = configuredConnections;
        _totalBytes = totalBytes;
        // 每个下载任务只记录一次配置摘要；进度更新每 200ms 一次，绝不写日志。
        AppLogService.Verbose(
            $"下载进度跟踪启动：配置连接数={_configuredConnections}，" +
            $"预期大小={(totalBytes is > 0 ? $"{totalBytes.Value} 字节" : "未知")}。");
        Publish();
        _ticker = TickAsync();
    }

    public void Configure(long? totalBytes, IEnumerable<InstallerDownloadSegmentProgress> segments, string? message = null)
    {
        int segmentCount;
        lock (_gate)
        {
            _totalBytes = totalBytes;
            _segments = segments.ToArray();
            segmentCount = _segments.Length;
            _phase = InstallerDownloadPhase.Downloading;
            _statusMessage = message;
            _samples.Clear();
            _samples.Enqueue((_clock.Elapsed.TotalSeconds, 0));
            PublishLocked();
        }

        AppLogService.Verbose(
            $"下载进度跟踪：开始传输，分段={segmentCount}，" +
            $"总大小={(totalBytes is > 0 ? $"{totalBytes.Value} 字节" : "未知")}" +
            $"{(string.IsNullOrWhiteSpace(message) ? "。" : $"，提示={message}。")}");
    }

    public void AddBytes(int index, int count)
    {
        lock (_gate)
        {
            var segment = _segments[index];
            _segments[index] = segment with { BytesReceived = checked(segment.BytesReceived + count) };
        }
    }

    public void SetSegmentState(int index, InstallerDownloadSegmentState state)
    {
        lock (_gate)
        {
            _segments[index] = _segments[index] with { State = state };
            PublishLocked();
        }
    }

    public void SetPhase(InstallerDownloadPhase phase, string? message = null)
    {
        InstallerDownloadPhase previous;
        lock (_gate)
        {
            previous = _phase;
            _phase = phase;
            if (message is not null) _statusMessage = message;
            PublishLocked();
        }

        if (previous != phase)
        {
            AppLogService.Verbose(
                $"下载阶段变化：{previous} → {phase}{(string.IsNullOrWhiteSpace(message) ? "。" : $"，提示={message}。")}");
        }
    }

    public void FinishUncompleted(InstallerDownloadSegmentState state)
    {
        lock (_gate)
        {
            for (var index = 0; index < _segments.Length; index++)
            {
                if (_segments[index].State is not (InstallerDownloadSegmentState.Completed or InstallerDownloadSegmentState.Failed or InstallerDownloadSegmentState.Cancelled))
                    _segments[index] = _segments[index] with { State = state };
            }
        }
    }

    private async Task TickAsync()
    {
        using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(200));
        try
        {
            while (await timer.WaitForNextTickAsync(_tickerCancellation.Token).ConfigureAwait(false)) Publish();
        }
        catch (OperationCanceledException) when (_tickerCancellation.IsCancellationRequested) { }
    }

    private void Publish()
    {
        lock (_gate) PublishLocked();
    }

    private void PublishLocked()
    {
        long received = 0;
        foreach (var segment in _segments) received = checked(received + segment.BytesReceived);
        var now = _clock.Elapsed.TotalSeconds;
        double speed = 0;
        TimeSpan? remaining = null;
        if (_phase == InstallerDownloadPhase.Downloading)
        {
            _samples.Enqueue((now, received));
            while (_samples.Count > 2 && now - _samples.Peek().Time > 3) _samples.Dequeue();
            var first = _samples.Peek();
            var elapsed = now - first.Time;
            if (elapsed >= 0.1) speed = Math.Max(0, (received - first.Bytes) / elapsed);
            if (_totalBytes.HasValue && speed > 0 && received < _totalBytes.Value)
            {
                var seconds = (_totalBytes.Value - received) / speed;
                if (double.IsFinite(seconds) && seconds <= TimeSpan.MaxValue.TotalSeconds)
                    remaining = TimeSpan.FromSeconds(seconds);
            }
            else if (_totalBytes.HasValue && received >= _totalBytes.Value) remaining = TimeSpan.Zero;
        }

        _progress?.Report(new InstallerDownloadProgress(received, _totalBytes, _phase)
        {
            BytesPerSecond = speed,
            EstimatedRemaining = remaining,
            ConfiguredConnections = _configuredConnections,
            Segments = Array.AsReadOnly(_segments.ToArray()),
            StatusMessage = _statusMessage
        });
    }

    public async Task StopAsync()
    {
        _tickerCancellation.Cancel();
        await _ticker.ConfigureAwait(false);
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync().ConfigureAwait(false);
        _tickerCancellation.Dispose();
    }
}
