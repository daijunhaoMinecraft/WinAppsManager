using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Security;
using System.Xml.Linq;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// 读取并切换“登录时自动运行”的计划任务。
/// <para>
/// 判定规则与任务管理器的“启动”页一致：任务必须带 <c>LogonTrigger</c>，并且至少有一个
/// <c>Exec</c> 动作。任务的启用状态直接存在任务定义文件的 <c>&lt;Settings&gt;&lt;Enabled&gt;</c> 里，
/// 因此读取不需要启动任何进程；切换与删除交给 <c>schtasks.exe</c>——让它自己改写 XML，
/// 比在这里拼一份可能破坏 schema 的定义安全得多。
/// </para>
/// </summary>
public static class StartupTaskProvider
{
    private const string TaskNamespaceRoot = "Task";
    private const string SystemTaskFolderPrefix = @"\Microsoft\";

    /// <summary>本机全部任务定义所在目录。</summary>
    public static string TasksDirectory =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), "System32", "Tasks");

    public static IReadOnlyList<StartupItem> Scan(CancellationToken cancellationToken = default)
    {
        var items = new List<StartupItem>();
        string root = TasksDirectory;
        if (!Directory.Exists(root))
        {
            return items;
        }

        var options = new EnumerationOptions
        {
            RecurseSubdirectories = true,
            IgnoreInaccessible = true,
            // 默认会跳过隐藏/系统属性，任务文件通常没有这些属性，但不要因此漏读。
            AttributesToSkip = 0,
            ReturnSpecialDirectories = false
        };

        foreach (string file in Directory.EnumerateFiles(root, "*", options))
        {
            cancellationToken.ThrowIfCancellationRequested();
            StartupItem? item = TryReadTaskFile(root, file, cancellationToken);
            if (item is not null)
            {
                items.Add(item);
            }
        }

        return items;
    }

    /// <summary>
    /// 重新读取某个任务是否启用。读的是任务定义文件，不启动 schtasks，切换后可以低成本回读确认。
    /// </summary>
    public static bool ReadIsEnabled(string taskPath)
    {
        string? file = ResolveTaskFile(taskPath);
        if (file is null)
        {
            return false;
        }

        try
        {
            XDocument document = XDocument.Load(file);
            return ReadEnabled(document);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or SecurityException or System.Xml.XmlException)
        {
            return false;
        }
    }

    /// <summary>用 <c>schtasks /Change</c> 启用或禁用任务。</summary>
    public static bool SetEnabled(string taskPath, bool enabled, out string? error)
        => RunSchtasks(
            ["/Change", "/TN", taskPath, enabled ? "/ENABLE" : "/DISABLE"],
            out error);

    /// <summary>
    /// 创建一个“登录时触发”的计划任务（“添加自启动项”的任务类型）。任务名固定落在根文件夹，
    /// 不允许带 <c>\</c>，否则会在任务计划程序里凭空建出目录。创建后以任务定义文件回读确认。
    /// </summary>
    public static bool Create(string taskName, string commandLine, out string? error)
    {
        error = null;
        string normalized = (taskName ?? string.Empty).Trim().TrimStart('\\');
        if (normalized.Length == 0)
        {
            error = "任务名称不能为空。";
            return false;
        }

        if (ResolveTaskFile(@"\" + normalized) is { } existing && File.Exists(existing))
        {
            error = $"已存在同名计划任务：{normalized}";
            return false;
        }

        if (!RunSchtasks(["/Create", "/TN", normalized, "/SC", "ONLOGON", "/TR", commandLine], out error))
        {
            return false;
        }

        // schtasks 返回成功后仍以任务定义文件为准，避免“命令成功但任务没建出来”。
        if (ResolveTaskFile(@"\" + normalized) is not { } created || !File.Exists(created))
        {
            error = "schtasks 报告成功，但没有找到任务定义文件。";
            return false;
        }

        return true;
    }

    /// <summary>用 <c>schtasks /Delete</c> 删除任务。</summary>
    public static bool Delete(string taskPath, out string? error)
        => RunSchtasks(["/Delete", "/TN", taskPath, "/F"], out error);

    private static StartupItem? TryReadTaskFile(string root, string file, CancellationToken cancellationToken)
    {
        try
        {
            string text = File.ReadAllText(file);
            // 先用最便宜的子串判断挡掉绝大多数无关任务，避免为两百多个文件建 XML 树。
            if (text.IndexOf("LogonTrigger", StringComparison.Ordinal) < 0)
            {
                return null;
            }

            XDocument document = XDocument.Parse(text);
            XElement? rootElement = document.Root;
            if (rootElement is null || !NameIs(rootElement, TaskNamespaceRoot))
            {
                return null;
            }

            if (!HasLogonTrigger(document) || !TryReadExecAction(document, out string command, out string arguments))
            {
                return null;
            }

            string relative = Path.GetRelativePath(root, file);
            string taskPath = @"\" + relative.Replace(Path.DirectorySeparatorChar, '\\');
            if (taskPath.StartsWith(SystemTaskFolderPrefix, StringComparison.OrdinalIgnoreCase))
            {
                // \Microsoft\ 下是 Windows 自己的维护/更新任务（ClipESU、DirectXDatabaseUpdater、
                // LicenseImdsIntegration 等）。任务管理器的“启动”页同样不显示它们，
                // 列出来只会把真正的自启动软件淹没，因此这里直接跳过。
                return null;
            }

            string name = Path.GetFileNameWithoutExtension(relative);
            if (name.Length == 0)
            {
                return null;
            }

            string? userId = ReadPrincipalUserId(document);
            string fullCommand = arguments.Length > 0 ? $"{command} {arguments}" : command;
            string executable = ExecutableMetadataService.ResolveExecutablePath(command);
            ExecutableMetadata metadata = ExecutableMetadataService.Read(executable);

            return new StartupItem
            {
                Id = $"task:{taskPath.ToUpperInvariant()}",
                RegistrationName = name,
                Name = ExecutableMetadataService.ResolveDisplayName(name, metadata),
                CompanyName = metadata.CompanyName,
                // 任务计划程序“常规”页的“描述”，也是鼠标悬停时显示的那段文字。
                Description = ReadChildValue(FindChild(document.Root, "RegistrationInfo"), "Description").Trim(),
                Source = StartupItemSource.ScheduledTask,
                Command = fullCommand.Trim(),
                Location = taskPath,
                ApprovalValueName = taskPath,
                IconPath = executable.Length > 0 ? executable : command.Trim().Trim('"'),
                IsMachineScope = IsMachinePrincipal(userId),
                IsEnabled = ReadEnabled(document)
            };
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or SecurityException or System.Xml.XmlException
                                              or ArgumentException or NotSupportedException)
        {
            // 单个任务文件损坏或不可读时跳过，不影响整张列表。
            Debug.WriteLine(exception);
            return null;
        }
    }

    private static bool HasLogonTrigger(XDocument document)
    {
        XElement? triggers = FindChild(document.Root, "Triggers");
        return triggers is not null && triggers.Elements().Any(child => NameIs(child, "LogonTrigger"));
    }

    private static bool TryReadExecAction(XDocument document, out string command, out string arguments)
    {
        command = string.Empty;
        arguments = string.Empty;
        XElement? actions = FindChild(document.Root, "Actions");
        XElement? exec = actions?.Elements().FirstOrDefault(child => NameIs(child, "Exec"));
        if (exec is null)
        {
            return false;
        }

        command = ReadChildValue(exec, "Command").Trim();
        arguments = ReadChildValue(exec, "Arguments").Trim();
        return command.Length > 0;
    }

    private static bool ReadEnabled(XDocument document)
    {
        XElement? settings = FindChild(document.Root, "Settings");
        string? value = FindChild(settings, "Enabled")?.Value?.Trim();
        return !string.Equals(value, "false", StringComparison.OrdinalIgnoreCase);
    }

    private static string? ReadPrincipalUserId(XDocument document)
    {
        XElement? principals = FindChild(document.Root, "Principals");
        XElement? principal = principals?.Elements().FirstOrDefault(child => NameIs(child, "Principal"));
        return FindChild(principal, "UserId")?.Value?.Trim();
    }

    /// <summary>SYSTEM、服务账户和内置管理员组都算“所有用户”范围。</summary>
    private static bool IsMachinePrincipal(string? userId)
    {
        if (string.IsNullOrWhiteSpace(userId))
        {
            return false;
        }

        return userId is "S-1-5-18" or "S-1-5-19" or "S-1-5-20"
            || userId.StartsWith("S-1-5-32-", StringComparison.OrdinalIgnoreCase)
            || userId.StartsWith("S-1-5-80-", StringComparison.OrdinalIgnoreCase);
    }

    private static XElement? FindChild(XElement? parent, string localName)
        => parent?.Elements().FirstOrDefault(child => NameIs(child, localName));

    private static string ReadChildValue(XElement? parent, string localName)
        => FindChild(parent, localName)?.Value ?? string.Empty;

    /// <summary>任务 XML 带 <c>mit/task</c> 命名空间，按本地名比较可以同时兼容 1.1 与 1.2 schema。</summary>
    private static bool NameIs(XElement element, string localName)
        => string.Equals(element.Name.LocalName, localName, StringComparison.Ordinal);

    private static string? ResolveTaskFile(string taskPath)
    {
        if (string.IsNullOrWhiteSpace(taskPath))
        {
            return null;
        }

        string relative = taskPath.TrimStart('\\');
        if (relative.Length == 0)
        {
            return null;
        }

        return Path.Combine(TasksDirectory, relative.Replace('\\', Path.DirectorySeparatorChar));
    }

    /// <summary>
    /// 执行 schtasks 并返回是否成功。超时会结束进程，输出作为错误信息回传给界面。
    /// </summary>
    private static bool RunSchtasks(IReadOnlyList<string> arguments, out string? error)
    {
        error = null;
        var startInfo = new ProcessStartInfo
        {
            FileName = "schtasks.exe",
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };
        foreach (string argument in arguments)
        {
            startInfo.ArgumentList.Add(argument);
        }

        try
        {
            using var process = Process.Start(startInfo);
            if (process is null)
            {
                error = "无法启动 schtasks.exe。";
                return false;
            }

            Task<string> stdout = process.StandardOutput.ReadToEndAsync();
            Task<string> stderr = process.StandardError.ReadToEndAsync();
            if (!process.WaitForExit(20000))
            {
                try
                {
                    process.Kill(entireProcessTree: true);
                }
                catch (Exception exception) when (exception is InvalidOperationException or System.ComponentModel.Win32Exception)
                {
                    // 进程可能已经退出，忽略。
                }

                error = "schtasks.exe 执行超时。";
                return false;
            }

            string output = stdout.GetAwaiter().GetResult().Trim();
            string errorOutput = stderr.GetAwaiter().GetResult().Trim();
            if (process.ExitCode == 0)
            {
                return true;
            }

            error = errorOutput.Length > 0
                ? errorOutput
                : output.Length > 0 ? output : $"schtasks.exe 退出码 {process.ExitCode.ToString(CultureInfo.InvariantCulture)}。";
            return false;
        }
        catch (Exception exception) when (exception is System.ComponentModel.Win32Exception
                                              or InvalidOperationException or IOException)
        {
            error = $"{exception.GetType().Name}：{exception.Message}";
            return false;
        }
    }
}
