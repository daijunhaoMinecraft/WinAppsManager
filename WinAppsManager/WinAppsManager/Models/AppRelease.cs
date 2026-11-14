namespace WinAppsManager.Models;

public sealed class AppRelease
{
    public string Id { get; set; } = string.Empty;
    public string Version { get; set; } = string.Empty;
    public string Platform { get; set; } = string.Empty;
    public string Architecture { get; set; } = string.Empty;
    public string DownloadUrl { get; set; } = string.Empty;
    public string FileName { get; set; } = string.Empty;
    public long? SizeBytes { get; set; }
    public string? Sha256 { get; set; }
}
