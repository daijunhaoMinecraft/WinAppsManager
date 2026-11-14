namespace WinAppsManager.Models;

public enum InstallerDownloadPhase
{
    Connecting,
    Downloading,
    Verifying,
    Completed,
    Cancelled,
    Failed
}

public enum InstallerDownloadSegmentState
{
    Waiting,
    Connecting,
    Downloading,
    Completed,
    Cancelled,
    Failed
}

public sealed record InstallerDownloadSegmentProgress(
    int Index, long StartByte, long? EndByte, long BytesReceived, InstallerDownloadSegmentState State)
{
    public long? TotalBytes => EndByte.HasValue ? EndByte.Value - StartByte + 1 : null;
    public double? Percentage => TotalBytes is > 0 ? Math.Min(100, BytesReceived * 100.0 / TotalBytes.Value) : null;
}

public sealed record InstallerDownloadProgress(long BytesReceived, long? TotalBytes, InstallerDownloadPhase Phase)
{
    public double? Percentage => TotalBytes is > 0 ? Math.Min(100, BytesReceived * 100.0 / TotalBytes.Value) : null;
    public double BytesPerSecond { get; init; }
    public TimeSpan? EstimatedRemaining { get; init; }
    public int ConfiguredConnections { get; init; } = 1;
    public IReadOnlyList<InstallerDownloadSegmentProgress> Segments { get; init; } = Array.Empty<InstallerDownloadSegmentProgress>();
    public string? StatusMessage { get; init; }
}

public sealed record DownloadedInstaller(string FilePath, long SizeBytes, string ReleaseId);
