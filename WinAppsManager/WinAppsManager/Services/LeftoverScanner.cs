using System.IO;
using System.Security;
using System.Text.RegularExpressions;
using Microsoft.Win32;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Heuristic leftover scanner. Registry items are
/// matched by "key name equals the program name" or "a value still points at the install
/// directory"; file items are matched by install/AppData folder name and by shortcut file name
/// inside the standard Start Menu and Desktop folders; MSI items come from the Windows Installer
/// database. File and MSI groups are marked risky and start unchecked.
/// </summary>
public static partial class LeftoverScanner
{
    private const int MaxProductDepth = 4;
    private const int MaxVisitedKeys = 20000;
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

    /// <summary>Subtrees that are never scanned and never deleted by key.</summary>
    private static readonly string[] PrunedSubtrees =
    [
        @"SOFTWARE\Microsoft",
        @"SOFTWARE\Classes",
        @"SOFTWARE\Policies",
        @"SOFTWARE\Wow6432Node",
        @"SOFTWARE\RegisteredApplications",
        @"SOFTWARE\Clients",
        @"SOFTWARE\Crypto",
        @"SOFTWARE\ODBC"
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
        var groups = new List<LeftoverNode>();

        AddGroup(groups, "卸载信息", "注册表中残留的卸载登记项", risky: false,
            ScanUninstallEntries(application, candidates, cancellationToken));
        AddGroup(groups, "软件数据", @"HKCU / HKLM\Software 下与该软件同名的注册表键", risky: false,
            ScanProductKeys(candidates, application.InstallLocation, cancellationToken));
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
                if (application.HasRegistryEntry &&
                    application.RegistryHive == hive &&
                    application.RegistryView == view &&
                    string.Equals(application.RegistryPath, path, StringComparison.OrdinalIgnoreCase))
                {
                    // The entry of the app that is still installed is not residue.
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

    private static List<LeftoverNode> ScanProductKeys(
        IReadOnlyCollection<string> candidates,
        string installLocation,
        CancellationToken cancellationToken)
    {
        var results = new List<LeftoverNode>();
        int visited = 0;
        foreach (var (hive, view) in HiveViews())
        {
            WalkProductKeys(hive, view, "SOFTWARE", 0, results, ref visited, candidates, installLocation,
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
        ref int visited,
        IReadOnlyCollection<string> candidates,
        string installLocation,
        CancellationToken cancellationToken)
    {
        if (depth >= MaxProductDepth || visited >= MaxVisitedKeys)
        {
            return;
        }

        foreach (string name in ReadSubKeyNames(hive, view, parentPath))
        {
            cancellationToken.ThrowIfCancellationRequested();
            visited++;
            if (visited >= MaxVisitedKeys)
            {
                return;
            }

            string path = $@"{parentPath}\{name}";
            if (IsPruned(path))
            {
                continue;
            }

            bool byName = MatchesAny(name, candidates);

            // A key whose own values still point at the install directory belongs to the app
            // even when the vendor named it differently from the display name.
            bool byValue = !byName &&
                           depth <= 1 &&
                           installLocation.Length >= 4 &&
                           ValueReferencesLocation(hive, view, path, installLocation);

            if (byName || byValue)
            {
                results.Add(new LeftoverNode
                {
                    Title = $"{LeftoverNode.HivePrefix(hive)}\\{path}",
                    Subtitle = byName ? "整个注册表键" : "键值仍指向该软件的安装目录",
                    Badge = "数据键",
                    RegistryPath = path,
                    Hive = hive,
                    View = view,
                    Kind = LeftoverKind.ProductData
                });
                continue;
            }

            WalkProductKeys(hive, view, path, depth + 1, results, ref visited, candidates, installLocation,
                cancellationToken);
        }
    }

    private static bool ValueReferencesLocation(
        RegistryHive hive,
        RegistryView view,
        string path,
        string installLocation)
    {
        using var key = OpenKey(hive, view, path);
        if (key is null)
        {
            return false;
        }

        foreach (string valueName in key.GetValueNames())
        {
            if (key.GetValue(valueName) is string text &&
                text.Contains(installLocation, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
    }

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
        if (!Guid.TryParse(keyName, out Guid productCode))
        {
            return results;
        }

        string compressed = CompressGuid(productCode);
        string raw = productCode.ToString("B");
        foreach (string userData in ReadSubKeyNames(RegistryHive.LocalMachine, RegistryView.Registry64,
                     InstallerKeyPath + @"\UserData"))
        {
            cancellationToken.ThrowIfCancellationRequested();
            foreach (string code in new[] { compressed, raw, productCode.ToString("N") })
            {
                string path = $@"{InstallerKeyPath}\UserData\{userData}\Products\{code}";
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

        string foldersPath = InstallerKeyPath + @"\Folders";
        if (OpenKey(RegistryHive.LocalMachine, RegistryView.Registry64, foldersPath) is { } foldersKey)
        {
            using (foldersKey)
            {
                string location = application.InstallLocation.Trim().TrimEnd('\\');
                if (location.Length >= 4)
                {
                    foreach (string valueName in foldersKey.GetValueNames())
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        if (!string.Equals(valueName.TrimEnd('\\'), location, StringComparison.OrdinalIgnoreCase))
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

    internal static IReadOnlyList<string> ShortcutRoots()
    {
        var roots = new List<string>();
        AddRoot(Environment.SpecialFolder.Programs);
        AddRoot(Environment.SpecialFolder.CommonPrograms);
        AddRoot(Environment.SpecialFolder.Desktop);
        AddRoot(Environment.SpecialFolder.CommonDesktopDirectory);
        return roots;

        void AddRoot(Environment.SpecialFolder folder)
        {
            string path = Environment.GetFolderPath(folder);
            if (path.Length > 0)
            {
                roots.Add(path);
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
        foreach (string pruned in PrunedSubtrees)
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

        void Add(string? value)
        {
            string text = WhitespacePattern.Replace((value ?? string.Empty).Trim().Trim('"'), " ").Trim();
            if (text.Length >= 3 && !GenericNames.Contains(text))
            {
                candidates.Add(text);
            }
        }

        Add(application.Name);
        Add(VersionSuffixPattern.Replace(application.Name, string.Empty));
        Add(Path.GetFileName(application.InstallLocation.TrimEnd('\\', '/')));
        if (application.RegistryPath.Length > 0)
        {
            int index = application.RegistryPath.LastIndexOf('\\');
            string keyName = index >= 0 ? application.RegistryPath[(index + 1)..] : application.RegistryPath;
            if (!Guid.TryParse(keyName, out _))
            {
                Add(keyName);
            }
        }

        return candidates.ToArray();
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

    private static readonly Regex WhitespacePattern = new(@"\s+");

    private static readonly Regex VersionSuffixPattern = new(
        @"\s*[\(\[-]?\s*(?:version|ver\.?|v)\s*\d+(?:\.\d+)*\s*[\)\]]?\s*$",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);
}
