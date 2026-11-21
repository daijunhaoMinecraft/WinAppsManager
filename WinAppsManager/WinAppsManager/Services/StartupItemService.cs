using System.IO;
using System.Security;
using Microsoft.Win32;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>一次自启动项操作的结果。<see cref="Error"/> 是给日志和提示用的技术细节。</summary>
public readonly record struct StartupOperationResult(bool Success, string Message, string? Error)
{
    public static StartupOperationResult Ok(string message) => new(true, message, null);

    public static StartupOperationResult Fail(string message, string? error = null) => new(false, message, error);
}

/// <summary>
/// 自启动项的写操作：启用/禁用、删除、新增。
/// <para>
/// 启用/禁用一律通过 <c>StartupApproved</c>（与 msconfig、任务管理器同一机制）或任务的
/// <c>Enabled</c> 标志完成，不删除条目本身，因此随时可以还原；只有“删除”才真正移除登记。
/// 每一次切换后都会回读实际状态，写成功但值没生效时按失败上报。
/// </para>
/// </summary>
public static class StartupItemService
{
    public static Task<StartupOperationResult> SetEnabledAsync(
        StartupItem item,
        bool enabled,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(item);
        return Task.Run(() => SetEnabled(item, enabled, cancellationToken), cancellationToken);
    }

    public static Task<StartupOperationResult> DeleteAsync(
        StartupItem item,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(item);
        return Task.Run(() => Delete(item, cancellationToken), cancellationToken);
    }

    public static Task<StartupOperationResult> AddAsync(
        StartupItemSource source,
        string name,
        string executablePath,
        string arguments,
        CancellationToken cancellationToken = default)
        => Task.Run(
            () => Add(source, name, executablePath, arguments, cancellationToken),
            cancellationToken);

    private static StartupOperationResult SetEnabled(
        StartupItem item,
        bool enabled,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!item.CanToggle)
        {
            return StartupOperationResult.Fail($"“{item.Name}”不支持启用/禁用切换。");
        }

        string action = enabled ? "启用" : "禁用";
        if (!StartupItemProvider.WriteEnabled(item, enabled, out string? error))
        {
            AppLogService.Warn($"启动项{action}失败：{item.Name}（{item.SourceText}，{item.Location}）：{error}");
            return StartupOperationResult.Fail($"无法{action}“{item.Name}”。", error);
        }

        if (StartupItemProvider.ReadIsEnabled(item) != enabled)
        {
            return StartupOperationResult.Fail($"“{item.Name}”的{action}没有生效。", "回读状态与写入值不一致。");
        }

