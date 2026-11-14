using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

public enum UninstallStatus
{
    Completed,
    Canceled,
    Failed
}

public sealed class UninstallResult : INotifyPropertyChanged
{
    private UninstallStatus _status;
    private string _message;
    private bool? _registryRemoved;

    private UninstallResult(InstalledApplication app, UninstallStatus status, string message, int? exitCode)
    {
        App = app;
        _status = status;
        _message = message;
        ExitCode = exitCode;
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    public InstalledApplication App { get; }
    public UninstallStatus Status => _status;
    public string Message => _message;
    public int? ExitCode { get; }
    public string AppName => App.Name;
    public bool IsSuccess => _status == UninstallStatus.Completed;

    /// <summary>
    /// Verification against the real registry / package database: true = removed, false = the
    /// software is still registered, null = not checked or unknown.
    /// </summary>
    public bool? RegistryRemoved => _registryRemoved;

    public string StatusText => _status switch
    {
        UninstallStatus.Completed => _registryRemoved == true ? "已卸载" : "已结束",
        UninstallStatus.Canceled => "已取消",
        _ => _registryRemoved == false ? "仍有残留" : "失败"
    };

    public string DetailText => ExitCode is { } code
        ? $"{Message}（退出代码 {code}）"
        : Message;

    /// <summary>
    /// Replaces the exit-code verdict with the registry check: an uninstaller that returns a
    /// non-zero code but removed its registration counts as successful, and one that returned
    /// zero while the software is still registered counts as failed.
    /// </summary>
    public void ApplyVerification(bool? stillInstalled)
    {
        if (stillInstalled is null || _status == UninstallStatus.Canceled)
        {
            return;
        }

        _registryRemoved = !stillInstalled.Value;
        _status = stillInstalled.Value ? UninstallStatus.Failed : UninstallStatus.Completed;
        _message = stillInstalled.Value ? "注册表中仍存在该项目" : "已卸载";

        foreach (string name in new[]
                 {
                     nameof(Status), nameof(StatusText), nameof(Message), nameof(DetailText),
                     nameof(IsSuccess), nameof(RegistryRemoved)
                 })
        {
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
        }
    }

    public static UninstallResult FromExitCode(InstalledApplication app, int exitCode)
    {
        ArgumentNullException.ThrowIfNull(app);
        if (app.IsStoreApplication)
        {
            return exitCode switch
            {
                0 => new UninstallResult(
                    app,
                    UninstallStatus.Completed,
                    "已卸载",
                    exitCode),
                3010 or 1641 => new UninstallResult(
                    app,
                    UninstallStatus.Completed,
                    "商店应用卸载程序要求重启 Windows 以完成操作。",
                    exitCode),
                1602 or 1223 => new UninstallResult(
                    app,
                    UninstallStatus.Canceled,
                    "卸载已取消",
                    exitCode),
                _ => new UninstallResult(
                    app,
                    UninstallStatus.Failed,
                    "卸载失败",
                    exitCode)
            };
        }

        return exitCode switch
        {
            0 => new UninstallResult(app, UninstallStatus.Completed, "卸载程序已结束。", exitCode),
            3010 or 1641 => new UninstallResult(
                app,
                UninstallStatus.Completed,
                "卸载程序要求重启 Windows 以完成卸载。",
                exitCode),
            1602 or 1223 => new UninstallResult(
                app,
                UninstallStatus.Canceled,
                "卸载已在卸载程序中取消。",
                exitCode),
            1603 => new UninstallResult(
                app,
                UninstallStatus.Failed,
                "卸载程序报告致命错误，请查看该软件的卸载日志或使用其官方修复工具。",
                exitCode),
            1618 => new UninstallResult(
                app,
                UninstallStatus.Failed,
                "另一个安装或卸载正在进行，请等待其结束后重试。",
                exitCode),
            _ => new UninstallResult(
                app,
                UninstallStatus.Failed,
                "卸载程序以非零代码结束，软件可能未完全卸载。",
                exitCode)
        };
    }

    public static UninstallResult CreateFailed(InstalledApplication app, string message)
    {
        ArgumentNullException.ThrowIfNull(app);
        return new UninstallResult(app, UninstallStatus.Failed, message, null);
    }

    public static UninstallResult CreateCanceled(InstalledApplication app, string message)
    {
        ArgumentNullException.ThrowIfNull(app);
        return new UninstallResult(app, UninstallStatus.Canceled, message, null);
    }

    public static UninstallResult CreateCompleted(InstalledApplication app, string message, int? exitCode = null)
    {
        ArgumentNullException.ThrowIfNull(app);
        return new UninstallResult(app, UninstallStatus.Completed, message, exitCode);
    }
}

/// <summary>
/// Starts one vendor uninstaller, tracks the process tree it creates and waits for that
/// tree to finish. Some uninstallers launch a helper child and return immediately, so the
/// launcher's exit code alone is not a reliable result. The caller may cancel the operation,
/// which terminates the uninstaller together with every child process it started.
/// </summary>
public static class UninstallRunner
{
    /// <summary>How often the job object is polled while a child process is still running.</summary>
    private static readonly TimeSpan ProcessTreePollInterval = TimeSpan.FromMilliseconds(200);

