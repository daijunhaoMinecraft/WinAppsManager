using System.Globalization;
using System.IO;
using System.Security;
using Microsoft.Win32;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// 枚举本机的自启动项。覆盖与“任务管理器 → 启动”同一批来源：
/// <list type="bullet">
///   <item>HKLM/HKCU 的 <c>Run</c> 与 <c>RunOnce</c>（HKLM 另含 32 位 <c>WOW6432Node</c> 视图）；</item>
///   <item>“所有用户”和“当前用户”的启动文件夹里的 <c>.lnk</c>；</item>
///   <item>任务计划程序中“登录时触发并且有可执行动作”的任务（见 <see cref="StartupTaskProvider"/>）；</item>
///   <item>Microsoft Store / UWP 应用声明的启动任务（<c>AppModel\SystemAppData</c>）。</item>
/// </list>
/// 启用/禁用状态：注册表与启动文件夹项来自 <c>Explorer\StartupApproved</c> 的二进制值——
/// 首字节最低位为 1 表示已禁用，其后是可选的“禁用时间” FILETIME，值不存在等价于已启用；
/// 计划任务看任务定义里的 <c>&lt;Settings&gt;&lt;Enabled&gt;</c>；UWP 启动任务看 <c>State</c> 值。
/// </summary>
public static class StartupItemProvider
{
    /// <summary>StartupApproved 里“已启用”的 12 字节值：首字节 0x02。</summary>
    private static readonly byte[] EnabledApproval = [0x02, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];

    /// <summary>UWP 启动任务的状态在 <c>AppModel\SystemAppData\&lt;包族名&gt;\&lt;任务 Id&gt;\State</c>。</summary>
    private const string StoreSystemAppDataPath =
        @"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel\SystemAppData";

    /// <summary>UWP <c>StartupTaskState</c>：0=Disabled、1=DisabledByUser、2=Enabled、3=EnabledByPolicy。</summary>
    private const int StoreStateDisabledByUser = 1;
    private const int StoreStateEnabled = 2;
    private const int StoreStateEnabledByPolicy = 3;

    public static Task<IReadOnlyList<StartupItem>> GetStartupItemsAsync(
        CancellationToken cancellationToken = default)
        => Task.Run<IReadOnlyList<StartupItem>>(() => Scan(cancellationToken), cancellationToken);

    public static IReadOnlyList<StartupItem> Scan(CancellationToken cancellationToken = default)
    {
        var items = new List<StartupItem>(64);

        bool is64Bit = Environment.Is64BitOperatingSystem;
        ReadRegistrySource(
            RegistryHive.LocalMachine,
            is64Bit ? RegistryView.Registry64 : RegistryView.Default,
            StartupSourceMetadata.RunKeyPath,
            StartupItemSource.MachineRun,
            machineScope: true,
            items,
            cancellationToken);
        ReadRegistrySource(
            RegistryHive.LocalMachine,
            is64Bit ? RegistryView.Registry64 : RegistryView.Default,
            StartupSourceMetadata.RunOnceKeyPath,
            StartupItemSource.MachineRunOnce,
            machineScope: true,
            items,
            cancellationToken);
        if (is64Bit)
        {
            // 32 位程序的启动项写在 WOW6432Node 里，只有用 Registry32 视图才看得到。
            ReadRegistrySource(
                RegistryHive.LocalMachine,
                RegistryView.Registry32,
                StartupSourceMetadata.RunKeyPath,
                StartupItemSource.MachineRun32,
                machineScope: true,
                items,
                cancellationToken);
        }

        ReadRegistrySource(
            RegistryHive.CurrentUser,
            RegistryView.Default,
            StartupSourceMetadata.RunKeyPath,
            StartupItemSource.UserRun,
            machineScope: false,
            items,
            cancellationToken);
        ReadRegistrySource(
            RegistryHive.CurrentUser,
            RegistryView.Default,
            StartupSourceMetadata.RunOnceKeyPath,
            StartupItemSource.UserRunOnce,
            machineScope: false,
            items,
            cancellationToken);

        ReadStartupFolder(
            Environment.SpecialFolder.CommonStartup,
            StartupItemSource.CommonStartupFolder,
            machineScope: true,
            items,
            cancellationToken);
        ReadStartupFolder(
            Environment.SpecialFolder.Startup,
            StartupItemSource.UserStartupFolder,
            machineScope: false,
            items,
            cancellationToken);

        try
        {
            items.AddRange(StartupTaskProvider.Scan(cancellationToken));
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or SecurityException or InvalidOperationException)
        {
            // 计划任务读取失败不能拖垮其余来源，页面仍会显示注册表与启动文件夹条目。
            AppLogService.Warn($"读取计划任务自启动项失败：{exception.GetType().Name}：{exception.Message}。", exception);
        }

        ReadStoreStartupTasks(items, cancellationToken);

        var (runningPaths, runningExeNames) = GetRunningProcessSnapshot();
        foreach (StartupItem item in items)
        {
            item.IsInvalid = IsTargetMissing(item.IconPath);
            item.IsRunning = !item.IsInvalid && IsProcessRunning(item.IconPath, runningPaths, runningExeNames);
        }

        return items;
    }
    
