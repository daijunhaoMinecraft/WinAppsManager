using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

namespace WinAppsManager.Services;

/// <summary>
/// Resolves <c>.lnk</c> targets so shortcuts can be matched by what they point at instead of by
/// file name (through IShellLink). WScript.Shell is apartment
/// threaded, so all resolutions run on one dedicated STA thread.
/// </summary>
public static class ShortcutResolver
{
    private static readonly TimeSpan ResolveTimeout = TimeSpan.FromSeconds(10);

    /// <summary>快捷方式解析结果：目标可执行文件以及传给它的参数。</summary>
    public readonly record struct ShortcutTarget(string TargetPath, string Arguments);

    /// <summary>
    /// 创建一个指向 <paramref name="targetPath"/> 的 <c>.lnk</c>（“添加自启动项 → 启动文件夹”用）。
    /// 与解析一样，COM 对象必须在 STA 线程上创建和保存。
    /// </summary>
    public static bool TryCreateLink(
        string shortcutPath,
        string targetPath,
        string arguments,
        string? description,
        out string? error)
    {
        if (string.IsNullOrWhiteSpace(shortcutPath) || string.IsNullOrWhiteSpace(targetPath))
        {
            error = "缺少快捷方式路径或目标路径。";
            return false;
        }

        var state = new LinkCreationState();
        var thread = new Thread(
            () => CreateCore(shortcutPath, targetPath, arguments ?? string.Empty, description, state))
        {
            IsBackground = true,
            Name = "Wam.ShortcutWriter"
        };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();

        if (!thread.Join(ResolveTimeout))
        {
            error = "创建快捷方式超时。";
            CleanupFailedFile(shortcutPath, state);
            return false;
        }

        error = state.Error;
        if (!state.Success)
        {
            CleanupFailedFile(shortcutPath, state);
        }

        return state.Success;
    }

    private static void CleanupFailedFile(string path, LinkCreationState state)
    {
        try
        {
            if (state.Success || !File.Exists(path))
            {
                return;
            }

            File.Delete(path);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            Debug.WriteLine(exception);
        }
    }

    /// <summary>
    /// 快捷方式创建是 COM 调用，失败形态不可枚举（HRESULT、绑定错误、IO 都见过），
    /// 这里统一转成错误文本：这个回调跑在后台 STA 线程上，抛出去会直接结束进程。
    /// </summary>
    private static void CreateCore(
        string shortcutPath,
        string targetPath,
        string arguments,
        string? description,
        LinkCreationState state)
    {
        Type? shellType = Type.GetTypeFromProgID("WScript.Shell");
        if (shellType is null)
        {
            state.Error = "系统没有注册 WScript.Shell，无法创建快捷方式。";
            return;
        }

        object? shell = null;
        object? link = null;
        try
        {
            shell = Activator.CreateInstance(shellType);
            if (shell is null)
            {
                state.Error = "无法创建 WScript.Shell 实例。";
                return;
            }

            dynamic wsh = shell;
            link = wsh.CreateShortcut(shortcutPath);
            dynamic shortcut = link;
            shortcut.TargetPath = targetPath;
            if (arguments.Length > 0)
            {
                shortcut.Arguments = arguments;
            }

            string? workingDirectory = Path.GetDirectoryName(targetPath);
            if (!string.IsNullOrEmpty(workingDirectory))
            {
                shortcut.WorkingDirectory = workingDirectory;
            }

            if (!string.IsNullOrEmpty(description))
            {
                shortcut.Description = description;
            }

            shortcut.Save();
            state.Success = true;
        }
        catch (Exception exception)
        {
            state.Error = $"{exception.GetType().Name}：{exception.Message}";
        }
        finally
        {
            Release(link);
            Release(shell);
        }
    }

    private sealed class LinkCreationState
    {
        public bool Success { get; set; }

        public string? Error { get; set; }
    }

    public static string? Resolve(string shortcutPath)
        => ResolveMany([shortcutPath]).TryGetValue(shortcutPath, out string? target) ? target : null;

    /// <summary>Resolves several shortcuts in one STA thread; missing or broken links are skipped.</summary>
    public static IReadOnlyDictionary<string, string> ResolveMany(IEnumerable<string> shortcutPaths)
    {
        var results = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var (path, target) in ResolveTargets(shortcutPaths))
        {
            results[path] = target.TargetPath;
        }

        return results;
    }

    /// <summary>
    /// Resolves several shortcuts including their arguments in one STA thread. The startup items
    /// page needs the arguments because that is the command line Windows actually runs.
    /// </summary>
    public static IReadOnlyDictionary<string, ShortcutTarget> ResolveTargets(IEnumerable<string> shortcutPaths)
    {
        ArgumentNullException.ThrowIfNull(shortcutPaths);
        string[] paths = shortcutPaths
            .Where(path => !string.IsNullOrWhiteSpace(path))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        var results = new Dictionary<string, ShortcutTarget>(StringComparer.OrdinalIgnoreCase);
        if (paths.Length == 0)
        {
            return results;
        }

        var thread = new Thread(() => ResolveCore(paths, results))
        {
            IsBackground = true,
            Name = "Wam.ShrotcutResolver"
        };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        if (!thread.Join(ResolveTimeout))
        {
            return results;
        }

        return results;
    }

    private static void ResolveCore(IReadOnlyList<string> paths, IDictionary<string, ShortcutTarget> results)
    {
        Type? shellType = Type.GetTypeFromProgID("WScript.Shell");
        if (shellType is null)
        {
            return;
        }

        object? shell = null;
        try
        {
            shell = Activator.CreateInstance(shellType);
            if (shell is null)
            {
                return;
            }

            dynamic wsh = shell;
            foreach (string path in paths)
            {
                object? link = null;
                try
                {
                    if (!File.Exists(path))
                    {
                        continue;
                    }

                    link = wsh.CreateShortcut(path);
                    dynamic shortcut = link;
                    string target = (string?)shortcut.TargetPath ?? string.Empty;
                    string arguments = (string?)shortcut.Arguments ?? string.Empty;
                    target = Environment.ExpandEnvironmentVariables(target.Trim()).Trim('"');
                    if (target.Length > 0)
                    {
                        results[path] = new ShortcutTarget(
                            target,
                            Environment.ExpandEnvironmentVariables(arguments.Trim()));
                    }
                }
                catch (Exception exception) when (exception is COMException or ArgumentException
                                                      or InvalidCastException or Microsoft.CSharp.RuntimeBinder.RuntimeBinderException)
                {
                    Debug.WriteLine(exception);
                }
                finally
                {
                    Release(link);
                }
            }
        }
        catch (Exception exception) when (exception is COMException or ArgumentException
                                              or InvalidCastException or Microsoft.CSharp.RuntimeBinder.RuntimeBinderException)
        {
            Debug.WriteLine(exception);
        }
        finally
        {
            Release(shell);
        }
    }

    private static void Release(object? instance)
    {
        if (instance is not null && Marshal.IsComObject(instance))
        {
            Marshal.FinalReleaseComObject(instance);
        }
    }
}
