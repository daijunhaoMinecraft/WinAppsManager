using System.IO;
using System.Security;
using System.Text.RegularExpressions;
using Microsoft.Win32;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Static leftover scanner:
/// <list type="bullet">
///   <item>The registry walk recurses through <c>Software</c> in HKCU and in
///         both HKLM views, and adds a key when its leaf name equals an application name; the
///         eight branches in <see cref="RegistryScanSkipList"/> are never entered.</item>
///   <item>The file walk adds the install folder, AppData/ProgramData folders,
///         and shortcuts under the Start Menu, Desktop and Quick Launch roots whose target is
///         inside the install folder.</item>
///   <item>The MSI walk adds Windows Installer product records, components
///         whose values resolve into the install folder, and <c>Installer\Folders</c> values.</item>
/// </list>
/// Install-time monitoring records and vendor rules are not scanned: this application does not
/// capture installs, so it has no trustworthy source for them. The startup and service groups are
/// local extensions; the normal scanner does not walk <c>SYSTEM</c> or read startup values.
/// File and MSI groups are marked risky and start unchecked.
/// </summary>
public static partial class LeftoverScanner
{
    private const int MaxRegistryDepth = 32;
    private const int MaxVisitedKeys = 200000;
    private const int MaxInstallerComponents = 20000;
    private const int MaxShortcutDepth = 3;
    private const int MaxTreeDepth = 12;
    private const int MaxTreeEntries = 5000;

    private const string UninstallKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall";
    private const string RunKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Run";
    private const string RunOnceKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce";
    private const string AppPathsKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths";
    private const string ServicesKeyPath = @"SYSTEM\CurrentControlSet\Services";
    private const string InstallerKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer";
    private const string StartupApprovedRunPath =
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run";
    private const string StartupApprovedFolderPath =
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder";
    private const string AppModelKeyPath =
        @"SOFTWARE\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel";

    /// <summary>
    /// The eight branches the registry walk never recurses into. They are shared system containers:
    /// matching a key inside them by name would offer Windows or Installer data for deletion. The
    /// cleaner re-uses this list to refuse deleting a key in these branches.
    /// </summary>
    private static readonly string[] RegistryScanSkipList =
    [
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\SideBySide",
        @"SOFTWARE\Microsoft\Fusion",
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer",
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
        @"SOFTWARE\Microsoft\.NETFramework",
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing",
        @"SOFTWARE\Wow6432Node",
        @"SOFTWARE\Classes"
    ];