        item.IsEnabled = enabled;
        AppLogService.Info($"已{action}自启动项：{item.Name}（{item.SourceText}，{item.Location}）。");
        return StartupOperationResult.Ok($"已{action}“{item.Name}”。");
    }

    private static StartupOperationResult Delete(StartupItem item, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!item.CanDelete)
        {
            return StartupOperationResult.Fail($"“{item.Name}”由系统或应用自己声明，只允许启用或禁用。");
        }

        switch (item.Source)
        {
            case StartupItemSource.ScheduledTask:
                return DeleteScheduledTask(item);
            case StartupItemSource.CommonStartupFolder:
            case StartupItemSource.UserStartupFolder:
                return DeleteShortcut(item);
            default:
                return DeleteRegistryValue(item);
        }
    }

    private static StartupOperationResult DeleteScheduledTask(StartupItem item)
    {
        if (!StartupTaskProvider.Delete(item.Location, out string? error))
        {
            AppLogService.Warn($"删除计划任务失败：{item.Location}：{error}");
            return StartupOperationResult.Fail($"无法删除计划任务“{item.Name}”。", error);
        }

        AppLogService.Info($"已删除计划任务自启动项：{item.Name}（{item.Location}）。");
        return StartupOperationResult.Ok($"已删除计划任务“{item.Name}”。");
    }

    private static StartupOperationResult DeleteShortcut(StartupItem item)
    {
        try
        {
            if (File.Exists(item.Location))
            {
                File.Delete(item.Location);
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or SecurityException)
        {
            AppLogService.Warn($"删除启动文件夹快捷方式失败：{item.Location}：{exception.Message}", exception);
            return StartupOperationResult.Fail(
                $"无法删除快捷方式“{item.Name}”。",
                $"{exception.GetType().Name}：{exception.Message}");
        }

        StartupItemProvider.RemoveApproval(item.Source, item.ApprovalValueName);
        AppLogService.Info($"已删除启动文件夹自启动项：{item.Name}（{item.Location}）。");
        return StartupOperationResult.Ok($"已删除“{item.Name}”。");
    }

    private static StartupOperationResult DeleteRegistryValue(StartupItem item)
    {
        if (!TryGetRegistryTarget(item.Source, out var hive, out var view, out string keyPath))
        {
            return StartupOperationResult.Fail($"“{item.Name}”的登记位置无法解析。");
        }

        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(hive, view);
            using var key = baseKey.OpenSubKey(keyPath, writable: true);
            if (key is null)
            {
                return StartupOperationResult.Fail($"找不到注册表键 {keyPath}。");
            }

            key.DeleteValue(item.ApprovalValueName, throwOnMissingValue: false);
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException or IOException)
        {
            AppLogService.Warn($"删除注册表启动项失败：{item.Name}：{exception.Message}", exception);
            return StartupOperationResult.Fail(
                $"无法删除“{item.Name}”。",
                $"{exception.GetType().Name}：{exception.Message}");
        }

        StartupItemProvider.RemoveApproval(item.Source, item.ApprovalValueName);
        AppLogService.Info($"已删除注册表自启动项：{item.Name}（{item.Location}）。");
        return StartupOperationResult.Ok($"已删除“{item.Name}”。");
    }

    private static StartupOperationResult Add(
        StartupItemSource source,
        string name,
        string executablePath,
        string arguments,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        string trimmedName = (name ?? string.Empty).Trim();
        string trimmedPath = (executablePath ?? string.Empty).Trim().Trim('"');
        string trimmedArguments = (arguments ?? string.Empty).Trim();

        if (trimmedName.Length == 0)
        {
            return StartupOperationResult.Fail("请填写自启动项名称。");
        }

        // 同一个规则覆盖三种落点：Run 值名、快捷方式文件名、计划任务名（任务不许带 \ 建目录）。
        if (trimmedName.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
        {
            return StartupOperationResult.Fail(@"名称不能包含 \ / : * ? "" < > | 等字符。");
        }

        if (trimmedPath.Length == 0)
        {
            return StartupOperationResult.Fail("请选择要自启动的程序。");
        }

        if (!File.Exists(trimmedPath))
        {
            return StartupOperationResult.Fail($"找不到程序文件：{trimmedPath}");
        }

        string command = trimmedArguments.Length > 0
            ? $"\"{trimmedPath}\" {trimmedArguments}"
            : $"\"{trimmedPath}\"";

        return StartupSourceMetadata.GetKind(source) switch
        {
            StartupItemKind.Registry => AddRegistryValue(source, trimmedName, command),
            StartupItemKind.StartupFolder => AddStartupShortcut(source, trimmedName, trimmedPath, trimmedArguments),
            StartupItemKind.ScheduledTask => AddScheduledTask(trimmedName, command),
            _ => StartupOperationResult.Fail("商店应用的自启动任务由应用自身声明，无法手动添加。")
        };
    }

    private static StartupOperationResult AddRegistryValue(
        StartupItemSource source,
        string name,
        string command)
    {
        if (!TryGetRegistryTarget(source, out var hive, out var view, out string keyPath))
        {
            return StartupOperationResult.Fail(
                $"不支持的启动类型：{StartupSourceMetadata.GetAddLabel(source)}。");
        }

        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(hive, view);
            using var key = baseKey.OpenSubKey(keyPath, writable: true)
                            ?? baseKey.CreateSubKey(keyPath, writable: true);
            if (key is null)
            {
                return StartupOperationResult.Fail($"无法打开注册表键 {keyPath}。");
            }

            if (key.GetValue(name) is not null)
            {
                return StartupOperationResult.Fail($"“{name}”已经存在于该键中。");
            }

            key.SetValue(name, command, RegistryValueKind.String);
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException or IOException)
        {
            AppLogService.Warn($"新增自启动项失败：{name}：{exception.Message}", exception);
            return StartupOperationResult.Fail(
                $"无法新增自启动项“{name}”。",
                $"{exception.GetType().Name}：{exception.Message}");
        }

        // 同名的旧审批记录（之前被禁用过）会让新条目一出现就是禁用的，顺手清掉。
        StartupItemProvider.RemoveApproval(source, name);

        string location = StartupSourceMetadata.DescribeRegistrationLocation(source, name);
        AppLogService.Info($"已新增自启动项：{name}（{location}，命令={command}）。");
        return StartupOperationResult.Ok($"已在 {location} 新增“{name}”。");
    }

    private static StartupOperationResult AddStartupShortcut(
        StartupItemSource source,
        string name,
        string executablePath,
        string arguments)
    {
        string? folder = StartupSourceMetadata.GetStartupFolderPath(source);
        if (string.IsNullOrWhiteSpace(folder))
        {
            return StartupOperationResult.Fail("找不到该启动文件夹。");
        }

        string fileName = name + ".lnk";
        string shortcutPath = Path.Combine(folder, fileName);
        if (File.Exists(shortcutPath))
        {
            return StartupOperationResult.Fail($"启动文件夹里已经有同名快捷方式：{shortcutPath}");
        }

        try
        {
            Directory.CreateDirectory(folder);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or SecurityException)
        {
            return StartupOperationResult.Fail(
                $"无法创建启动文件夹 {folder}。",
                $"{exception.GetType().Name}：{exception.Message}");
        }

        if (!ShortcutResolver.TryCreateLink(shortcutPath, executablePath, arguments, name, out string? error))
        {
            AppLogService.Warn($"创建启动文件夹快捷方式失败：{shortcutPath}：{error}");
            return StartupOperationResult.Fail($"无法创建快捷方式“{name}”。", error);
        }

        StartupItemProvider.RemoveApproval(source, fileName);
        AppLogService.Info($"已创建启动文件夹自启动项：{shortcutPath}。");
        return StartupOperationResult.Ok($"已创建启动文件夹快捷方式“{name}”（{shortcutPath}）。");
    }

    private static StartupOperationResult AddScheduledTask(string name, string command)
    {
        if (!StartupTaskProvider.Create(name, command, out string? error))
        {
            AppLogService.Warn($"创建登录时计划任务失败：{name}：{error}");
            return StartupOperationResult.Fail($"无法创建计划任务“{name}”。", error);
        }

        AppLogService.Info($"已创建登录时计划任务：{name}（命令={command}）。");
        return StartupOperationResult.Ok($"已创建“登录时执行”的计划任务“{name}”。");
    }

    /// <summary>Run/RunOnce 的真实登记位置（审批表的位置另见 <see cref="StartupItem.TryGetApprovalTarget"/>）。</summary>
    private static bool TryGetRegistryTarget(
        StartupItemSource source,
        out RegistryHive hive,
        out RegistryView view,
        out string keyPath)
    {
        hive = RegistryHive.CurrentUser;
        view = RegistryView.Default;
        keyPath = StartupSourceMetadata.RunKeyPath;
        switch (source)
        {
            case StartupItemSource.MachineRun:
                hive = RegistryHive.LocalMachine;
                view = Environment.Is64BitOperatingSystem ? RegistryView.Registry64 : RegistryView.Default;
                return true;
            case StartupItemSource.MachineRun32:
                hive = RegistryHive.LocalMachine;
                view = RegistryView.Registry32;
                return true;
            case StartupItemSource.MachineRunOnce:
                hive = RegistryHive.LocalMachine;
                view = Environment.Is64BitOperatingSystem ? RegistryView.Registry64 : RegistryView.Default;
                keyPath = StartupSourceMetadata.RunOnceKeyPath;
                return true;
            case StartupItemSource.UserRun:
                return true;
            case StartupItemSource.UserRunOnce:
                keyPath = StartupSourceMetadata.RunOnceKeyPath;
                return true;
            default:
                return false;
        }
    }
}