    /// <summary>
    /// A launcher that only creates a console helper (conhost) releases it almost at once,
    /// while a launcher that hands the real work to a child keeps its tree alive for a while.
    /// Only the second case is treated as a delegation.
    /// </summary>
    private static readonly TimeSpan DelegationGracePeriod = TimeSpan.FromMilliseconds(1200);

    public static async Task<UninstallResult> UninstallAsync(
        InstalledApplication app,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(app);
        if (!UninstallCommandParser.TryParse(app.UninstallString, out var command, out string? parseError)
            || command is null)
        {
            AppLogService.Warn(
                $"无法解析卸载命令：{app.Name}（{app.Id}），{parseError ?? "卸载命令为空"}，将提示用户使用 Windows 设置。");
            return UninstallResult.CreateFailed(
                app,
                parseError ?? "无法解析该软件的卸载命令，请使用 Windows 设置中的卸载入口。");
        }

        cancellationToken.ThrowIfCancellationRequested();

        var startInfo = new ProcessStartInfo
        {
            FileName = command.FileName,
            Arguments = command.Arguments,
            UseShellExecute = true
        };
        if (!app.IsStoreApplication &&
            !string.IsNullOrWhiteSpace(app.InstallLocation) &&
            Directory.Exists(app.InstallLocation))
        {
            startInfo.WorkingDirectory = app.InstallLocation;
        }

        try
        {
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                AppLogService.Warn($"无法启动卸载程序：{app.Name}（{app.Id}），Process.Start 返回空。");
                return UninstallResult.CreateFailed(app, "无法启动卸载程序，请使用 Windows 设置中的卸载入口。");
            }

            using var job = UninstallProcessJob.TryCreate(process);
            int? exitCode;
            try
            {
                exitCode = await WaitForProcessExitCodeAsync(process, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                TerminateProcessTree(process, job);
                return UninstallResult.CreateCanceled(app, "已停止卸载：卸载程序已终止。");
            }

            bool delegatedToChild;
            try
            {
                delegatedToChild = job is not null &&
                                   await WaitForProcessTreeAsync(job, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                TerminateProcessTree(process, job);
                return UninstallResult.CreateCanceled(app, "已停止卸载：卸载程序及其子进程已终止。");
            }

            return BuildResult(app, exitCode, delegatedToChild);
        }
        catch (Win32Exception exception) when (exception.NativeErrorCode == 1223)
        {
            AppLogService.Info($"卸载已取消（用户取消 UAC 权限请求）：{app.Name}（{app.Id}）。");
            return UninstallResult.CreateCanceled(app, "已取消 Windows 权限请求，未执行卸载。");
        }
        catch (Win32Exception exception) when (exception.NativeErrorCode == 2)
        {
            AppLogService.Warn(
                $"找不到卸载程序：{app.Name}（{app.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            return UninstallResult.CreateFailed(
                app,
                "找不到卸载程序，软件可能已被移除或安装信息已损坏。");
        }
        catch (Win32Exception exception) when (exception.NativeErrorCode == 5)
        {
            AppLogService.Warn(
                $"无法启动卸载程序（拒绝访问）：{app.Name}（{app.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            return UninstallResult.CreateFailed(
                app,
                "无法启动卸载程序（拒绝访问）。请以管理员身份运行本应用后重试。");
        }
        catch (Win32Exception exception)
        {
            AppLogService.Warn(
                $"Windows 无法启动卸载程序：{app.Name}（{app.Id}），错误码 {exception.NativeErrorCode}，{exception.Message}",
                exception);
            return UninstallResult.CreateFailed(
                app,
                "Windows 无法启动该卸载程序，请使用 Windows 设置中的卸载入口。");
        }
        catch (InvalidOperationException exception)
        {
            AppLogService.Warn(
                $"无法启动卸载程序：{app.Name}（{app.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            return UninstallResult.CreateFailed(
                app,
                "无法启动该卸载程序，请使用 Windows 设置中的卸载入口。");
        }
        catch (IOException exception)
        {
            AppLogService.Warn(
                $"启动卸载程序时发生 IO 错误：{app.Name}（{app.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            return UninstallResult.CreateFailed(
                app,
                "启动卸载程序时发生错误，请稍后重试。");
        }
        catch (UnauthorizedAccessException exception)
        {
            AppLogService.Warn(
                $"没有权限启动卸载程序：{app.Name}（{app.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            return UninstallResult.CreateFailed(
                app,
                "没有权限启动该卸载程序，请以管理员身份运行本应用后重试。");
        }
        catch (NotSupportedException exception)
        {
            AppLogService.Warn(
                $"Windows 不支持该卸载命令：{app.Name}（{app.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            return UninstallResult.CreateFailed(
                app,
                "Windows 不支持该卸载命令，请使用 Windows 设置中的卸载入口。");
        }
    }

    private static UninstallResult BuildResult(
        InstalledApplication app,
        int? exitCode,
        bool delegatedToChild)
    {
        if (exitCode is null)
        {
            // Elevated uninstallers refuse ExitCode access to a non-elevated caller, and a
            // launcher that already handed over to a child has no meaningful code either.
            return UninstallResult.CreateCompleted(
                app,
                delegatedToChild
                    ? "卸载程序及其子进程已结束；Windows 未提供退出代码，请刷新列表确认结果。"
                    : "卸载程序已结束；Windows 未提供退出代码，请刷新列表确认结果。");
        }

        var result = UninstallResult.FromExitCode(app, exitCode.Value);
        if (delegatedToChild && result.Status == UninstallStatus.Failed)
        {
            // The launcher exited with a failure code but its child kept working, which is
            // how several vendor bootstrappers report "handed over" rather than "failed".
            return UninstallResult.CreateCompleted(
                app,
                "卸载程序把卸载工作交给子进程，子进程已结束；请刷新列表确认结果。",
                exitCode);
        }

        return result;
    }

    private static async Task<int?> WaitForProcessExitCodeAsync(Process process, CancellationToken cancellationToken)
    {
        await process.WaitForExitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            return process.ExitCode;
        }
        catch (InvalidOperationException)
        {
            return null;
        }
        catch (Win32Exception)
        {
            return null;
        }
    }

    /// <summary>
    /// Waits until no process is left in the job. Returns true when the tree stayed alive well
    /// past the launcher's own exit, i.e. the uninstaller handed its work over to a child.
    /// </summary>
    private static async Task<bool> WaitForProcessTreeAsync(
        UninstallProcessJob job,
        CancellationToken cancellationToken)
    {
        var stopwatch = Stopwatch.StartNew();
        bool sustained = false;
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            uint active = job.ActiveProcessCount();
            if (active is 0 or uint.MaxValue)
            {
                return sustained;
            }

            if (stopwatch.Elapsed >= DelegationGracePeriod)
            {
                sustained = true;
            }

            await Task.Delay(ProcessTreePollInterval, cancellationToken).ConfigureAwait(false);
        }
    }

    private static void TerminateProcessTree(Process process, UninstallProcessJob? job)
    {
        if (job is not null && job.Terminate(1))
        {
            return;
        }

        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        catch (Exception exception) when (exception is Win32Exception or InvalidOperationException
                                              or NotSupportedException or AggregateException)
        {
            AppLogService.Warn(
                $"终止卸载程序进程树失败：{exception.GetType().Name}：{exception.Message}",
                exception);
            Debug.WriteLine(exception);
        }
    }
}