    private static readonly HashSet<string> GenericNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "microsoft", "windows", "program files", "app", "apps", "application", "update", "updates",
        "setup", "installer", "install", "bin", "common", "common files", "system", "temp", "data",
        "config", "settings", "user", "users", "tools", "software", "uninstall", "uninstaller"
    };

    public static Task<IReadOnlyList<LeftoverNode>> ScanAsync(
        InstalledApplication application,
        CancellationToken cancellationToken = default)
        => Task.Run<IReadOnlyList<LeftoverNode>>(
            () => Scan(application, cancellationToken),
            cancellationToken);

    public static IReadOnlyList<LeftoverNode> Scan(
        InstalledApplication application,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(application);
        if (application.IsStoreApplication)
        {
            // Store packages keep their state under AppModel\PolicyCache / SystemAppData.
            return ScanStoreApplication(application);
        }

        if (!application.HasRegistryEntry)
        {
            return Array.Empty<LeftoverNode>();
        }

        var candidates = BuildNameCandidates(application);
        // For a hidden patch key like "{GUID}.KB2151757" the split identifier (GUID, KB) is useful
        // for registry/MSI leftovers, but it must not make the parent product's own uninstall key
        // (named just "{GUID}") look like residue. The uninstall group therefore only uses the
        // full key name.
        IReadOnlyCollection<string> uninstallCandidates = application.IsHiddenComponent
            ? BuildHiddenKeyCandidates(application)
            : candidates;
        var groups = new List<LeftoverNode>();

        AddGroup(groups, "卸载信息", "注册表中残留的卸载登记项", risky: false,
            ScanUninstallEntries(application, uninstallCandidates, cancellationToken));
        AddGroup(groups, "软件数据", @"HKCU / HKLM\Software 下与该软件同名的注册表键（递归匹配键名）", risky: false,
            ScanProductKeys(candidates, cancellationToken));
        AddGroup(groups, "启动项", "开机启动中指向该软件的条目", risky: false,
            ScanValueEntries(cancellationToken, LeftoverKind.StartupEntry,
                [RunKeyPath, RunOnceKeyPath, StartupApprovedRunPath, StartupApprovedFolderPath],
                candidates, application.InstallLocation));
        AddGroup(groups, "服务", "映像路径指向该软件安装目录的 Windows 服务", risky: false,
            ScanServices(candidates, application.InstallLocation, cancellationToken));
        AddGroup(groups, "应用路径", @"App Paths 中登记的该软件可执行文件", risky: false,
            ScanAppPaths(candidates, application.InstallLocation, cancellationToken));

        // The groups below delete real files or MSI database rows, so they start unchecked.
        AddGroup(groups, "安装目录", "该软件记录的安装文件夹；勾选会删除整个目录", risky: true,
            ScanInstallFolder(application));
        AddGroup(groups, "用户数据目录", "AppData / ProgramData 下的同名文件夹；可能包含个人数据", risky: true,
            ScanAppDataFolders(candidates));
        AddGroup(groups, "快捷方式", "开始菜单和桌面上指向该软件的快捷方式", risky: true,
            ScanShortcuts(candidates, application.InstallLocation, cancellationToken));
        AddGroup(groups, "MSI 安装数据库", "Windows Installer 中登记的产品记录与安装源缓存", risky: true,
            ScanMsiRecords(application, cancellationToken));

        return groups;
    }

    /// <summary>
    /// Store packages have no classic uninstall key; what stays behind after removal is the AppModel
    /// state cache keyed by package family name.
    /// </summary>
    private static IReadOnlyList<LeftoverNode> ScanStoreApplication(InstalledApplication application)
    {
        var names = new List<string>();
        if (application.PackageFamilyName.Length > 0)
        {
            names.Add(application.PackageFamilyName);
        }

        if (application.PackageName.Length > 0 && !names.Contains(application.PackageName))
        {
            names.Add(application.PackageName);
        }

        var items = new List<LeftoverNode>();
        foreach (string root in new[] { $@"{AppModelKeyPath}\PolicyCache", $@"{AppModelKeyPath}\SystemAppData" })
        {
            foreach (string name in names)
            {
                string path = $@"{root}\{name}";
                if (OpenKey(RegistryHive.CurrentUser, RegistryView.Default, path) is not { } key)
                {
                    continue;
                }

                key.Dispose();
                items.Add(new LeftoverNode
                {
                    Title = $"{LeftoverNode.HivePrefix(RegistryHive.CurrentUser)}\\{path}",
                    Subtitle = "商店应用的策略/状态缓存",
                    Badge = "商店",
                    RegistryPath = path,
                    Hive = RegistryHive.CurrentUser,
                    View = RegistryView.Default,
                    Kind = LeftoverKind.StoreStateKey
                });
            }
        }

        var groups = new List<LeftoverNode>();
        AddGroup(groups, "商店应用注册表", "AppModel 下该包留下的策略与状态缓存", risky: false, items);
        return groups;
    }

    private static void AddGroup(
        ICollection<LeftoverNode> groups,
        string title,
        string subtitle,
        bool risky,
        IEnumerable<LeftoverNode> items)
    {
        var group = new LeftoverNode
        {
            Title = title,
            Subtitle = subtitle,
            IsGroup = true,
            IsRisky = risky,
            Badge = risky ? "谨慎" : string.Empty,
            Kind = LeftoverKind.ProductData
        };
        foreach (var item in items)
        {
            group.AddChild(item);
        }

        if (group.Children.Count == 0)
        {
            return;
        }

        if (risky)
        {
            group.IsChecked = false;
        }

        groups.Add(group);
    }

    private static IEnumerable<LeftoverNode> ScanUninstallEntries(
        InstalledApplication application,
        IReadOnlyCollection<string> candidates,
        CancellationToken cancellationToken)
    {
        var results = new List<LeftoverNode>();
        foreach (var (hive, view) in HiveViews())
        {
            foreach (string name in ReadSubKeyNames(hive, view, UninstallKeyPath))
            {
                cancellationToken.ThrowIfCancellationRequested();
                string path = $@"{UninstallKeyPath}\{name}";
                bool isOwnEntry = application.HasRegistryEntry &&
                                  application.RegistryHive == hive &&
                                  application.RegistryView == view &&
                                  string.Equals(application.RegistryPath, path, StringComparison.OrdinalIgnoreCase);
                if (isOwnEntry && !application.IsHiddenComponent)
                {
                    // The entry of an application that can still be uninstalled normally is not
                    // residue. A hidden component never has a vendor uninstaller, so its
                    // registration key (the identifier in the list) is exactly the leftover the
                    // forced uninstall must offer.
                    continue;
                }

                using var key = OpenKey(hive, view, path);
                if (key is null)
                {
                    continue;
                }

                string displayName = (key.GetValue("DisplayName") as string)?.Trim() ?? string.Empty;
                if (!MatchesAny(displayName, candidates) && !MatchesAny(name, candidates))
                {
                    continue;
                }

                results.Add(new LeftoverNode
                {
                    Title = displayName.Length > 0 ? displayName : name,
                    Subtitle = $"{LeftoverNode.HivePrefix(hive)}\\{path}",
                    Badge = "卸载项",
                    RegistryPath = path,
                    Hive = hive,
                    View = view,
                    Kind = LeftoverKind.UninstallEntry
                });
            }
        }

        return results;
    }

    /// <summary>
    /// Scans registry product keys: every <c>Software</c> child is walked recursively
    /// in HKCU and in both HKLM views. A key is added when its own leaf name equals one of the
    /// application name candidates; a match stops the descent into that key (the whole subtree is
    /// offered as one item), while the eight <see cref="RegistryScanSkipList"/> branches are
    /// skipped entirely. The uninstall branch is left to <see cref="ScanUninstallEntries"/> so the
    /// same key is not listed twice.
    /// </summary>
    private static List<LeftoverNode> ScanProductKeys(
        IReadOnlyCollection<string> candidates,
        CancellationToken cancellationToken)
    {
        var results = new List<LeftoverNode>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        int visited = 0;
        foreach (var (hive, view) in HiveViews())
        {
            WalkProductKeys(hive, view, "SOFTWARE", 0, results, seen, ref visited, candidates,
                cancellationToken);
        }

        return results;
    }

    private static void WalkProductKeys(
        RegistryHive hive,
        RegistryView view,
        string parentPath,
        int depth,
        ICollection<LeftoverNode> results,
        ISet<string> seen,
        ref int visited,
        IReadOnlyCollection<string> candidates,
        CancellationToken cancellationToken)
    {
        if (depth >= MaxRegistryDepth || visited >= MaxVisitedKeys)
        {
            return;
        }

        foreach (string name in ReadSubKeyNames(hive, view, parentPath))
        {
            cancellationToken.ThrowIfCancellationRequested();
            visited++;
            if (visited > MaxVisitedKeys)
            {
                return;
            }

            string path = $@"{parentPath}\{name}";
            if (IsPruned(path) || IsUninstallBranch(path))
            {
                continue;
            }

            if (MatchesAny(name, candidates))
            {
                // A match adds the whole key and stops descending into it.
                if (seen.Add($"{hive}|{view}|{path}"))
                {
                    results.Add(new LeftoverNode
                    {
                        Title = $"{LeftoverNode.HivePrefix(hive)}\\{path}",
                        Subtitle = "键名与该软件同名",
                        Badge = "数据键",
                        RegistryPath = path,
                        Hive = hive,
                        View = view,
                        Kind = LeftoverKind.ProductData
                    });
                }

                continue;
            }

            // CUninstallItem::GetAppRegPath only looks at top-level Software keys, but compares
            // them with spaces removed ("Telegram Desktop" matches "TelegramDesktop").
            if (depth == 0 && MatchesSpaceInsensitiveName(name, candidates))
            {
                if (seen.Add($"{hive}|{view}|{path}"))
                {
                    results.Add(new LeftoverNode
                    {
                        Title = $"{LeftoverNode.HivePrefix(hive)}\\{path}",
                        Subtitle = "键名与该软件同名（忽略空格）",
                        Badge = "数据键",
                        RegistryPath = path,
                        Hive = hive,
                        View = view,
                        Kind = LeftoverKind.ProductData
                    });
                }

                continue;
            }

            WalkProductKeys(hive, view, path, depth + 1, results, seen, ref visited, candidates,
                cancellationToken);
        }
    }

    private static bool IsUninstallBranch(string path)
        => string.Equals(path, UninstallKeyPath, StringComparison.OrdinalIgnoreCase) ||
           path.StartsWith(UninstallKeyPath + "\\", StringComparison.OrdinalIgnoreCase);

    private static List<LeftoverNode> ScanValueEntries(
        CancellationToken cancellationToken,
        LeftoverKind kind,
        IReadOnlyList<string> keyPaths,
        IReadOnlyCollection<string> candidates,
        string installLocation)
    {
        var results = new List<LeftoverNode>();
        string location = installLocation.Trim().TrimEnd('\\');
        foreach (var (hive, view) in HiveViews())
        {
            foreach (string keyPath in keyPaths)
            {
                using var key = OpenKey(hive, view, keyPath);
                if (key is null)
                {
                    continue;
                }

                foreach (string valueName in key.GetValueNames())
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    string data = (key.GetValue(valueName) as string)?.Trim() ?? string.Empty;
                    bool byName = MatchesAny(valueName, candidates);
                    bool byLocation = location.Length >= 4 &&
                                      data.Contains(location, StringComparison.OrdinalIgnoreCase);
                    if (!byName && !byLocation)
                    {
                        continue;
                    }

                    results.Add(new LeftoverNode
                    {
                        Title = valueName,
                        Subtitle = $"{HiveLabel(hive, view)} · {(data.Length > 0 ? data : keyPath)}",
                        Badge = kind == LeftoverKind.StartupEntry ? "启动项" : "路径项",
                        RegistryPath = keyPath,
                        ValueName = valueName,
                        Hive = hive,
                        View = view,
                        Kind = kind
                    });
                }
            }
        }

        return results;
    }

    private static List<LeftoverNode> ScanServices(
        IReadOnlyCollection<string> candidates,
        string installLocation,
        CancellationToken cancellationToken)
    {
        var results = new List<LeftoverNode>();
        string location = installLocation.Trim().TrimEnd('\\');
        foreach (string name in ReadSubKeyNames(RegistryHive.LocalMachine, RegistryView.Registry64, ServicesKeyPath))
        {
            cancellationToken.ThrowIfCancellationRequested();
            string path = $@"{ServicesKeyPath}\{name}";
            using var key = OpenKey(RegistryHive.LocalMachine, RegistryView.Registry64, path);
            if (key is null)
            {
                continue;
            }

            string imagePath = (key.GetValue("ImagePath") as string)?.Trim() ?? string.Empty;
            bool byName = MatchesAny(name, candidates);
            bool byLocation = location.Length >= 4 &&
                              imagePath.Contains(location, StringComparison.OrdinalIgnoreCase);
            if (!byName && !byLocation)
            {
                continue;
            }

            results.Add(new LeftoverNode
            {
                Title = name,
                Subtitle = $"本机 · {(imagePath.Length > 0 ? imagePath : "服务项")}",
                Badge = "服务",
                RegistryPath = path,
                Hive = RegistryHive.LocalMachine,
                View = RegistryView.Registry64,
                Kind = LeftoverKind.ServiceEntry
            });
        }

        return results;
    }

    private static List<LeftoverNode> ScanAppPaths(
        IReadOnlyCollection<string> candidates,
        string installLocation,
        CancellationToken cancellationToken)
    {
        var results = new List<LeftoverNode>();
        string location = installLocation.Trim().TrimEnd('\\');
        foreach (var (hive, view) in HiveViews())
        {
            foreach (string name in ReadSubKeyNames(hive, view, AppPathsKeyPath))
            {
                cancellationToken.ThrowIfCancellationRequested();
                string path = $@"{AppPathsKeyPath}\{name}";
                using var key = OpenKey(hive, view, path);
                if (key is null)
                {
                    continue;
                }

                string target = (key.GetValue(null) as string)?.Trim() ?? string.Empty;
                bool byName = MatchesAny(Path.GetFileNameWithoutExtension(name), candidates);
                bool byLocation = location.Length >= 4 &&
                                  target.Contains(location, StringComparison.OrdinalIgnoreCase);
                if (!byName && !byLocation)
                {
                    continue;
                }

                results.Add(new LeftoverNode
                {
                    Title = name,
                    Subtitle = $"{HiveLabel(hive, view)} · {(target.Length > 0 ? target : path)}",
                    Badge = "路径项",
                    RegistryPath = path,
                    Hive = hive,
                    View = view,
                    Kind = LeftoverKind.AppPathEntry
                });
            }
        }

        return results;
    }

    private static IEnumerable<LeftoverNode> ScanInstallFolder(InstalledApplication application)
    {
        string folder = application.InstallLocation.Trim().TrimEnd('\\', '/');
        if (!IsSafeFolder(folder) || !Directory.Exists(folder))
        {
            return [];
        }

        var budget = new ScanBudget();
        var node = BuildFolderNode(folder, folder, LeftoverKind.InstallFolder, risky: true, 0, budget);
        return node is null ? [] : [node];
    }

    private static List<LeftoverNode> ScanAppDataFolders(IReadOnlyCollection<string> candidates)
    {
        var results = new List<LeftoverNode>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        string[] roots =
        [
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                @"AppData\LocalLow")
        ];

        foreach (string root in roots)
        {
            if (root.Length == 0)
            {
                continue;
            }

            foreach (string candidate in candidates)
            {
                string folder = Path.Combine(root, candidate);
                if (!Directory.Exists(folder) || !seen.Add(folder))
                {
                    continue;
                }

                var node = BuildFolderNode(folder, folder, LeftoverKind.AppDataFolder, risky: true, 0,
                    new ScanBudget());
                if (node is not null)
                {
                    results.Add(node);
                }
            }
        }

        string programs = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs");
        if (Directory.Exists(programs))
        {
            foreach (string candidate in candidates)
            {
                string folder = Path.Combine(programs, candidate);
                if (!Directory.Exists(folder) || !seen.Add(folder))
                {
                    continue;
                }

                var node = BuildFolderNode(folder, folder, LeftoverKind.AppDataFolder, risky: true, 0,
                    new ScanBudget());
                if (node is not null)
                {
                    results.Add(node);
                }
            }
        }

        return results;
    }

    /// <summary>
    /// Builds a folder node with its whole subtree, listing every file of the install directory as
    /// its own checkable row.
    /// </summary>
    private static LeftoverNode? BuildFolderNode(
        string path,
        string root,
        LeftoverKind kind,
        bool risky,
        int depth,
        ScanBudget budget)
    {
        if (depth > MaxTreeDepth || budget.Visited >= MaxTreeEntries)
        {
            return null;
        }

        var children = new List<LeftoverNode>();
        try
        {
            foreach (string directory in Directory.EnumerateDirectories(path)
                         .OrderBy(item => item, StringComparer.OrdinalIgnoreCase))
            {
                if (budget.Visited >= MaxTreeEntries)
                {
                    break;
                }

                budget.Visited++;
                try
                {
                    if ((File.GetAttributes(directory) & FileAttributes.ReparsePoint) != 0)
                    {
                        // Junctions and symlinks are skipped so a loop cannot explode the tree.
                        continue;
                    }
                }
                catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
                {
                    continue;
                }

                var childFolder = BuildFolderNode(directory, root, LeftoverKind.FolderItem, risky: false,
                    depth + 1, budget);
                if (childFolder is not null)
                {
                    children.Add(childFolder);
                }
            }

            foreach (string file in Directory.EnumerateFiles(path)
                         .OrderBy(item => item, StringComparer.OrdinalIgnoreCase))
            {
                if (budget.Visited >= MaxTreeEntries)
                {
                    break;
                }

                budget.Visited++;
                long? size = TryGetFileSize(file);
                children.Add(new LeftoverNode
                {
                    Title = Path.GetFileName(file),
                    Subtitle = $"{FormatSize(size)} · {file}",
                    FilePath = file,
                    RootPath = root,
                    SizeBytes = size,
                    Kind = LeftoverKind.FileItem
                });
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or DirectoryNotFoundException or SecurityException)
        {
            // A locked or vanished folder must not abort the whole scan.
        }

        string title = Path.GetFileName(path.TrimEnd('\\', '/'));
        var node = new LeftoverNode
        {
            Title = title.Length > 0 ? title : path,
            Subtitle = children.Count == 0
                ? $"{path} · 空文件夹"
                : $"{path} · {children.Sum(child => child.FileCount())} 个文件（{FormatSize(children.Sum(child => child.TotalSizeBytes()))}）",
            Badge = risky ? "文件夹" : string.Empty,
            FilePath = path,
            RootPath = root,
            Kind = kind,
            IsRisky = risky
        };
        foreach (var child in children)
        {
            node.AddChild(child);
        }

        return node;
    }

    private static long? TryGetFileSize(string path)
    {
        try
        {
            return new FileInfo(path).Length;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or FileNotFoundException or SecurityException)
        {
            return null;
        }
    }

    internal static string FormatSize(long? bytes) => bytes switch
    {
        null => "大小未知",
        < 1024 => $"{bytes} B",
        < 1024 * 1024 => $"{bytes / 1024.0:0.#} KB",
        < 1024L * 1024 * 1024 => $"{bytes / (1024.0 * 1024):0.#} MB",
        _ => $"{bytes / (1024.0 * 1024 * 1024):0.##} GB"
    };

    internal static List<LeftoverNode> ScanShortcuts(
        IReadOnlyCollection<string> candidates,
        string installLocation,
        CancellationToken cancellationToken)
    {
        var results = new List<LeftoverNode>();
        string location = installLocation.Trim().TrimEnd('\\');

        // Collect every link in the shortcut roots, then resolve them in one STA pass.
        var shortcuts = new List<string>();
        foreach (string root in ShortcutRoots())
        {
            if (root.Length == 0 || !Directory.Exists(root))
            {
                continue;
            }

            CollectShortcuts(root, 0, shortcuts, cancellationToken);
        }

        var resolved = ShortcutResolver.ResolveMany(shortcuts);
        var matched = new List<string>();
        foreach (string shortcut in shortcuts)
        {
            cancellationToken.ThrowIfCancellationRequested();
            bool byName = MatchesAny(Path.GetFileNameWithoutExtension(shortcut), candidates);
            bool byTarget = false;
            if (location.Length >= 4 && resolved.TryGetValue(shortcut, out string? target))
            {
                byTarget = target.Length >= 4 && IsUnder(target, location);
            }
            if (byName || byTarget)
            {
                matched.Add(shortcut);
            }
        }

        foreach (var group in matched.GroupBy(
                     shortcut => Path.GetDirectoryName(shortcut) ?? string.Empty,
                     StringComparer.OrdinalIgnoreCase))
        {
            string folder = group.Key;
            if (folder.Length > 0 && !IsShortcutRoot(folder) && IsDedicatedShortcutFolder(folder, location, resolved))
            {
                var folderNode = BuildFolderNode(folder, folder, LeftoverKind.FolderItem, risky: true, 0,
                    new ScanBudget());
                if (folderNode is not null)
                {
                    results.Add(folderNode);
                    continue;
                }
            }

            foreach (string shortcut in group)
            {
                long? size = TryGetFileSize(shortcut);
                string rootForShortcut = ShortcutRoots()
                    .FirstOrDefault(root => IsUnder(shortcut, root)) ?? shortcut;
                results.Add(new LeftoverNode
                {
                    Title = Path.GetFileName(shortcut),
                    Subtitle = $"{FormatSize(size)} · {shortcut}",
                    Badge = "快捷方式",
                    FilePath = shortcut,
                    RootPath = rootForShortcut,
                    SizeBytes = size,
                    Kind = LeftoverKind.Shortcut,
                    IsRisky = true
                });
            }
        }

        return results;
    }

    private static void CollectShortcuts(
        string folder,
        int depth,
        ICollection<string> shortcuts,
        CancellationToken cancellationToken)
    {
        if (depth > MaxShortcutDepth)
        {
            return;
        }

        try
        {
            foreach (string file in Directory.EnumerateFiles(folder, "*.lnk"))
            {
                cancellationToken.ThrowIfCancellationRequested();
                shortcuts.Add(file);
            }

            foreach (string child in Directory.EnumerateDirectories(folder))
            {
                cancellationToken.ThrowIfCancellationRequested();
                CollectShortcuts(child, depth + 1, shortcuts, cancellationToken);
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or SecurityException or DirectoryNotFoundException)
        {
            // An unreadable folder is skipped.
        }
    }

    private static bool IsShortcutRoot(string folder)
        => ShortcutRoots().Any(root => string.Equals(root.TrimEnd('\\'), folder.TrimEnd('\\'),
            StringComparison.OrdinalIgnoreCase));

    /// <summary>
    /// A Start Menu program group is only offered as one item when every shortcut inside it points
    /// at this application; otherwise the group is not offered for deletion as a whole.
    /// </summary>
    private static bool IsDedicatedShortcutFolder(
        string folder,
        string installLocation,
        IReadOnlyDictionary<string, string> resolved)
    {
        int own = 0;
        int others = 0;
        try
        {
            foreach (string file in Directory.EnumerateFiles(folder, "*.lnk"))
            {
                if (!resolved.TryGetValue(file, out string? target))
                {
                    others++;
                    continue;
                }

                if (installLocation.Length >= 4 && IsUnder(target, installLocation))
                {
                    own++;
                }
                else if (!target.EndsWith("msiexec.exe", StringComparison.OrdinalIgnoreCase))
                {
                    others++;
                }
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or SecurityException or DirectoryNotFoundException)
        {
            return false;
        }

        return own == 1 && others == 0;
    }

    private static bool IsUnder(string path, string root)
    {
        string normalizedRoot = root.TrimEnd('\\', '/');
        return normalizedRoot.Length > 0 &&
               path.Length > normalizedRoot.Length &&
               path.StartsWith(normalizedRoot + "\\", StringComparison.OrdinalIgnoreCase);
    }

    private sealed class ScanBudget
    {
        public int Visited;
    }

    private static List<LeftoverNode> ScanMsiRecords(
        InstalledApplication application,
        CancellationToken cancellationToken)
    {
        var results = new List<LeftoverNode>();
        int index = application.RegistryPath.LastIndexOf('\\');
        string keyName = index >= 0 ? application.RegistryPath[(index + 1)..] : application.RegistryPath;
        if (!TryGetProductCode(keyName, out Guid productCode))
        {
            return results;
        }

        string compressed = CompressGuid(productCode);
        string raw = productCode.ToString("B");
        string location = application.InstallLocation.Trim().TrimEnd('\\');
        string userDataRoot = InstallerKeyPath + @"\UserData";

        // One product record per user SID, stored under the compressed product code.
        foreach (string userData in ReadSubKeyNames(RegistryHive.LocalMachine, RegistryView.Registry64,
                     userDataRoot))
        {
            cancellationToken.ThrowIfCancellationRequested();
            foreach (string code in new[] { compressed, raw, productCode.ToString("N") })
            {
                string path = $@"{userDataRoot}\{userData}\Products\{code}";
                if (OpenKey(RegistryHive.LocalMachine, RegistryView.Registry64, path) is not { } key)
                {
                    continue;
                }

                key.Dispose();
                results.Add(new LeftoverNode
                {
                    Title = path,
                    Subtitle = $"Windows Installer 产品记录（{userData}）",
                    Badge = "MSI",
                    RegistryPath = path,
                    Hive = RegistryHive.LocalMachine,
                    View = RegistryView.Registry64,
                    Kind = LeftoverKind.MsiProductKey,
                    IsRisky = true
                });
            }
        }

        foreach (string code in new[] { compressed, raw, productCode.ToString("N") })
        {
            string path = $@"{InstallerKeyPath}\Products\{code}";
            if (OpenKey(RegistryHive.LocalMachine, RegistryView.Registry64, path) is not { } key)
            {
                continue;
            }

            key.Dispose();
            results.Add(new LeftoverNode
            {
                Title = path,
                Subtitle = "Windows Installer 已安装产品记录",
                Badge = "MSI",
                RegistryPath = path,
                Hive = RegistryHive.LocalMachine,
                View = RegistryView.Registry64,
                Kind = LeftoverKind.MsiProductKey,
                IsRisky = true
            });
        }

        // Also walks UserData\<SID>\Components. Each component key
        // stores the installed paths as values, so the key is only a leftover when one of those
        // values still resolves into this application's install folder.
        int visited = 0;
        foreach (string userData in ReadSubKeyNames(RegistryHive.LocalMachine, RegistryView.Registry64,
                     userDataRoot))
        {
            string componentsRoot = $@"{userDataRoot}\{userData}\Components";
            foreach (string component in ReadSubKeyNames(RegistryHive.LocalMachine,
                         RegistryView.Registry64, componentsRoot))
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (++visited > MaxInstallerComponents)
                {
                    break;
                }

                string path = $@"{componentsRoot}\{component}";
                if (location.Length < 4 ||
                    !RegistryValuesReferenceFolder(RegistryHive.LocalMachine, RegistryView.Registry64,
                        path, location))
                {
                    continue;
                }

                results.Add(new LeftoverNode
                {
                    Title = $"Windows Installer Component: {path}",
                    Subtitle = "Windows Installer 组件记录；组件值仍指向该软件的安装目录",
                    Badge = "MSI",
                    RegistryPath = path,
                    Hive = RegistryHive.LocalMachine,
                    View = RegistryView.Registry64,
                    Kind = LeftoverKind.MsiComponentKey,
                    IsRisky = true
                });
            }
        }

        string foldersPath = InstallerKeyPath + @"\Folders";
        if (OpenKey(RegistryHive.LocalMachine, RegistryView.Registry64, foldersPath) is { } foldersKey)
        {
            using (foldersKey)
            {
                if (location.Length >= 4)
                {
                    foreach (string valueName in foldersKey.GetValueNames())
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        string stored = valueName.TrimEnd('\\');
                        if (!string.Equals(stored, location, StringComparison.OrdinalIgnoreCase) &&
                            !stored.StartsWith(location + "\\", StringComparison.OrdinalIgnoreCase))
                        {
                            continue;
                        }

                        results.Add(new LeftoverNode
                        {
                            Title = valueName,
                            Subtitle = "Windows Installer 安装源目录缓存",
                            Badge = "MSI",
                            RegistryPath = foldersPath,
                            ValueName = valueName,
                            Hive = RegistryHive.LocalMachine,
                            View = RegistryView.Registry64,
                            Kind = LeftoverKind.MsiInstallerFolder,
                            IsRisky = true
                        });
                    }
                }
            }
        }

        return results;
    }

    /// <summary>
    /// True when any string value of the key still contains the install folder. MSI component
    /// values keep absolute paths, sometimes without the drive (<c>\Program Files\App\file</c>),
    /// so both forms are accepted.
    /// </summary>
    private static bool RegistryValuesReferenceFolder(
        RegistryHive hive,
        RegistryView view,
        string path,
        string folder)
    {
        using var key = OpenKey(hive, view, path);
        if (key is null)
        {
            return false;
        }

        foreach (string valueName in key.GetValueNames())
        {
            if (key.GetValue(valueName) is string text && StoredPathReferencesFolder(text, folder))
            {
                return true;
            }
        }

        return false;
    }

    private static bool StoredPathReferencesFolder(string? value, string folder)
    {
        string location = folder.Trim().TrimEnd('\\');
        string text = (value ?? string.Empty).Trim().Trim('"');
        if (text.Length < 3 || location.Length < 4)
        {
            return false;
        }

        text = Environment.ExpandEnvironmentVariables(text).Replace('/', '\\');
        if (text.Contains(location, StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        string withoutDrive = location.Length > 2 && location[1] == ':'
            ? location[2..].TrimStart('\\')
            : location.TrimStart('\\');
        return withoutDrive.Length >= 3 &&
               text.TrimStart('\\').StartsWith(withoutDrive, StringComparison.OrdinalIgnoreCase);
    }

    /// <summary>
    /// Reads the MSI product code from an uninstall key name. A normal MSI entry is a plain GUID;
    /// a hidden patch entry looks like <c>{1D8E6291-…}.KB2533623</c>, so the embedded product GUID
    /// is used as the identifier for the Installer database lookup.
    /// </summary>
    private static bool TryGetProductCode(string keyName, out Guid productCode)
    {
        if (Guid.TryParse(keyName, out productCode))
        {
            return true;
        }

        Match match = GuidPattern.Match(keyName);
        if (match.Success && Guid.TryParse(match.Value, out productCode))
        {
            return true;
        }

        productCode = Guid.Empty;
        return false;
    }

    /// <summary>
    /// Windows Installer stores product codes with the first three GUID groups byte-reversed and
    /// without dashes, for example <c>11223344-5566-7788-…</c> becomes <c>4433221166558877…</c>.
    /// </summary>
    internal static string CompressGuid(Guid value)
    {
        string hex = value.ToString("N").ToUpperInvariant();
        string head = string.Concat(
            hex[6], hex[7], hex[4], hex[5], hex[2], hex[3], hex[0], hex[1],
            hex[10], hex[11], hex[8], hex[9],
            hex[14], hex[15], hex[12], hex[13]);
        return head + hex[16..];
    }

    /// <summary>
    /// Shortcut roots: both Start Menu roots (which contain the
    /// Programs groups), both Desktop roots and the legacy Quick Launch folder.
    /// </summary>
    internal static IReadOnlyList<string> ShortcutRoots()
    {
        var candidates = new List<string>();
        AddRoot(Environment.SpecialFolder.StartMenu);
        AddRoot(Environment.SpecialFolder.CommonStartMenu);
        AddRoot(Environment.SpecialFolder.Programs);
        AddRoot(Environment.SpecialFolder.CommonPrograms);
        AddRoot(Environment.SpecialFolder.Desktop);
        AddRoot(Environment.SpecialFolder.CommonDesktopDirectory);
        AddPath(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            @"Microsoft\Internet Explorer\Quick Launch"));

        // StartMenu already contains Programs; keep only the outermost roots so one shortcut is
        // not collected twice.
        return candidates
            .Where(path => path.Length > 0)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .Where(path => !candidates.Any(other =>
                other.Length > 0 && !string.Equals(other, path, StringComparison.OrdinalIgnoreCase) &&
                IsUnder(path, other)))
            .ToArray();

        void AddRoot(Environment.SpecialFolder folder)
        {
            string path = Environment.GetFolderPath(folder);
            if (path.Length > 0)
            {
                candidates.Add(path);
            }
        }

        void AddPath(string path)
        {
            if (path.Length > 0)
            {
                candidates.Add(path);
            }
        }
    }

    internal static bool IsSafeFolder(string folder)
    {
        if (folder.Length < 4 || folder.Length != folder.Trim().Length)
        {
            return false;
        }

        try
        {
            string full = Path.GetFullPath(folder);
            if (!Path.IsPathFullyQualified(full) || Path.GetPathRoot(full)?.Length == full.Length)
            {
                return false;
            }

            string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows);
            return windows.Length == 0 ||
                   !full.StartsWith(windows + "\\", StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException
                                              or PathTooLongException or SecurityException)
        {
            return false;
        }
    }

    private static string HiveLabel(RegistryHive hive, RegistryView view) => (hive, view) switch
    {
        (RegistryHive.CurrentUser, _) => "当前用户",
        (_, RegistryView.Registry32) => "本机 32 位",
        _ => "本机"
    };

    internal static IReadOnlyList<(RegistryHive Hive, RegistryView View)> HiveViews()
    {
        var views = new List<(RegistryHive, RegistryView)>
        {
            (RegistryHive.CurrentUser, RegistryView.Default),
            (RegistryHive.LocalMachine, RegistryView.Registry64)
        };
        if (Environment.Is64BitOperatingSystem)
        {
            views.Add((RegistryHive.LocalMachine, RegistryView.Registry32));
        }

        return views;
    }

    internal static bool IsPruned(string path)
    {
        foreach (string pruned in RegistryScanSkipList)
        {
            if (string.Equals(path, pruned, StringComparison.OrdinalIgnoreCase) ||
                path.StartsWith(pruned + "\\", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
    }

    internal static IEnumerable<string> ReadSubKeyNames(
        RegistryHive hive,
        RegistryView view,
        string path)
    {
        string[] names;
        try
        {
            using var key = RegistryKey.OpenBaseKey(hive, view).OpenSubKey(path);
            names = key?.GetSubKeyNames() ?? [];
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException
            or IOException or PlatformNotSupportedException)
        {
            return [];
        }

        return names;
    }

    internal static RegistryKey? OpenKey(RegistryHive hive, RegistryView view, string path)
    {
        try
        {
            return RegistryKey.OpenBaseKey(hive, view).OpenSubKey(path);
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException
            or IOException or PlatformNotSupportedException)
        {
            return null;
        }
    }

    private static IReadOnlyList<string> BuildNameCandidates(InstalledApplication application)
    {
        var candidates = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        void Add(string? value) => AddNameCandidate(candidates, value);

        Add(application.Name);
        Add(VersionSuffixPattern.Replace(application.Name, string.Empty));
        Add(Path.GetFileName(application.InstallLocation.TrimEnd('\\', '/')));
        if (application.RegistryPath.Length > 0)
        {
            int index = application.RegistryPath.LastIndexOf('\\');
            string keyName = index >= 0 ? application.RegistryPath[(index + 1)..] : application.RegistryPath;
            if (!Guid.TryParse(keyName, out _) || application.IsHiddenComponent)
            {
                Add(keyName);
            }
        }

        if (application.IsHiddenComponent)
        {
            // A hidden component has no DisplayName, so its uninstall key name is the identifier:
            // "{1D8E6291-...}.KB2533623", "Steam App 228980", "{12228a0d-...}", "mspaint-<guid>".
            // The KB number and the embedded GUID are the stable parts the scanner can match
            // against other registry keys and Windows Installer product records.
            foreach (Match match in KnowledgeBasePattern.Matches(application.Name))
            {
                Add(match.Value);
            }

            foreach (Match match in GuidPattern.Matches(application.Name))
            {
                Add(match.Value);
                Add("{" + match.Value + "}");
            }
        }

        return candidates.ToArray();
    }

    /// <summary>
    /// Candidate set for the uninstall-entry group of a hidden component: only the full identifier
    /// (its key name). The split GUID/KB candidates are intentionally excluded so the parent
    /// product's own uninstall entry is not offered for deletion.
    /// </summary>
    private static IReadOnlyList<string> BuildHiddenKeyCandidates(InstalledApplication application)
    {
        var candidates = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        AddNameCandidate(candidates, application.Name);
        AddNameCandidate(candidates, VersionSuffixPattern.Replace(application.Name, string.Empty));
        if (application.RegistryPath.Length > 0)
        {
            int index = application.RegistryPath.LastIndexOf('\\');
            AddNameCandidate(candidates,
                index >= 0 ? application.RegistryPath[(index + 1)..] : application.RegistryPath);
        }

        return candidates.ToArray();
    }

    private static void AddNameCandidate(ISet<string> candidates, string? value)
    {
        string text = WhitespacePattern.Replace((value ?? string.Empty).Trim().Trim('"'), " ").Trim();
        if (text.Length >= 3 && !GenericNames.Contains(text))
        {
            candidates.Add(text);
        }
    }

    private static bool MatchesAny(string value, IReadOnlyCollection<string> candidates)
    {
        string text = WhitespacePattern.Replace(value.Trim(), " ").Trim();
        if (text.Length < 3)
        {
            return false;
        }

        foreach (string candidate in candidates)
        {
            if (string.Equals(text, candidate, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
    }

    /// <summary>
    /// The space-insensitive comparison <c>CUninstallItem::GetAppRegPath</c> uses for the
    /// top-level <c>Software</c> key, e.g. <c>Telegram Desktop</c> vs <c>TelegramDesktop</c>.
    /// Only an exact (space-stripped) match is accepted; the target's "either contains the other"
    /// rule would also match vendor containers such as <c>Software\Google</c> for Google Chrome,
    /// which is too destructive for a default-checked group.
    /// </summary>
    private static bool MatchesSpaceInsensitiveName(
        string value,
        IReadOnlyCollection<string> candidates)
    {
        string normalized = WhitespacePattern.Replace(value.Trim(), string.Empty);
        if (normalized.Length <= 4)
        {
            return false;
        }

        foreach (string candidate in candidates)
        {
            if (string.Equals(
                    normalized,
                    WhitespacePattern.Replace(candidate, string.Empty),
                    StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
    }

    private static readonly Regex WhitespacePattern = new(@"\s+");

    private static readonly Regex VersionSuffixPattern = new(
        @"\s*[\(\[-]?\s*(?:version|ver\.?|v)\s*\d+(?:\.\d+)*\s*[\)\]]?\s*$",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    private static readonly Regex KnowledgeBasePattern = new(
        @"KB\d{5,8}",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    private static readonly Regex GuidPattern = new(
        @"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        RegexOptions.CultureInvariant);
}
