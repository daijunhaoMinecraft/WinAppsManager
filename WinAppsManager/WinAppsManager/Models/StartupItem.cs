using System.ComponentModel;
using System.Globalization;
using System.IO;
using System.Runtime.CompilerServices;
using System.Windows.Media;
using Microsoft.Win32;

namespace WinAppsManager.Models;

/// <summary>
/// 自启动项的登记位置。<see cref="StartupItemSource"/> 的取值顺序与菜单/筛选顺序一致，
/// 新增来源时同步 <see cref="StartupSourceMetadata"/>。
/// </summary>
public enum StartupItemSource
{
    /// <summary>HKLM 的 64 位 Run 键（64 位程序的开机自启）。</summary>
    MachineRun,

    /// <summary>HKLM 的 32 位 Run 键：64 位系统上就是 WOW6432Node，32 位程序写在这里。</summary>
    MachineRun32,

    /// <summary>HKLM 的 RunOnce 键（只运行一次）。</summary>
    MachineRunOnce,

    /// <summary>HKCU 的 Run 键（当前用户的开机自启）。</summary>
    UserRun,

    /// <summary>HKCU 的 RunOnce 键（当前用户，只运行一次）。</summary>
    UserRunOnce,

    /// <summary>“所有用户”的启动文件夹（%ProgramData% 下的 Start Menu\Programs\Startup）。</summary>
    CommonStartupFolder,

    /// <summary>当前用户的启动文件夹（%APPDATA% 下的 Start Menu\Programs\Startup）。</summary>
    UserStartupFolder,

    /// <summary>任务计划程序中“登录时触发”的任务。</summary>
    ScheduledTask,

    /// <summary>Microsoft Store / UWP 应用自带的启动任务（任务管理器“启动”页里的商店应用）。</summary>
    StoreStartupTask
}

/// <summary>页面筛选用的三大类登记位置。</summary>
public enum StartupItemKind
{
    Registry,
    StartupFolder,
    ScheduledTask,
    StoreApp
}

/// <summary>
/// 一条自启动项的界面模型：注册表 Run/RunOnce 值、启动文件夹快捷方式或计划任务。
/// 只存放展示和后续操作需要的字段，真正的读写由 <c>StartupItemService</c> 负责。
/// </summary>
public sealed class StartupItem : INotifyPropertyChanged
{
    private static readonly string[] AvatarPalette =
    [
        "#0067C0", "#0F6CBD", "#107C10", "#8764B8", "#C239B3", "#CA5010",
        "#986F0B", "#7A7574", "#005B70", "#4F6BED", "#C50F1F", "#038387"
    ];

    private bool _isEnabled;
    private ImageSource? _iconSource;

    public event PropertyChangedEventHandler? PropertyChanged;

    /// <summary>稳定标识：来源 + 注册表键 + 值名（或快捷方式路径、任务路径），用于刷新后比对。</summary>
    public required string Id { get; init; }

    /// <summary>
    /// 显示名称：优先取程序 exe 的“文件说明”（与任务管理器“启动”页一致），取不到时回退
    /// <see cref="RegistrationName"/>。注册表值名常常是 <c>App</c>、<c>Update</c> 这种看不出是什么
    /// 软件的字符串，直接显示会让人认不出条目。
    /// </summary>
    public required string Name { get; init; }

    /// <summary>登记名：注册表值名、快捷方式文件名（不含扩展名）或计划任务名。搜索与悬浮提示用。</summary>
    public required string RegistrationName { get; init; }

    public required StartupItemSource Source { get; init; }

    /// <summary>程序的公司名（exe 版本信息里的 CompanyName），取不到时为空。</summary>
    public string CompanyName { get; init; } = string.Empty;

    /// <summary>计划任务定义里的描述（<c>RegistrationInfo/Description</c>），其他来源为空。</summary>
    public string Description { get; init; } = string.Empty;

    public bool HasDescription => Description.Length > 0;

    /// <summary>实际会执行的命令行；注册表项是值数据，启动文件夹是快捷方式目标，任务是命令 + 参数。</summary>
    public string Command { get; init; } = string.Empty;

    /// <summary>完整登记位置：注册表键路径、<c>.lnk</c> 完整路径或计划任务路径（形如 <c>\Microsoft\Windows\…</c>）。</summary>
    public string Location { get; init; } = string.Empty;

