using System.Diagnostics;
using System.IO;

namespace WinAppsManager.Services;

/// <summary>Requests a Windows restart after the user accepts the delayed-deletion prompt.</summary>
public static class SystemRestartService
{
    /// <summary>
    /// Starts <c>shutdown.exe /r</c> with a short grace period. The app runs elevated, so the
    /// command is allowed to restart the machine. Returns false with a message on failure.
    /// </summary>
    public static bool TryRestart(out string error)
    {
        try
        {
            string shutdown = Path.Combine(Environment.SystemDirectory, "shutdown.exe");
            var startInfo = new ProcessStartInfo(shutdown)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                Arguments = "/r /t 5 /c \"WinAppsManager 正在重启以完成残留清理\""
            };
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                error = "无法启动 Windows 重启命令。";
                return false;
            }

            error = string.Empty;
            AppLogService.Info("用户选择立即重启，已启动 shutdown.exe /r /t 5。");
            return true;
        }
        catch (Exception exception) when (exception is IOException or
                                              System.ComponentModel.Win32Exception or
                                              InvalidOperationException or
                                              UnauthorizedAccessException)
        {
            error = exception.Message;
            AppLogService.Warn($"启动 Windows 重启命令失败：{exception.Message}", exception);
            return false;
        }
    }
}