    /// <summary>
    /// 判断目标文件是否缺失。仅检查明确带有路径的文件（排除 UWP 空路径以及依赖环境变量 PATH 查找的裸命令如 rundll32.exe）。
    /// </summary>
    private static bool IsTargetMissing(string iconPath)
    {
        if (string.IsNullOrWhiteSpace(iconPath))
        {
            return false;
        }

        string expanded = Environment.ExpandEnvironmentVariables(iconPath.Trim());
        return Path.IsPathRooted(expanded) && !File.Exists(expanded);
    }

    /// <summary>
    /// 重新读取某条目当前的启用状态。切换后用它回读确认，避免只相信写入返回值。
    /// </summary>
    public static bool ReadIsEnabled(StartupItem item)
    {
        ArgumentNullException.ThrowIfNull(item);
        return item.Source switch
        {
            StartupItemSource.ScheduledTask => StartupTaskProvider.ReadIsEnabled(item.Location),
            StartupItemSource.StoreStartupTask => ReadStoreStateEnabled(item),
            _ => !TryReadApprovalDisabled(item.Source, item.ApprovalValueName)
        };
    }

    /// <summary>
    /// 写入启用状态：注册表/启动文件夹项写 <c>StartupApproved</c>（启用 0x02，禁用 0x03 加当前
    /// FILETIME），计划任务交给 schtasks，UWP 启动任务写 <c>State</c>。条目不删除，随时可还原。
    /// </summary>
    public static bool WriteEnabled(StartupItem item, bool enabled, out string? error)
    {
        ArgumentNullException.ThrowIfNull(item);
        error = null;

        switch (item.Source)
        {
            case StartupItemSource.ScheduledTask:
                return StartupTaskProvider.SetEnabled(item.Location, enabled, out error);
            case StartupItemSource.StoreStartupTask:
                return WriteStoreState(item, enabled, out error);
            default:
                return WriteApproval(item.Source, item.ApprovalValueName, enabled, out error);
        }
    }