    /// <summary>
    /// 写入 <c>StartupApproved</c> 时使用的值名。注册表项就是 Run 值名；启动文件夹项必须带
    /// <c>.lnk</c> 扩展名，Windows 用文件名（含扩展名）作为审批表的键。
    /// </summary>
    public string ApprovalValueName { get; init; } = string.Empty;

    /// <summary>用于提取图标的文件：可执行文件或快捷方式。</summary>
    public string IconPath { get; init; } = string.Empty;

    /// <summary>是否为“所有用户”范围（HKLM / 公共启动文件夹 / 以 SYSTEM 或 Everyone 运行的任务）。</summary>
    public bool IsMachineScope { get; init; }

    /// <summary>UWP 启动任务所属的包族名（仅 <see cref="StartupItemSource.StoreStartupTask"/>）。</summary>
    public string StorePackageFamilyName { get; init; } = string.Empty;

    /// <summary>
    /// Microsoft Store 产品页链接（<c>ms-windows-store://pdp/?PFN=…</c>），只有 UWP 启动任务有。
    /// 没有从商店安装的包打开后会被 Store 提示找不到产品，这是 Store 自己的行为。
    /// </summary>
    public string StoreUri => Source == StartupItemSource.StoreStartupTask
                              && StorePackageFamilyName.Length > 0
        ? $"ms-windows-store://pdp/?PFN={StorePackageFamilyName}"
        : string.Empty;

    /// <summary>右键菜单里是否可以提供“在 Microsoft Store 中查看”。</summary>
    public bool CanOpenStore => StoreUri.Length > 0;

    /// <summary>是否支持启用/禁用切换。</summary>
    public bool CanToggle { get; init; } = true;

    /// <summary>是否支持删除。UWP 启动任务由应用自己声明，只能启用/禁用。</summary>
    public bool CanDelete { get; init; } = true;
    
    /// <summary> 启动项是否有效 </summary>
    public bool IsInvalid { get; set; }
    
    /// <summary> 对应的程序当前是否正在运行 </summary>
    public bool IsRunning { get; set; }

    /// <summary> 列表“状态”列显示文本：优先显示“无效的”，其次“运行中”，其余留空 </summary>
    public string RuntimeStatusText => IsInvalid
        ? "无效的"
        : IsRunning
            ? "运行中"
            : string.Empty;    

    public string KindText => StartupSourceMetadata.GetKindText(StartupSourceMetadata.GetKind(Source));

    public string SourceText => StartupSourceMetadata.GetDisplayName(Source);

    public string ScopeText => IsMachineScope ? "所有用户" : "当前用户";

