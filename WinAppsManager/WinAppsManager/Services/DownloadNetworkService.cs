using System.Net;
using System.Net.Http;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Resolves download URLs through an optional GitHub mirror and builds the
/// HTTP client used for installer transfers from the saved network settings.
/// </summary>
public static class DownloadNetworkService
{
    private static readonly string[] GitHubHosts =
    [
        "github.com",
        "githubusercontent.com",
        "githubassets.com"
    ];

    public static string ResolveDownloadUrl(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) || !IsGitHubHost(uri.Host))
        {
            return url;
        }

        string mirror = AppSettingsService.Current.GitHubMirror;
        if (!AppSettingsService.IsValidHttpUrl(mirror))
        {
            return url;
        }

        string prefix = mirror.EndsWith('/') ? mirror : mirror + "/";
        return prefix + url;
    }

    public static AppRelease ApplyMirror(AppRelease release)
    {
        ArgumentNullException.ThrowIfNull(release);
        string original = release.DownloadUrl;
        string mirrored = ResolveDownloadUrl(original);
        if (string.Equals(mirrored, original, StringComparison.Ordinal))
        {
            return release;
        }

        // 只记录主机名，绝不记录完整（可能带签名/令牌）的下载地址。
        AppLogService.Info(
            $"GitHub 下载地址已改写为镜像：镜像主机={DescribeHost(AppSettingsService.Current.GitHubMirror)}，" +
            $"原始主机={DescribeHost(original)}。");
        return new AppRelease
        {
            Id = release.Id,
            Version = release.Version,
            Platform = release.Platform,
            Architecture = release.Architecture,
            DownloadUrl = mirrored,
            FileName = release.FileName,
            SizeBytes = release.SizeBytes,
            Sha256 = release.Sha256
        };
    }

    public static HttpClient CreateHttpClient()
    {
        var settings = AppSettingsService.Current;
        try
        {
            var handler = new SocketsHttpHandler
            {
                ConnectTimeout = TimeSpan.FromSeconds(settings.DownloadTimeoutSeconds),
                MaxConnectionsPerServer = InstallerDownloadOptions.MaximumConnections,
                AutomaticDecompression = DecompressionMethods.None
            };

            switch (settings.ProxyMode)
            {
                case ProxyMode.Direct:
                    handler.UseProxy = false;
                    break;
                case ProxyMode.Custom when Uri.TryCreate(settings.CustomProxyUrl, UriKind.Absolute, out var proxyUri)
                                           && (proxyUri.Scheme == Uri.UriSchemeHttp || proxyUri.Scheme == Uri.UriSchemeHttps):
                    handler.UseProxy = true;
                    var proxy = new WebProxy(proxyUri);
                    if (!string.IsNullOrWhiteSpace(proxyUri.UserInfo))
                    {
                        string[] credentials = proxyUri.UserInfo.Split(':', 2);
                        proxy.Credentials = new NetworkCredential(
                            Uri.UnescapeDataString(credentials[0]),
                            credentials.Length > 1 ? Uri.UnescapeDataString(credentials[1]) : string.Empty);
                    }
                    handler.Proxy = proxy;
                    break;
                default:
                    // System proxy: let the platform/environment decide.
                    handler.UseProxy = true;
                    handler.Proxy = null;
                    break;
            }

            if (settings.IgnoreCertificateErrors)
            {
                handler.SslOptions.RemoteCertificateValidationCallback = static (_, _, _, _) => true;
            }

            var client = new HttpClient(handler) { Timeout = Timeout.InfiniteTimeSpan };
            client.DefaultRequestHeaders.UserAgent.ParseAdd("WinAppsManager/1.0");
            // 只记录镜像/代理模式等定位问题的配置，不记录代理认证信息、证书配置或任何下载地址。
            AppLogService.Info(
                $"创建下载 HTTP 客户端：代理模式={settings.ProxyMode}，超时={settings.DownloadTimeoutSeconds} 秒，" +
                $"GitHub 镜像={DescribeMirror(settings.GitHubMirror)}。");
            return client;
        }
        catch (Exception exception)
        {
            AppLogService.Error(
                $"创建下载 HTTP 客户端失败：代理模式={settings.ProxyMode}，{exception.GetType().Name}：{exception.Message}",
                exception);
            throw;
        }
    }

    /// <summary>日志用的镜像摘要：只保留主机名（镜像前缀可能包含路径，但不需要写进日志）。</summary>
    private static string DescribeMirror(string? mirror)
    {
        string text = (mirror ?? string.Empty).Trim();
        if (text.Length == 0)
        {
            return "直连";
        }

        return Uri.TryCreate(text, UriKind.Absolute, out var uri) && !string.IsNullOrEmpty(uri.Host)
            ? $"镜像主机 {uri.Host}"
            : "已配置（地址无法解析）";
    }

    /// <summary>日志用的主机名；无法解析时返回“未知主机”，绝不回显完整地址。</summary>
    private static string DescribeHost(string? url)
        => Uri.TryCreate((url ?? string.Empty).Trim(), UriKind.Absolute, out var uri) && !string.IsNullOrEmpty(uri.Host)
            ? uri.Host
            : "未知主机";

    private static bool IsGitHubHost(string host)
    {
        foreach (string candidate in GitHubHosts)
        {
            if (host.Equals(candidate, StringComparison.OrdinalIgnoreCase)
                || host.EndsWith("." + candidate, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }
        return false;
    }
}
