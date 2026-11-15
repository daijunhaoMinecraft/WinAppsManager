using System.Collections.ObjectModel;
using System.ComponentModel;
using System.IO;
using WinAppsManager.Services;

namespace WinAppsManager.Models;

/// <summary>
/// A single download task shown in the download center. Progress is reported by
/// <see cref="Services.DownloadCenterService"/> on the UI thread.
/// </summary>
public sealed class DownloadTaskViewModel : INotifyPropertyChanged
{
    private readonly object _gate = new();
    private InstallerDownloadPhase _phase = InstallerDownloadPhase.Connecting;
    private bool _isFinal;
    private bool _isCancelling;
    private long _bytesReceived;
    private long? _totalBytes;
    private double _bytesPerSecond;
    private TimeSpan? _remaining;
    private int _configuredConnections = 1;
    private string _statusMessage = string.Empty;
    private string _version;
    private string _fileName = string.Empty;
    private string _downloadUrl = string.Empty;
    private string? _filePath;
    private bool _hasSignatureInfo;
    private bool _signatureIsValid;
    private string _signatureStatusText = string.Empty;
    private string _signatureDetailText = string.Empty;
    private CancellationTokenSource? _cancellation;

    public DownloadTaskViewModel(AppItem app)
    {
        ArgumentNullException.ThrowIfNull(app);
        App = app;
        _version = app.Version;
        _fileName = app.LatestRelease?.FileName ?? string.Empty;
        _downloadUrl = app.LatestRelease?.DownloadUrl ?? string.Empty;
        _totalBytes = app.LatestRelease?.SizeBytes;
        StartedAt = DateTimeOffset.Now;
    }

    public AppItem App { get; }
    public string AppId => App.Id;
    public string AppName => App.Name;
    public string Category => App.Category;
    public string Version => _version;
    public string FileName => _fileName;
    public string DownloadUrl => _downloadUrl;
    public string? FilePath => _filePath;
    public DateTimeOffset StartedAt { get; }
    public string StartedText => $"开始于 {StartedAt:HH:mm}";
    public InstallerDownloadPhase Phase => _phase;
    public bool IsActive => !_isFinal && _phase is InstallerDownloadPhase.Connecting or InstallerDownloadPhase.Downloading or InstallerDownloadPhase.Verifying;
    public bool IsCompleted => _isFinal && _phase == InstallerDownloadPhase.Completed;
    public bool IsFailed => _isFinal && _phase == InstallerDownloadPhase.Failed;
    public bool IsCancelled => _isFinal && _phase == InstallerDownloadPhase.Cancelled;
    public bool CanCancel => IsActive && !_isCancelling;
    public bool CanRetry => _isFinal && _phase is InstallerDownloadPhase.Failed or InstallerDownloadPhase.Cancelled;
    public bool CanRemove => _isFinal;
    public bool CanOpenInstaller => IsCompleted && !string.IsNullOrWhiteSpace(_filePath);
    public bool CanOpenFolder => IsCompleted;
    public bool HasFilePath => !string.IsNullOrWhiteSpace(_filePath);
    public bool HasSignatureInfo => _hasSignatureInfo;
    public bool SignatureIsValid => _signatureIsValid;
    public string SignatureStatusText => _signatureStatusText;
    public string SignatureDetailText => _signatureDetailText;
    public bool HasSegments => Segments.Count > 0;
    public string ProgressText => _totalBytes is > 0 ? $"{ProgressValue:0.0}%" : "未知";
    public double ProgressValue => _totalBytes is > 0
        ? Math.Clamp(_bytesReceived * 100.0 / _totalBytes.Value, 0, 100) : 0;
    public bool IsIndeterminate => _phase is InstallerDownloadPhase.Connecting or InstallerDownloadPhase.Verifying
        || (_phase == InstallerDownloadPhase.Downloading && _totalBytes is null);
    public string BytesText => _totalBytes is > 0
        ? $"{DownloadDisplayFormatting.Bytes(_bytesReceived)} / {DownloadDisplayFormatting.Bytes(_totalBytes.Value)} · {ProgressValue:0.0}%"
        : $"已下载 {DownloadDisplayFormatting.Bytes(_bytesReceived)} · 文件大小未知";
    public string SpeedText => _phase switch
    {
        InstallerDownloadPhase.Downloading => DownloadDisplayFormatting.Speed(_bytesPerSecond),
        InstallerDownloadPhase.Verifying => "校验中",
        InstallerDownloadPhase.Completed => "下载完成",
        InstallerDownloadPhase.Cancelled or InstallerDownloadPhase.Failed => "已停止",
        _ => "等待下载"
    };
    public string RemainingText => _phase switch
    {
        InstallerDownloadPhase.Downloading => _totalBytes is > 0
            ? DownloadDisplayFormatting.Remaining(_remaining) : "文件大小未知",
        InstallerDownloadPhase.Verifying => "校验中",
        InstallerDownloadPhase.Completed => "已完成",
        InstallerDownloadPhase.Cancelled => "已取消",
        InstallerDownloadPhase.Failed => "未完成",
        _ => "等待连接"
    };
    public string StatusText => _phase switch
    {
        InstallerDownloadPhase.Connecting => "正在获取安装包信息…",
        InstallerDownloadPhase.Downloading => "正在下载安装包…",
        InstallerDownloadPhase.Verifying => "正在校验安装包…",
        InstallerDownloadPhase.Completed => "下载完成",
        InstallerDownloadPhase.Cancelled => "下载已取消",
        InstallerDownloadPhase.Failed => string.IsNullOrWhiteSpace(_statusMessage) ? "下载失败，请重试。" : _statusMessage,
        _ => "等待下载"
    };
    public string ModeText => _statusMessage;
    public bool HasModeText => !string.IsNullOrWhiteSpace(_statusMessage)
        && !string.Equals(_statusMessage, StatusText, StringComparison.Ordinal);
    public string ConnectionsText => Segments.Count > 0
        ? $"实际分段：{Segments.Count} · 本次设置线程数：{_configuredConnections}"
        : $"本次设置线程数：{_configuredConnections}";
    public string SegmentStateText => Segments.Count == 0
        ? string.Empty
        : "分段状态：" + string.Join(" · ", Segments.GroupBy(segment => segment.State)
            .Select(group => $"{group.First().StateText} {group.Count()}"));