    public bool IsEnabled
    {
        get => _isEnabled;
        set
        {
            if (_isEnabled == value)
            {
                return;
            }

            _isEnabled = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(StatusText));
        }
    }

    public string StatusText => _isEnabled ? "已启用" : "已禁用";
    
    public ImageSource? IconSource
    {
        get => _iconSource;
        set
        {
            if (ReferenceEquals(_iconSource, value))
            {
                return;
            }

            _iconSource = value;
            OnPropertyChanged();
            OnPropertyChanged(nameof(HasIcon));
        }
    }

    public bool HasIcon => _iconSource is not null;

    public string Initial
    {
        get
        {
            if (Name.Length == 0)
            {
                return "?";
            }

            var element = StringInfo.GetNextTextElement(Name);
            return element.ToUpperInvariant();
        }
    }

    public string AvatarBackground => AvatarPalette[GetStableHash(Name) % AvatarPalette.Length];

    /// <summary>命令行不可用时的提示，避免列表里出现空白行。</summary>
    public string CommandText => Command.Length > 0 ? Command : "（无法读取命令行）";

    /// <summary>
    /// 名称的悬浮提示：计划任务显示任务定义里的描述（任务计划程序“常规”页那段文字）；
    /// 显示名被换成文件说明后，再补一行原始登记名，否则用户看不出这条登记本来叫什么。
    /// 两者都没有时返回 <c>null</c>，不显示空提示。
    /// </summary>
    public string? NameToolTip
    {
        get
        {
            bool renamed = !string.Equals(Name, RegistrationName, StringComparison.OrdinalIgnoreCase);
            if (Description.Length > 0)
            {
                return renamed
                    ? $"{Description}{Environment.NewLine}登记名：{RegistrationName}"
                    : Description;
            }

            return renamed ? $"登记名：{RegistrationName}" : null;
        }
    }

    /// <summary>根据来源推导 <c>StartupApproved</c> 所在的注册表配置单元与子键。</summary>
    public static bool TryGetApprovalTarget(
        StartupItemSource source,
        out RegistryHive hive,
        out RegistryView view,
        out string subKeyPath)
    {
        hive = RegistryHive.CurrentUser;
        view = RegistryView.Default;
        subKeyPath = string.Empty;
        switch (source)
        {
            case StartupItemSource.MachineRun:
            case StartupItemSource.MachineRunOnce:
                hive = RegistryHive.LocalMachine;
                view = Environment.Is64BitOperatingSystem ? RegistryView.Registry64 : RegistryView.Default;
                subKeyPath = StartupSourceMetadata.ApprovalRunKeyPath;
                return true;
            case StartupItemSource.MachineRun32:
                hive = RegistryHive.LocalMachine;
                view = RegistryView.Registry32;
                subKeyPath = StartupSourceMetadata.ApprovalRun32KeyPath;
                return true;
            case StartupItemSource.UserRun:
            case StartupItemSource.UserRunOnce:
                hive = RegistryHive.CurrentUser;
                view = RegistryView.Default;
                subKeyPath = StartupSourceMetadata.ApprovalRunKeyPath;
                return true;
            case StartupItemSource.CommonStartupFolder:
                hive = RegistryHive.LocalMachine;
                view = Environment.Is64BitOperatingSystem ? RegistryView.Registry64 : RegistryView.Default;
                subKeyPath = StartupSourceMetadata.ApprovalStartupFolderKeyPath;
                return true;
            case StartupItemSource.UserStartupFolder:
                hive = RegistryHive.CurrentUser;
                view = RegistryView.Default;
                subKeyPath = StartupSourceMetadata.ApprovalStartupFolderKeyPath;
                return true;
            default:
                // 计划任务的开关状态存在任务自身里，通过 schtasks 切换，不写审批表。
                return false;
        }
    }

    private void OnPropertyChanged([CallerMemberName] string? propertyName = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(propertyName));

    /// <summary>
    /// 与 <see cref="InstalledApplication"/> 相同的稳定散列：同一名称永远得到同一个头像底色，
    /// 刷新列表时不会因为顺序变化而跳动。
    /// </summary>
    private static int GetStableHash(string text)
    {
        unchecked
        {
            int hash = 17;
            foreach (char character in text)
            {
                hash = (hash * 31) + character;
            }

            return Math.Abs(hash == int.MinValue ? int.MaxValue : hash);
        }
    }
}

/// <summary>来源枚举与界面文案、注册表路径的映射，集中一处以免散落在各服务里。</summary>
public static class StartupSourceMetadata
{
    public const string RunKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Run";
    public const string RunOnceKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce";
    public const string ApprovalRootPath =
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved";
    public const string ApprovalRunKeyPath = ApprovalRootPath + @"\Run";
    public const string ApprovalRun32KeyPath = ApprovalRootPath + @"\Run32";
    public const string ApprovalStartupFolderKeyPath = ApprovalRootPath + @"\StartupFolder";

    public static string GetDisplayName(StartupItemSource source) => source switch
    {
        StartupItemSource.MachineRun => "注册表 · 所有用户 (64 位)",
        StartupItemSource.MachineRun32 => "注册表 · 所有用户 (32 位)",
        StartupItemSource.MachineRunOnce => "注册表 · 所有用户（仅一次）",
        StartupItemSource.UserRun => "注册表 · 当前用户",
        StartupItemSource.UserRunOnce => "注册表 · 当前用户（仅一次）",
        StartupItemSource.CommonStartupFolder => "启动文件夹 · 所有用户",
        StartupItemSource.UserStartupFolder => "启动文件夹 · 当前用户",
        StartupItemSource.ScheduledTask => "计划任务 · 登录时",
        StartupItemSource.StoreStartupTask => "应用启动任务 · 商店应用",
        _ => source.ToString()
    };

    public static StartupItemKind GetKind(StartupItemSource source) => source switch
    {
        StartupItemSource.CommonStartupFolder or StartupItemSource.UserStartupFolder => StartupItemKind.StartupFolder,
        StartupItemSource.ScheduledTask => StartupItemKind.ScheduledTask,
        StartupItemSource.StoreStartupTask => StartupItemKind.StoreApp,
        _ => StartupItemKind.Registry
    };

