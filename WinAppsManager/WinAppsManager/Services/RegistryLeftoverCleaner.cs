using System.IO;
using System.Security;
using Microsoft.Win32;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Deletes the registry nodes the user selected in the deep-clean tree. Every path is
/// re-validated here so a manipulated or stale node can never remove a system key.
/// </summary>
public static class RegistryLeftoverCleaner
{
    private const string UninstallKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall";
    private const string AppPathsKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths";
    private const string ServicesKeyPath = @"SYSTEM\CurrentControlSet\Services";
    private const string RunKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Run";
    private const string RunOnceKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\RunOnce";
    private const string InstallerKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Installer";
    private const string StartupApprovedRunPath =
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run";
    private const string StartupApprovedFolderPath =
        @"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder";
    private const string AppModelKeyPath =
        @"SOFTWARE\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppModel";

    public static IReadOnlyList<LeftoverCleanupResult> Delete(
        IEnumerable<LeftoverNode> nodes,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(nodes);
        var results = new List<LeftoverCleanupResult>();
        var deletedPaths = new List<string>();

        // Parents first: deleting a parent key also removes the children below it.
        foreach (var node in nodes
                     .Where(node => node is not null)
                     .Distinct()
                     .OrderBy(node => node.RegistryPath.Count(character => character == '\\'))
                     .ThenBy(node => node.RegistryPath, StringComparer.OrdinalIgnoreCase))
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (IsCoveredByDeletedParent(node, deletedPaths))
            {
                results.Add(new LeftoverCleanupResult(node, true, "已随上级键一起删除。"));
                continue;
            }

            if (!IsDeletable(node, out string reason))
            {
                results.Add(new LeftoverCleanupResult(node, false, reason));
                continue;
            }

            results.Add(DeleteSingle(node, deletedPaths));
        }

