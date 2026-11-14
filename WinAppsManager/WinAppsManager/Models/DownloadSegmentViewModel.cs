using System.ComponentModel;

namespace WinAppsManager.Models;

public sealed class DownloadSegmentViewModel : INotifyPropertyChanged
{
    private InstallerDownloadSegmentProgress _progress;

    public DownloadSegmentViewModel(InstallerDownloadSegmentProgress progress) => _progress = progress;

    public int Index => _progress.Index;
    public InstallerDownloadSegmentState State => _progress.State;
    public string Name => $"分段 {Index}";
    public string StateText => _progress.State switch
    {
        InstallerDownloadSegmentState.Waiting => "等待中",
        InstallerDownloadSegmentState.Connecting => "连接中",
        InstallerDownloadSegmentState.Downloading => "下载中",
        InstallerDownloadSegmentState.Completed => "已完成",
        InstallerDownloadSegmentState.Cancelled => "已取消",
        InstallerDownloadSegmentState.Failed => "失败",
        _ => "等待中"
    };
    public string PercentageText => _progress.Percentage.HasValue
        ? $"{Math.Clamp(_progress.Percentage.Value, 0, 100):0.0}%" : "未知";
    public double ProgressValue => _progress.Percentage is double percentage && double.IsFinite(percentage)
        ? Math.Clamp(percentage, 0, 100) : 0;
    public string BytesText => _progress.TotalBytes is > 0
        ? $"{DownloadDisplayFormatting.Bytes(_progress.BytesReceived)} / {DownloadDisplayFormatting.Bytes(_progress.TotalBytes.Value)}"
        : DownloadDisplayFormatting.Bytes(_progress.BytesReceived);
    public string RangeText => _progress.EndByte.HasValue
        ? $"字节范围 {_progress.StartByte:N0} – {_progress.EndByte.Value:N0}" : "正在读取完整文件";
    public string ToolTipText => $"{Name} · {StateText}\n{PercentageText} · {BytesText}";

    public event PropertyChangedEventHandler? PropertyChanged;

    public void Update(InstallerDownloadSegmentProgress progress)
    {
        _progress = progress;
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(string.Empty));
    }

    public void FinishIncomplete(InstallerDownloadSegmentState state)
    {
        if (_progress.State is InstallerDownloadSegmentState.Completed or InstallerDownloadSegmentState.Failed or InstallerDownloadSegmentState.Cancelled)
        {
            return;
        }
        Update(_progress with { State = state });
    }

    public void Complete(long? completedFileSize = null)
    {
        var progress = _progress;
        if (!progress.EndByte.HasValue && completedFileSize is > 0)
        {
            progress = progress with { EndByte = progress.StartByte + completedFileSize.Value - 1 };
        }
        Update(progress with { BytesReceived = progress.TotalBytes ?? progress.BytesReceived,
            State = InstallerDownloadSegmentState.Completed });
    }
}

internal static class DownloadDisplayFormatting
{
    public static string Bytes(long bytes) => Math.Max(0, bytes) switch
    {
        >= 1024 * 1024 * 1024 => $"{Math.Max(0, bytes) / (1024.0 * 1024 * 1024):0.00} GB",
        >= 1024 * 1024 => $"{Math.Max(0, bytes) / (1024.0 * 1024):0.0} MB",
        >= 1024 => $"{Math.Max(0, bytes) / 1024.0:0.0} KB",
        _ => $"{Math.Max(0, bytes)} B"
    };

    public static string Speed(double bytesPerSecond)
    {
        if (!double.IsFinite(bytesPerSecond) || bytesPerSecond < 0)
        {
            return "计算中";
        }
        return bytesPerSecond switch
        {
            >= 1024 * 1024 * 1024 => $"{bytesPerSecond / (1024.0 * 1024 * 1024):0.00} GB/s",
            >= 1024 * 1024 => $"{bytesPerSecond / (1024.0 * 1024):0.0} MB/s",
            >= 1024 => $"{bytesPerSecond / 1024.0:0.0} KB/s",
            _ => $"{bytesPerSecond:0} B/s"
        };
    }

    public static string Remaining(TimeSpan? remaining)
    {
        if (!remaining.HasValue || remaining.Value < TimeSpan.Zero)
        {
            return "计算中";
        }
        var seconds = Math.Ceiling(remaining.Value.TotalSeconds);
        if (!double.IsFinite(seconds))
        {
            return "计算中";
        }
        if (seconds < 1)
        {
            return "即将完成";
        }
        if (seconds < 60)
        {
            return $"约 {seconds:0} 秒";
        }
        var hours = (long)(seconds / 3600);
        var minutes = (long)(seconds / 60) % 60;
        var remainingSeconds = (long)seconds % 60;
        return hours >= 1
            ? $"约 {hours:00}:{minutes:00}:{remainingSeconds:00}"
            : $"约 {minutes:00}:{remainingSeconds:00}";
    }
}