    public ObservableCollection<DownloadSegmentViewModel> Segments { get; } = new();

    public Task Completion { get; internal set; } = Task.CompletedTask;

    public event PropertyChangedEventHandler? PropertyChanged;

    internal void AttachCancellation(CancellationTokenSource cancellation) => _cancellation = cancellation;

    internal void DetachCancellation() => _cancellation = null;

    internal void SetRelease(AppRelease release)
    {
        _version = release.Version;
        _fileName = release.FileName;
        _downloadUrl = release.DownloadUrl;
        if (release.SizeBytes is > 0)
        {
            _totalBytes = release.SizeBytes;
        }
        _statusMessage = string.Empty;
        NotifyAll();
    }

    internal void ApplyProgress(InstallerDownloadProgress progress)
    {
        if (_isFinal || progress.Phase is InstallerDownloadPhase.Completed
            or InstallerDownloadPhase.Cancelled or InstallerDownloadPhase.Failed)
        {
            return;
        }

        lock (_gate)
        {
            _phase = progress.Phase;
            _bytesReceived = progress.BytesReceived;
            _totalBytes = progress.TotalBytes ?? _totalBytes;
            _bytesPerSecond = progress.BytesPerSecond;
            _remaining = progress.EstimatedRemaining;
            if (progress.ConfiguredConnections > 0)
            {
                _configuredConnections = progress.ConfiguredConnections;
            }
            if (!_isCancelling)
            {
                _statusMessage = progress.StatusMessage ?? string.Empty;
            }
            UpdateSegments(progress.Segments);
        }
        NotifyAll();
    }

    internal void Complete(DownloadedInstaller result)
    {
        if (_isFinal)
        {
            return;
        }

        lock (_gate)
        {
            _isFinal = true;
            _isCancelling = false;
            _phase = InstallerDownloadPhase.Completed;
            _filePath = result.FilePath;
            _bytesReceived = result.SizeBytes;
            _totalBytes = result.SizeBytes;
            _bytesPerSecond = 0;
            _remaining = null;
            _statusMessage = string.Empty;
            _hasSignatureInfo = true;
            _signatureIsValid = false;
            _signatureStatusText = "数字签名：校验中…";
            _signatureDetailText = "下载完成，正在读取安装包数字签名。";
            if (Segments.Count == 0)
            {
                Segments.Add(new DownloadSegmentViewModel(new InstallerDownloadSegmentProgress(
                    1, 0, result.SizeBytes - 1, result.SizeBytes, InstallerDownloadSegmentState.Completed)));
            }
            foreach (var segment in Segments)
            {
                segment.Complete(Segments.Count == 1 ? result.SizeBytes : null);
            }
        }
        NotifyAll();
    }

    internal void ApplySignature(AuthenticodeSignatureInfo signature)
    {
        lock (_gate)
        {
            _hasSignatureInfo = true;
            _signatureIsValid = signature.IsValid;
            _signatureStatusText = signature.StatusText;
            _signatureDetailText = signature.DetailText;
        }
        NotifyAll();
    }

    internal void Fail(string message)
    {
        if (_isFinal)
        {
            return;
        }

        lock (_gate)
        {
            _isFinal = true;
            _isCancelling = false;
            _phase = InstallerDownloadPhase.Failed;
            _bytesPerSecond = 0;
            _remaining = null;
            _statusMessage = message;
            foreach (var segment in Segments)
            {
                segment.FinishIncomplete(InstallerDownloadSegmentState.Failed);
            }
        }
        NotifyAll();
    }

    internal void Cancel()
    {
        if (_isFinal)
        {
            return;
        }

        lock (_gate)
        {
            _isFinal = true;
            _isCancelling = false;
            _phase = InstallerDownloadPhase.Cancelled;
            _bytesPerSecond = 0;
            _remaining = null;
            _statusMessage = string.Empty;
            foreach (var segment in Segments)
            {
                segment.FinishIncomplete(InstallerDownloadSegmentState.Cancelled);
            }
        }
        NotifyAll();
    }

    internal void RequestCancel()
    {
        if (!IsActive)
        {
            return;
        }
        _isCancelling = true;
        _statusMessage = "正在取消下载…";
        _cancellation?.Cancel();
        NotifyAll();
    }

    internal void MarkFileMissing(string message)
    {
        lock (_gate)
        {
            _isFinal = true;
            _isCancelling = false;
            _phase = InstallerDownloadPhase.Failed;
            _filePath = null;
            _bytesPerSecond = 0;
            _remaining = null;
            _statusMessage = message;
        }
        NotifyAll();
    }

    private void UpdateSegments(IReadOnlyList<InstallerDownloadSegmentProgress> segments)
    {
        if (Segments.Count != segments.Count
            || segments.Where((segment, index) => segment.Index != Segments[index].Index).Any())
        {
            Segments.Clear();
            foreach (var segment in segments)
            {
                Segments.Add(new DownloadSegmentViewModel(segment));
            }
            return;
        }

        for (var index = 0; index < Segments.Count; index++)
        {
            Segments[index].Update(segments[index]);
        }
    }

    private void NotifyAll() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
}