        return results;
    }

    /// <summary>Validates that a node points at an app-owned registry key or value.</summary>
    public static bool IsDeletable(LeftoverNode? node, out string reason)
    {
        reason = string.Empty;
        if (node is null)
        {
            reason = "节点为空。";
            return false;
        }

        if (node.IsGroup || node.RegistryPath.Length == 0)
        {
            reason = "分类节点不能删除。";
            return false;
        }

        string path = node.RegistryPath.Trim('\\');
        string[] parts = path.Split('\\', StringSplitOptions.RemoveEmptyEntries);
        switch (node.Kind)
        {
            case LeftoverKind.UninstallEntry:
                return RequireLeaf(path, UninstallKeyPath, 1, out reason);
            case LeftoverKind.AppPathEntry:
                return RequireLeaf(path, AppPathsKeyPath, 1, out reason);
            case LeftoverKind.ServiceEntry:
                if (node.Hive != RegistryHive.LocalMachine)
                {
                    reason = "服务项只允许在 HKLM 中删除。";
                    return false;
                }

                return RequireLeaf(path, ServicesKeyPath, 1, out reason);
            case LeftoverKind.StartupEntry:
                if (node.ValueName is null or { Length: 0 })
                {
                    reason = "启动项只能删除对应的值，不能删除整个键。";
                    return false;
                }

                if (!string.Equals(path, RunKeyPath, StringComparison.OrdinalIgnoreCase) &&
                    !string.Equals(path, RunOnceKeyPath, StringComparison.OrdinalIgnoreCase) &&
                    !string.Equals(path, StartupApprovedRunPath, StringComparison.OrdinalIgnoreCase) &&
                    !string.Equals(path, StartupApprovedFolderPath, StringComparison.OrdinalIgnoreCase))
                {
                    reason = "启动项路径不在允许范围内。";
                    return false;
                }

                return true;
            case LeftoverKind.StoreStateKey:
                return IsStoreStateKey(node, path, out reason);
            case LeftoverKind.MsiProductKey:
                return IsInstallerProductKey(node, path, out reason);
            case LeftoverKind.MsiComponentKey:
                return IsInstallerComponentKey(node, path, out reason);
            case LeftoverKind.MsiInstallerFolder:
                if (node.ValueName is null or { Length: 0 } ||
                    !string.Equals(path, InstallerKeyPath + @"\Folders", StringComparison.OrdinalIgnoreCase))
                {
                    reason = "只能删除 Installer\\Folders 下的单个路径值。";
                    return false;
                }

                return true;
            case LeftoverKind.InstallFolder:
            case LeftoverKind.AppDataFolder:
            case LeftoverKind.Shortcut:
                reason = "文件系统项必须由文件清理器处理。";
                return false;
            default:
                // Deleting a value cannot damage a subtree, so only key deletions are pruned.
                if (node.ValueName is null && LeftoverScanner.IsPruned(path))
                {
                    reason = "该路径属于受保护的系统注册表分支，已跳过。";
                    return false;
                }

                if (parts.Length < 2 || !string.Equals(parts[0], "SOFTWARE", StringComparison.OrdinalIgnoreCase))
                {
                    reason = "只能删除 SOFTWARE 下的软件数据键。";
                    return false;
                }

                return true;
        }
    }

    /// <summary>
    /// Store packages only leave <c>AppModel\PolicyCache</c> / <c>AppModel\SystemAppData</c> keys,
    /// always in the current user hive and always one level below those two branches.
    /// </summary>
    private static bool IsStoreStateKey(LeftoverNode node, string path, out string reason)
    {
        reason = string.Empty;
        if (node.Hive != RegistryHive.CurrentUser)
        {
            reason = "商店应用的状态键只允许在当前用户中删除。";
            return false;
        }

        string prefix = AppModelKeyPath + @"\";
        if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            reason = "商店应用状态键路径不在允许范围内。";
            return false;
        }

        string[] rest = path[prefix.Length..].Split('\\', StringSplitOptions.RemoveEmptyEntries);
        if (rest.Length != 2)
        {
            reason = "只能删除 AppModel 下具体某个包的状态键。";
            return false;
        }

        bool allowedBranch = string.Equals(rest[0], "PolicyCache", StringComparison.OrdinalIgnoreCase) ||
                             string.Equals(rest[0], "SystemAppData", StringComparison.OrdinalIgnoreCase);
        if (!allowedBranch)
        {
            reason = "商店应用状态键只允许在 PolicyCache 或 SystemAppData 下删除。";
            return false;
        }

        return true;
    }

    /// <summary>
    /// <c>Installer\UserData\&lt;SID&gt;\Components\&lt;component&gt;</c> keys; only one component may be
    /// deleted at a time.
    /// </summary>
    private static bool IsInstallerComponentKey(LeftoverNode node, string path, out string reason)
    {
        reason = string.Empty;
        if (node.Hive != RegistryHive.LocalMachine)
        {
            reason = "MSI 组件记录只允许在 HKLM 中删除。";
            return false;
        }

        string prefix = InstallerKeyPath + @"\";
        if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            reason = "路径不在 Windows Installer 分支内。";
            return false;
        }

        string[] remainder = path[prefix.Length..].Split('\\', StringSplitOptions.RemoveEmptyEntries);
        bool userDataComponent = remainder.Length == 4 &&
                                 string.Equals(remainder[0], "UserData", StringComparison.OrdinalIgnoreCase) &&
                                 string.Equals(remainder[2], "Components", StringComparison.OrdinalIgnoreCase);
        if (!userDataComponent)
        {
            reason = "只允许删除 Installer\\UserData\\<SID>\\Components 下的单个组件键。";
            return false;
        }

        return true;
    }

    private static bool IsInstallerProductKey(LeftoverNode node, string path, out string reason)
    {
        reason = string.Empty;
        if (node.Hive != RegistryHive.LocalMachine)
        {
            reason = "MSI 记录只允许在 HKLM 中删除。";
            return false;
        }

        string prefix = InstallerKeyPath + @"\";
        if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            reason = "路径不在 Windows Installer 分支内。";
            return false;
        }

        string[] remainder = path[prefix.Length..].Split('\\', StringSplitOptions.RemoveEmptyEntries);
        bool userDataProduct = remainder.Length == 4 &&
                               string.Equals(remainder[0], "UserData", StringComparison.OrdinalIgnoreCase) &&
                               string.Equals(remainder[2], "Products", StringComparison.OrdinalIgnoreCase);
        bool machineProduct = remainder.Length == 2 &&
                              string.Equals(remainder[0], "Products", StringComparison.OrdinalIgnoreCase);
        if (!userDataProduct && !machineProduct)
        {
            reason = "只允许删除 Installer\\Products 或 UserData\\<SID>\\Products 下的单个产品键。";
            return false;
        }

        return true;
    }

    private static bool RequireLeaf(string path, string parent, int extraSegments, out string reason)
    {
        reason = string.Empty;
        string prefix = parent + @"\";
        if (!path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
        {
            reason = "路径不在允许的注册表分支内。";
            return false;
        }

        string[] remainder = path[prefix.Length..].Split('\\', StringSplitOptions.RemoveEmptyEntries);
        if (remainder.Length != extraSegments)
        {
            reason = "只允许删除该分支下的单个子键。";
            return false;
        }

        return true;
    }

    private static bool IsCoveredByDeletedParent(LeftoverNode node, List<string> deletedPaths)
        => deletedPaths.Any(path =>
            node.RegistryPath.StartsWith(path + "\\", StringComparison.OrdinalIgnoreCase));

    private static LeftoverCleanupResult DeleteSingle(
        LeftoverNode node,
        ICollection<string> deletedPaths)
    {
        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(node.Hive, node.View);
            if (node.ValueName is { Length: > 0 } valueName)
            {
                using var valueKey = baseKey.OpenSubKey(node.RegistryPath, writable: true);
                if (valueKey is null)
                {
                    return new LeftoverCleanupResult(node, true, "注册表键已不存在。");
                }

                valueKey.DeleteValue(valueName, throwOnMissingValue: false);
                return new LeftoverCleanupResult(node, true, "已删除该注册表值。");
            }

            int index = node.RegistryPath.LastIndexOf('\\');
            if (index <= 0)
            {
                return new LeftoverCleanupResult(node, false, "注册表路径不完整，已跳过。");
            }

            string parentPath = node.RegistryPath[..index];
            string leafName = node.RegistryPath[(index + 1)..];
            using var parentKey = baseKey.OpenSubKey(parentPath, writable: true);
            if (parentKey is null)
            {
                return new LeftoverCleanupResult(node, true, "上级注册表键已不存在。");
            }

            parentKey.DeleteSubKeyTree(leafName, throwOnMissingSubKey: false);
            deletedPaths.Add(node.RegistryPath);
            return new LeftoverCleanupResult(node, true, "已删除该注册表键。");
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException)
        {
            return new LeftoverCleanupResult(
                node,
                false,
                "拒绝访问，需要以管理员身份运行本应用后才能删除该项；也可以重启 Windows 后重试。");
        }
        catch (Exception exception) when (exception is IOException or InvalidOperationException
                                              or ArgumentException)
        {
            return new LeftoverCleanupResult(
                node,
                false,
                "删除失败，注册表键可能正在被占用；请重启 Windows 后重试。");
        }
    }
}
