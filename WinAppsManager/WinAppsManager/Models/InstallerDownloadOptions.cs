namespace WinAppsManager.Models;

public sealed class InstallerDownloadOptions
{
    public const int MinimumConnections = 1;
    public const int MaximumConnections = 32;
    public const int DefaultConnections = 4;

    public const int MinimumTimeoutSeconds = 10;
    public const int MaximumTimeoutSeconds = 300;
    public const int DefaultTimeoutSeconds = 30;
    public const int TimeoutStepSeconds = 10;

    public int ConnectionCount { get; init; } = DefaultConnections;
    public long MinimumSegmentSize { get; init; } = 1024 * 1024;
    public int TimeoutSeconds { get; init; } = DefaultTimeoutSeconds;

    public TimeSpan Timeout => TimeSpan.FromSeconds(TimeoutSeconds);
}
