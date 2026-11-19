namespace WinAppsManager.Models;

/// <summary>
/// Persisted snapshot of one finished download. Only the fields the download-center card needs are
/// stored; the full <see cref="AppItem"/> is rebuilt when the history is loaded after a restart.
/// </summary>
public sealed class DownloadHistoryRecord
{
    public string AppId { get; set; } = string.Empty;
    public string AppName { get; set; } = string.Empty;
    public string AppCategory { get; set; } = string.Empty;
    public string SourceId { get; set; } = string.Empty;
    public string SourceName { get; set; } = string.Empty;
    public string AppVersion { get; set; } = string.Empty;
    public string? IconUrl { get; set; }
    public string? IconFallbackUrl { get; set; }
    public string IconText { get; set; } = string.Empty;
    public string IconBackground { get; set; } = "#0067C0";
    public long? AppSizeBytes { get; set; }

    public string Version { get; set; } = string.Empty;
    public string FileName { get; set; } = string.Empty;
    public string FilePath { get; set; } = string.Empty;
    public string DownloadUrl { get; set; } = string.Empty;
    public string ReleaseId { get; set; } = string.Empty;
    public long BytesReceived { get; set; }
    public long? TotalBytes { get; set; }

    public string Status { get; set; } = string.Empty;
    public string Message { get; set; } = string.Empty;
    public DateTimeOffset StartedAt { get; set; }

    public bool HasSignatureInfo { get; set; }
    public bool SignatureIsValid { get; set; }
    public string SignatureStatusText { get; set; } = string.Empty;
    public string SignatureDetailText { get; set; } = string.Empty;
}
