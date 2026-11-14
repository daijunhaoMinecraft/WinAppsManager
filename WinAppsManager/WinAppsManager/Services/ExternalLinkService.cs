using System.ComponentModel;
using System.Diagnostics;
using System.Windows;

namespace WinAppsManager.Services;

public static class ExternalLinkService
{
    public static bool OpenWebsite(string url)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var uri) ||
            (uri.Scheme != Uri.UriSchemeHttps && uri.Scheme != Uri.UriSchemeHttp))
        {
            MessageBox.Show("这个网站地址无效，请稍后重试。", "无法打开网站", MessageBoxButton.OK, MessageBoxImage.Warning);
            return false;
        }

        try
        {
            Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
            return true;
        }
        catch (Exception exception) when (exception is Win32Exception or InvalidOperationException or System.IO.FileNotFoundException)
        {
            MessageBox.Show($"无法启动默认浏览器。你可以复制以下地址后在浏览器中打开：\n\n{uri.AbsoluteUri}",
                "无法打开网站", MessageBoxButton.OK, MessageBoxImage.Warning);
            return false;
        }
    }
}