    /// <summary>
    /// 删除审批表里对应的值。条目本身被删掉之后，残留的审批值只会变成孤儿记录（任务管理器的
    /// 启动页里就常见这种幽灵条目），所以删除启动项时一并清掉。
    /// </summary>
    public static void RemoveApproval(StartupItemSource source, string approvalValueName)
    {
        if (!StartupItem.TryGetApprovalTarget(source, out var hive, out var view, out string subKeyPath)
            || string.IsNullOrEmpty(approvalValueName))
        {
            return;
        }

        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(hive, view);
            using var key = baseKey.OpenSubKey(subKeyPath, writable: true);
            key?.DeleteValue(approvalValueName, throwOnMissingValue: false);
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException
                                              or IOException or PlatformNotSupportedException)
        {
            AppLogService.Warn($"清除 StartupApproved 记录失败：{approvalValueName}：{exception.Message}", exception);
        }
    }

    private static bool WriteApproval(
        StartupItemSource source,
        string approvalValueName,
        bool enabled,
        out string? error)
    {
        error = null;
        if (!StartupItem.TryGetApprovalTarget(source, out var hive, out var view, out string subKeyPath))
        {
            error = "该来源不使用 StartupApproved 表。";
            return false;
        }

        if (string.IsNullOrEmpty(approvalValueName))
        {
            error = "缺少用于写入审批表的值名。";
            return false;
        }

        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(hive, view);
            using var key = baseKey.CreateSubKey(subKeyPath, writable: true);
            if (key is null)
            {
                error = $"无法打开或创建注册表键 {subKeyPath}。";
                return false;
            }

            byte[] data = enabled ? EnabledApproval : BuildDisabledApproval();
            key.SetValue(approvalValueName, data, RegistryValueKind.Binary);
            return true;
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException
                                              or IOException or PlatformNotSupportedException)
        {
            error = $"{exception.GetType().Name}：{exception.Message}";
            return false;
        }
    }

    // ---- Microsoft Store / UWP 启动任务 ----

    private static void ReadStoreStartupTasks(List<StartupItem> items, CancellationToken cancellationToken)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(StoreSystemAppDataPath);
            if (key is null)
            {
                return;
            }

            // 先只收集 (包族名, 任务 Id, State)：显示名、发布者与图标要按包族名批量解析，
            // 一次查询覆盖所有包，比每个任务各查一次快得多。
            var records = new List<(string FamilyName, string TaskId, int State)>(16);
            foreach (string packageFamilyName in key.GetSubKeyNames())
            {
                cancellationToken.ThrowIfCancellationRequested();
                using var packageKey = key.OpenSubKey(packageFamilyName);
                if (packageKey is null)
                {
                    continue;
                }

                foreach (string taskId in packageKey.GetSubKeyNames())
                {
                    // PSR / Schemas 是包级状态，不是启动任务。
                    if (IsStorePackageStateKey(taskId))
                    {
                        continue;
                    }

                    using var taskKey = packageKey.OpenSubKey(taskId);
                    if (taskKey?.GetValue("State") is int stateValue)
                    {
                        records.Add((packageFamilyName, taskId, stateValue));
                    }
                }
            }

            if (records.Count == 0)
            {
                return;
            }

            string[] familyNames = records
                .Select(record => record.FamilyName)
                .Distinct(StringComparer.OrdinalIgnoreCase)
                .ToArray();
            IReadOnlyDictionary<string, StoreApplicationProvider.StorePackageSummary> summaries =
                StoreApplicationProvider.ResolvePackageSummaries(familyNames, cancellationToken);

            foreach (var record in records)
            {
                summaries.TryGetValue(record.FamilyName, out StoreApplicationProvider.StorePackageSummary summary);
                string displayName = summary.DisplayName.Length > 0 ? summary.DisplayName : record.TaskId;
                items.Add(new StartupItem
                {
                    Id = $"store:{record.FamilyName}\\{record.TaskId}",
                    // 任务 Id（App / TeamsTfwStartupTask）只在取不到商店应用名时才会显示出来。
                    RegistrationName = record.TaskId,
                    Name = displayName,
                    CompanyName = summary.Publisher,
                    Source = StartupItemSource.StoreStartupTask,
                    Command = $"Windows 应用启动任务：{displayName}",
                    Location = $@"HKEY_CURRENT_USER\{StoreSystemAppDataPath}\{record.FamilyName}\{record.TaskId}",
                    ApprovalValueName = record.TaskId,
                    StorePackageFamilyName = record.FamilyName,
                    // 商店应用的图标是包里的 PNG，交给图标服务按位图加载。
                    IconPath = summary.LogoPath,
                    IsMachineScope = false,
                    // 启动任务由应用自己在清单里声明，删除没有意义，只能启用/禁用。
                    CanDelete = false,
                    IsEnabled = IsStoreStateEnabled(record.State)
                });
            }
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException
                                              or IOException or PlatformNotSupportedException)
        {
            AppLogService.Warn(
                $"读取应用启动任务失败：{exception.GetType().Name}：{exception.Message}。",
                exception);
        }
    }

    /// <summary>包级状态子键（不是启动任务）：<c>PSR</c> 与 <c>Schemas</c>。</summary>
    private static bool IsStorePackageStateKey(string name)
        => string.Equals(name, "PSR", StringComparison.OrdinalIgnoreCase)
           || string.Equals(name, "Schemas", StringComparison.OrdinalIgnoreCase);

    private static bool ReadStoreStateEnabled(StartupItem item)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(
                $@"{StoreSystemAppDataPath}\{item.StorePackageFamilyName}\{item.ApprovalValueName}");
            return key?.GetValue("State") is int state && IsStoreStateEnabled(state);
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException
                                              or IOException or PlatformNotSupportedException)
        {
            return false;
        }
    }

    private static bool IsStoreStateEnabled(int state)
        => state is StoreStateEnabled or StoreStateEnabledByPolicy;

    private static bool WriteStoreState(StartupItem item, bool enabled, out string? error)
    {
        error = null;
        if (string.IsNullOrEmpty(item.StorePackageFamilyName) || string.IsNullOrEmpty(item.ApprovalValueName))
        {
            error = "应用启动任务缺少包族名或任务 Id。";
            return false;
        }

        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(
                $@"{StoreSystemAppDataPath}\{item.StorePackageFamilyName}\{item.ApprovalValueName}",
                writable: true);
            if (key is null)
            {
                error = "找不到该应用启动任务的注册表项。";
                return false;
            }

            // EnabledByPolicy(3) 由组策略控制，改它没有意义。
            if (!enabled && key.GetValue("State") is int current && current == StoreStateEnabledByPolicy)
            {
                error = "该启动任务由组策略启用，无法在此禁用。";
                return false;
            }

            key.SetValue("State", enabled ? StoreStateEnabled : StoreStateDisabledByUser, RegistryValueKind.DWord);
            return true;
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException
                                              or IOException or PlatformNotSupportedException)
        {
            error = $"{exception.GetType().Name}：{exception.Message}";
            return false;
        }
    }

    /// <summary>读取审批表，判断该条目是否被禁用。值不存在时视为启用。</summary>
    private static bool TryReadApprovalDisabled(StartupItemSource source, string approvalValueName)
    {
        if (!StartupItem.TryGetApprovalTarget(source, out var hive, out var view, out string subKeyPath)
            || string.IsNullOrEmpty(approvalValueName))
        {
            return false;
        }

        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(hive, view);
            using var key = baseKey.OpenSubKey(subKeyPath);
            if (key?.GetValue(approvalValueName) is not byte[] data || data.Length == 0)
            {
                return false;
            }

            return (data[0] & 1) != 0;
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException
                                              or IOException or PlatformNotSupportedException)
        {
            return false;
        }
    }

    private static byte[] BuildDisabledApproval()
    {
        var data = new byte[12];
        data[0] = 0x03;
        long fileTime = DateTime.UtcNow.ToFileTimeUtc();
        BitConverter.TryWriteBytes(data.AsSpan(4), fileTime);
        return data;
    }

    private static void ReadRegistrySource(
        RegistryHive hive,
        RegistryView view,
        string keyPath,
        StartupItemSource source,
        bool machineScope,
        ICollection<StartupItem> items,
        CancellationToken cancellationToken)
    {
        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(hive, view);
            using var key = baseKey.OpenSubKey(keyPath);
            if (key is null)
            {
                return;
            }

            string hiveLabel = hive == RegistryHive.LocalMachine ? "HKEY_LOCAL_MACHINE" : "HKEY_CURRENT_USER";
            foreach (string valueName in key.GetValueNames())
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (valueName.Length == 0)
                {
                    continue;
                }

                string command = ReadValueText(key, valueName);
                if (command.Length == 0)
                {
                    // 空值没有任何可执行内容，列出来只会是噪音。
                    continue;
                }

                bool isEnabled = !TryReadApprovalDisabled(source, valueName);
                string executable = ExecutableMetadataService.ResolveExecutablePath(command);
                ExecutableMetadata metadata = ExecutableMetadataService.Read(executable);
                items.Add(new StartupItem
                {
                    Id = $"reg:{source}:{valueName}",
                    // 值名经常是 App/Update 这类看不出是什么软件的字符串，显示名优先用 exe 的文件说明。
                    RegistrationName = valueName,
                    Name = ExecutableMetadataService.ResolveDisplayName(valueName, metadata),
                    CompanyName = metadata.CompanyName,
                    Source = source,
                    Command = command,
                    Location = $@"{hiveLabel}\{keyPath}",
                    ApprovalValueName = valueName,
                    IconPath = executable,
                    IsMachineScope = machineScope,
                    IsEnabled = isEnabled
                });
            }
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException
                                              or IOException or PlatformNotSupportedException)
        {
            // 单个配置单元不可访问时跳过，其余来源继续。
            AppLogService.Warn(
                $"读取启动项注册表失败（{hive}/{view}/{keyPath}）：{exception.GetType().Name}：{exception.Message}。",
                exception);
        }
    }

    /// <summary>读取字符串值；<c>REG_EXPAND_SZ</c> 保留原始文本，展示与图标解析各自决定是否展开。</summary>
    private static string ReadValueText(RegistryKey key, string valueName)
    {
        object? value = key.GetValue(valueName, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
        return value switch
        {
            string text => text.Trim(),
            string[] parts => string.Join(' ', parts).Trim(),
            _ => string.Empty
        };
    }

    private static void ReadStartupFolder(
        Environment.SpecialFolder folder,
        StartupItemSource source,
        bool machineScope,
        ICollection<StartupItem> items,
        CancellationToken cancellationToken)
    {
        string directory;
        try
        {
            directory = Environment.GetFolderPath(folder);
        }
        catch (Exception exception) when (exception is ArgumentException or PlatformNotSupportedException)
        {
            return;
        }

        if (string.IsNullOrWhiteSpace(directory) || !Directory.Exists(directory))
        {
            return;
        }

        string[] shortcuts;
        try
        {
            shortcuts = Directory.GetFiles(directory, "*.lnk");
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or SecurityException)
        {
            AppLogService.Warn($"读取启动文件夹失败（{directory}）：{exception.GetType().Name}：{exception.Message}。", exception);
            return;
        }

        // 所有快捷方式一次解析：WScript.Shell 必须在同一个 STA 线程上批量处理。
        var targets = ShortcutResolver.ResolveTargets(shortcuts);
        foreach (string shortcut in shortcuts)
        {
            cancellationToken.ThrowIfCancellationRequested();
            string fileName = Path.GetFileName(shortcut);
            if (fileName.Length == 0)
            {
                continue;
            }

            string command = shortcut;
            string iconPath = shortcut;
            if (targets.TryGetValue(shortcut, out var target))
            {
                command = target.Arguments.Length > 0
                    ? $"\"{target.TargetPath}\" {target.Arguments}"
                    : $"\"{target.TargetPath}\"";
                // 目标可能带参数或环境变量，这里只要可执行文件本身。
                string resolved = ExecutableMetadataService.ResolveExecutablePath(target.TargetPath);
                iconPath = resolved.Length > 0 ? resolved : shortcut;
            }

            string registrationName = Path.GetFileNameWithoutExtension(shortcut);
            ExecutableMetadata metadata = string.Equals(iconPath, shortcut, StringComparison.OrdinalIgnoreCase)
                ? ExecutableMetadata.Empty
                : ExecutableMetadataService.Read(iconPath);

            items.Add(new StartupItem
            {
                Id = $"lnk:{source}:{shortcut.ToUpperInvariant()}",
                RegistrationName = registrationName,
                Name = ExecutableMetadataService.ResolveDisplayName(registrationName, metadata),
                CompanyName = metadata.CompanyName,
                Source = source,
                Command = command,
                Location = shortcut,
                // 审批表用“含扩展名的文件名”作为键，例如 Ollama.lnk。
                ApprovalValueName = fileName,
                IconPath = iconPath,
                IsMachineScope = machineScope,
                IsEnabled = !TryReadApprovalDisabled(source, fileName)
            });
        }
    }

    /// <summary>把审批表的时间戳格式化成可读文本（仅日志与诊断使用）。</summary>
    public static string DescribeApprovalTimestamp(byte[] data)
    {
        if (data.Length < 12)
        {
            return string.Empty;
        }

        long fileTime = BitConverter.ToInt64(data, 4);
        if (fileTime <= 0)
        {
            return string.Empty;
        }

        try
        {
            return DateTime.FromFileTimeUtc(fileTime)
                .ToLocalTime()
                .ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture);
        }
        catch (ArgumentOutOfRangeException)
        {
            return string.Empty;
        }
    }
    
    private static (HashSet<string> Paths, HashSet<string> ExeNames) GetRunningProcessSnapshot()
    {
        var paths = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var exeNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        foreach (var process in System.Diagnostics.Process.GetProcesses())
        {
            using (process)
            {
                try
                {
                    if (!string.IsNullOrWhiteSpace(process.ProcessName))
                    {
                        exeNames.Add(process.ProcessName);
                    }

                    string? fileName = process.MainModule?.FileName;
                    if (!string.IsNullOrWhiteSpace(fileName))
                    {
                        paths.Add(fileName);
                    }
                }
                catch
                {
                    // 系统核心进程或高权限进程可能拒绝访问 MainModule，保留 ProcessName 用于兜底匹配
                }
            }
        }

        return (paths, exeNames);
    }

    private static bool IsProcessRunning(
        string iconPath,
        HashSet<string> runningPaths,
        HashSet<string> runningExeNames)
    {
        if (string.IsNullOrWhiteSpace(iconPath))
        {
            return false;
        }

        string expanded = Environment.ExpandEnvironmentVariables(iconPath.Trim());
        if (runningPaths.Contains(expanded))
        {
            return true;
        }

        // 仅对 .exe 匹配进程名（避免把未运行的 .bat / .vbs 等脚本误判为运行中）
        if (expanded.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
        {
            string nameWithoutExt = Path.GetFileNameWithoutExtension(expanded);
            return nameWithoutExt.Length > 0 && runningExeNames.Contains(nameWithoutExt);
        }

        return false;
    }
}
