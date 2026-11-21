using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;

namespace WinAppsManager.Services;

/// <summary>
/// 可执行文件版本信息里与界面相关的两个字段。<see cref="FileDescription"/> 是资源管理器“详细信息”
/// 里的“文件说明”，<see cref="CompanyName"/> 是“公司”。
/// </summary>
public readonly record struct ExecutableMetadata(string FileDescription, string CompanyName)
{
    public static ExecutableMetadata Empty => new(string.Empty, string.Empty);

    public bool IsEmpty => FileDescription.Length == 0 && CompanyName.Length == 0;
}

/// <summary>
/// 解析命令行指向的程序并读取它的版本信息，结果按路径缓存。
/// <para>
/// 自启动项的名字不能直接用注册表值名：那经常是 <c>Update</c>、<c>App</c>、<c>Bootstrap</c> 这类
/// 完全看不出是什么软件的字符串，而 exe 的“文件说明”才是用户熟悉的名字（任务管理器的“启动”页也是
/// 这么显示的）。取不到文件说明时才回退到注册表值名 / 快捷方式名 / 任务名。
/// </para>
/// </summary>
public static class ExecutableMetadataService
{
    /// <summary>版本信息读取结果缓存：同一个 exe 在一张列表里往往出现多次。</summary>
    private static readonly ConcurrentDictionary<string, ExecutableMetadata> Cache =
        new(StringComparer.OrdinalIgnoreCase);

    /// <summary>显示名：优先文件说明，取不到时用登记名。</summary>
    public static string ResolveDisplayName(string registrationName, in ExecutableMetadata metadata)
        => metadata.FileDescription.Length > 0 ? metadata.FileDescription : registrationName;

    /// <summary>命令行 → 完整可执行文件路径；解析不出来时返回空字符串。</summary>
    public static string ResolveExecutablePath(string? commandLine)
        => TryParseExecutablePath(commandLine, out string candidate)
            ? ResolveExistingFile(candidate)
            : string.Empty;

    /// <summary>
    /// 从命令行里取出可执行文件本身（不是参数里提到的另一个程序）。支持带引号与不带引号两种常见写法：
    /// <c>"C:\A B\x.exe" --arg</c>、<c>C:\A B\x.exe --arg</c>、<c>rundll32.exe C:\x.dll,Entry</c>。
    /// </summary>
    public static bool TryParseExecutablePath(string? commandLine, out string path)
    {
        path = string.Empty;
        string text = Environment.ExpandEnvironmentVariables(commandLine?.Trim() ?? string.Empty);
        if (text.Length == 0)
        {
            return false;
        }

        string candidate;
        if (text[0] == '"')
        {
            int closing = text.IndexOf('"', 1);
            if (closing <= 1)
            {
                return false;
            }

            candidate = text[1..closing];
        }
        else
        {
            // 没有引号时优先在第一个 ".exe" 处截断（参数里也可能出现别的 .exe，但第一个一定是主程序），
            // 命令行里没有 .exe 才退回第一个空格。
            int executableEnd = text.IndexOf(".exe", StringComparison.OrdinalIgnoreCase);
            if (executableEnd >= 0)
            {
                candidate = text[..(executableEnd + 4)];
            }
            else
            {
                int space = text.IndexOf(' ');
                candidate = space > 0 ? text[..space] : text;
            }
        }

        candidate = candidate.Trim().Trim('"');
        if (candidate.Length == 0)
        {
            return false;
        }

        path = candidate;
        return true;
    }

    /// <summary>读取版本信息。文件不存在、不是 PE 或没有版本资源时返回 <see cref="ExecutableMetadata.Empty"/>。</summary>
    public static ExecutableMetadata Read(string? executablePath)
    {
        if (string.IsNullOrWhiteSpace(executablePath))
        {
            return ExecutableMetadata.Empty;
        }

        string path;
        try
        {
            path = Environment.ExpandEnvironmentVariables(executablePath.Trim().Trim('"'));
        }
        catch (ArgumentException)
        {
            return ExecutableMetadata.Empty;
        }

        if (path.Length == 0)
        {
            return ExecutableMetadata.Empty;
        }

        if (Cache.TryGetValue(path, out ExecutableMetadata cached))
        {
            return cached;
        }

        ExecutableMetadata metadata = ReadCore(path);
        Cache[path] = metadata;
        return metadata;
    }

    private static ExecutableMetadata ReadCore(string path)
    {
        try
        {
            if (!File.Exists(path))
            {
                return ExecutableMetadata.Empty;
            }

            FileVersionInfo info = FileVersionInfo.GetVersionInfo(path);
            return new ExecutableMetadata(
                NormalizeVersionText(info.FileDescription),
                NormalizeVersionText(info.CompanyName));
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or ArgumentException or InvalidOperationException
                                              or NotSupportedException)
        {
            Debug.WriteLine(exception);
            return ExecutableMetadata.Empty;
        }
    }

    /// <summary>
    /// 有些 exe 的版本资源里干脆没填，留下的是模板占位符（实测 ArmourySocketServer 的
    /// 让显示名回退到登记名，而不是把占位符显示给用户。
    /// </summary>
    private static string NormalizeVersionText(string? value)
    {
        string trimmed = (value ?? string.Empty).Trim();
        if (trimmed.Length == 0)
        {
            return string.Empty;
        }

        return trimmed;
    }

    /// <summary>
    /// 只写了文件名的命令（<c>rundll32.exe</c>、<c>cmd.exe</c>）先在 System32 与 Windows 目录里找，
    /// 再按 PATH 查找；都找不到时保留原样，让“目标文件缺失”的判断继续保持原来的行为。
    /// </summary>
    private static string ResolveExistingFile(string candidate)
    {
        try
        {
            if (Path.IsPathRooted(candidate))
            {
                return candidate;
            }

            string systemDirectory = Environment.SystemDirectory;
            if (systemDirectory.Length > 0)
            {
                string inSystem = Path.Combine(systemDirectory, candidate);
                if (File.Exists(inSystem))
                {
                    return inSystem;
                }
            }

            string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            if (windows.Length > 0)
            {
                string inWindows = Path.Combine(windows, candidate);
                if (File.Exists(inWindows))
                {
                    return inWindows;
                }
            }

            string pathVariable = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
            foreach (string directory in pathVariable.Split(';', StringSplitOptions.RemoveEmptyEntries))
            {
                string trimmed = directory.Trim().Trim('"');
                if (trimmed.Length == 0)
                {
                    continue;
                }

                string inPath;
                try
                {
                    inPath = Path.Combine(trimmed, candidate);
                }
                catch (ArgumentException)
                {
                    continue;
                }

                if (File.Exists(inPath))
                {
                    return inPath;
                }
            }
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException
                                              or IOException or UnauthorizedAccessException)
        {
            Debug.WriteLine(exception);
        }

        return candidate;
    }
}