    public static string GetKindText(StartupItemKind kind) => kind switch
    {
        StartupItemKind.StartupFolder => "启动文件夹",
        StartupItemKind.ScheduledTask => "计划任务",
        StartupItemKind.StoreApp => "应用启动任务",
        _ => "注册表"
    };

    /// <summary>页面“来源”下拉框的选项顺序：全部 + 四大类。</summary>
    public static readonly (string Label, StartupItemKind? Kind)[] FilterOptions =
    [
        ("全部来源", null),
        ("注册表", StartupItemKind.Registry),
        ("启动文件夹", StartupItemKind.StartupFolder),
        ("计划任务", StartupItemKind.ScheduledTask),
        ("应用启动任务", StartupItemKind.StoreApp)
    ];

    /// <summary>
    /// “添加自启动项”对话框里“启动类型”的顺序（与参考实现 UninstallTool 一致）：
    /// 两种 Run、两种 RunOnce、两个启动文件夹、一个登录时任务。HKLM 的 32 位 Run 键只在
    /// 64 位系统上单独存在，32 位系统上它就是默认视图，因此那里不列出。
    /// </summary>
    public static IReadOnlyList<StartupItemSource> AddableSources { get; } = BuildAddableSources();

    private static StartupItemSource[] BuildAddableSources()
    {
        var sources = new List<StartupItemSource>(8)
        {
            StartupItemSource.MachineRun
        };

        if (Environment.Is64BitOperatingSystem)
        {
            sources.Add(StartupItemSource.MachineRun32);
        }

        sources.Add(StartupItemSource.UserRun);
        sources.Add(StartupItemSource.MachineRunOnce);
        sources.Add(StartupItemSource.UserRunOnce);
        sources.Add(StartupItemSource.CommonStartupFolder);
        sources.Add(StartupItemSource.UserStartupFolder);
        sources.Add(StartupItemSource.ScheduledTask);
        return sources.ToArray();
    }

    /// <summary>“启动类型”下拉框里的文案（比列表里的“来源”更口语化）。</summary>
    public static string GetAddLabel(StartupItemSource source) => source switch
    {
        StartupItemSource.MachineRun => "所有用户 Run 键",
        StartupItemSource.MachineRun32 => "所有用户 Run 键（32 位）",
        StartupItemSource.UserRun => "当前用户 Run 键",
        StartupItemSource.MachineRunOnce => "所有用户 Run Once 键",
        StartupItemSource.UserRunOnce => "当前用户 Run Once 键",
        StartupItemSource.CommonStartupFolder => "所有用户开始菜单启动文件夹",
        StartupItemSource.UserStartupFolder => "当前用户开始菜单启动文件夹",
        StartupItemSource.ScheduledTask => "登录时执行的 Windows 任务计划",
        _ => GetDisplayName(source)
    };

    /// <summary>启动文件夹路径（<c>null</c> 表示该系统上没有这个文件夹）。</summary>
    public static string? GetStartupFolderPath(StartupItemSource source) => source switch
    {
        StartupItemSource.CommonStartupFolder => Environment.GetFolderPath(Environment.SpecialFolder.CommonStartup),
        StartupItemSource.UserStartupFolder => Environment.GetFolderPath(Environment.SpecialFolder.Startup),
        _ => null
    };

    /// <summary>新增条目会被写到哪里，用于对话框里的“写入位置”提示与结果文案。</summary>
    public static string DescribeRegistrationLocation(StartupItemSource source, string name)
    {
        string fileName = name + ".lnk";
        return source switch
        {
            StartupItemSource.MachineRun => $@"HKEY_LOCAL_MACHINE\{RunKeyPath}（64 位视图）",
            StartupItemSource.MachineRun32 => $@"HKEY_LOCAL_MACHINE\{RunKeyPath}（32 位视图，WOW6432Node）",
            StartupItemSource.MachineRunOnce => $@"HKEY_LOCAL_MACHINE\{RunOnceKeyPath}",
            StartupItemSource.UserRun => $@"HKEY_CURRENT_USER\{RunKeyPath}",
            StartupItemSource.UserRunOnce => $@"HKEY_CURRENT_USER\{RunOnceKeyPath}",
            StartupItemSource.CommonStartupFolder or StartupItemSource.UserStartupFolder =>
                Path.Combine(GetStartupFolderPath(source) ?? string.Empty, fileName),
            StartupItemSource.ScheduledTask => $@"任务计划程序库\{name}",
            _ => GetDisplayName(source)
        };
    }
}
