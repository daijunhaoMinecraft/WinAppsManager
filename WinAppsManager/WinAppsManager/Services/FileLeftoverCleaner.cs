using System.IO;
using System.Runtime.InteropServices;
using System.Security;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Deletes the file-system leftovers the user explicitly selected: files or empty folders below
/// a scanned root (install folder / AppData folder) and shortcut files inside the standard Start
/// Menu and Desktop folders. Every path is validated against its scan root, so a bad or tampered
/// node can never wipe a drive root, the Windows directory or a personal folder.
/// </summary>
public static class FileLeftoverCleaner
{
    private const uint MoveFileDelayUntilReboot = 0x00000004;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool MoveFileEx(string existingFileName, string? newFileName, uint flags);

    public static IReadOnlyList<LeftoverCleanupResult> Delete(
        IEnumerable<LeftoverNode> nodes,
        CancellationToken cancellationToken = default)
    {
        ArgumentNullException.ThrowIfNull(nodes);
        var results = new List<LeftoverCleanupResult>();

        // Files first, then the deepest folders, so a folder only disappears when it is empty.
        var ordered = nodes
            .Where(node => node is not null)
            .Distinct()
            .OrderBy(node => node.Kind == LeftoverKind.FolderItem ||
                             node.Kind == LeftoverKind.InstallFolder ||
                             node.Kind == LeftoverKind.AppDataFolder
                ? 1
                : 0)
            .ThenByDescending(node => node.FilePath.Count(character => character == '\\'));

        foreach (var node in ordered)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (node.CleanupDisposition == LeftoverCleanupDisposition.ScheduledForReboot)
            {
                results.Add(new LeftoverCleanupResult(
                    node,
                    LeftoverCleanupDisposition.ScheduledForReboot,
                    "已安排在下次 Windows 重启时删除。"));
                continue;
            }

            if (!IsDeletable(node, out string reason))
            {
                results.Add(new LeftoverCleanupResult(node, false, reason));
                continue;
            }

            results.Add(DeleteSingle(node));
        }

        return results;
    }

    public static bool IsDeletable(LeftoverNode? node, out string reason)
    {
        reason = string.Empty;
        if (node is null || node.IsGroup || !node.IsFileSystemItem)
        {
            reason = "不是文件系统项。";
            return false;
        }

        switch (node.Kind)
        {
            case LeftoverKind.Shortcut:
                return IsDeletableShortcut(node.FilePath, out reason);
            case LeftoverKind.FileItem:
                return IsDeletableFile(node, out reason);
            case LeftoverKind.InstallFolder:
            case LeftoverKind.AppDataFolder:
            case LeftoverKind.FolderItem:
                return IsDeletableFolder(node, out reason);
            default:
                reason = "该类型不支持文件删除。";
                return false;
        }
    }

    private static bool IsDeletableShortcut(string path, out string reason)
    {
        reason = string.Empty;
        if (!path.EndsWith(".lnk", StringComparison.OrdinalIgnoreCase))
        {
            reason = "只允许删除 .lnk 快捷方式文件。";
            return false;
        }

        string full = Normalize(path);
        if (full.Length == 0)
        {
            reason = "路径无效。";
            return false;
        }

        if (!File.Exists(full))
        {
            reason = "快捷方式已不存在。";
            return false;
        }

        if (!LeftoverScanner.ShortcutRoots().Any(root => IsUnder(full, root)))
        {
            reason = "只能删除开始菜单或桌面上的快捷方式。";
            return false;
        }

        return true;
    }

    private static bool IsDeletableFile(LeftoverNode node, out string reason)
    {
        reason = string.Empty;
        string full = Normalize(node.FilePath);
        if (full.Length == 0)
        {
            reason = "路径无效。";
            return false;
        }

        string root = Normalize(node.RootPath);
        if (root.Length == 0 || !IsUnder(full, root))
        {
            reason = "该文件不在扫描到的残留目录内，已跳过。";
            return false;
        }

        if (!IsSafeFolderRoot(root, out reason))
        {
            return false;
        }

        if (!File.Exists(full))
        {
            reason = "文件已不存在。";
            return false;
        }

        return true;
    }

    private static bool IsDeletableFolder(LeftoverNode node, out string reason)
    {
        reason = string.Empty;
        if (node.Children.Count > 0)
        {
            reason = "文件夹只能通过其中的文件删除，请勾选具体文件。";
            return false;
        }

        string full = Normalize(node.FilePath);
        if (full.Length == 0 || !Directory.Exists(full))
        {
            reason = "目录已不存在。";
            return false;
        }

        if (node.RootPath.Length > 0 && !IsUnder(full, node.RootPath) &&
            !string.Equals(full, Normalize(node.RootPath), StringComparison.OrdinalIgnoreCase))
        {
            reason = "该目录不在扫描到的残留目录内，已跳过。";
            return false;
        }

        return IsSafeFolderRoot(full, out reason);
    }

    /// <summary>Shared root validation for install folders, AppData folders and file parents.</summary>
    private static bool IsSafeFolderRoot(string path, out string reason)
    {
        reason = string.Empty;
        if (!LeftoverScanner.IsSafeFolder(path))
        {
            reason = "该目录不在允许删除的范围内（不能是磁盘根目录、Windows 目录或系统目录本身）。";
            return false;
        }

        string full = Normalize(path);
        if (full.Length == 0)
        {
            reason = "路径无效。";
            return false;
        }

        string[] protectedRoots =
        [
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
            Environment.GetFolderPath(Environment.SpecialFolder.CommonDesktopDirectory),
            Environment.GetFolderPath(Environment.SpecialFolder.Desktop),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Temp")
        ];
        foreach (string root in protectedRoots)
        {
            if (root.Length > 0 && string.Equals(root.TrimEnd('\\'), full.TrimEnd('\\'),
                    StringComparison.OrdinalIgnoreCase))
            {
                reason = "不能删除系统目录本身。";
                return false;
            }
        }

        string[] allowedRoots = protectedRoots.Take(6).ToArray();
        if (allowedRoots.Any(root => root.Length > 0 && IsUnder(full, root)))
        {
            return true;
        }

        if (!IsDeepDrivePath(full))
        {
            reason = "目录层级过浅，已跳过。";
            return false;
        }

        string userProfile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string roamingAppData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        if (userProfile.Length > 0 && IsUnder(full, userProfile) &&
            !IsUnder(full, localAppData) && !IsUnder(full, roamingAppData))
        {
            reason = "该目录位于用户个人文件夹（文档、桌面等）内，已拒绝删除。";
            return false;
        }

        return true;
    }

    private static LeftoverCleanupResult DeleteSingle(LeftoverNode node)
    {
        string lockNote = string.Empty;
        if (node.Kind is LeftoverKind.FileItem or LeftoverKind.Shortcut &&
            FileLockInspector.IsLocked(node.FilePath))
        {
            // Check the sharing lock before deleting, end normal user processes that keep the file
            // open, then fall through to the normal delete/reboot path.
            lockNote = FileLockInspector.TryRelease(node.FilePath).Note;
        }

        try
        {
            switch (node.Kind)
            {
                case LeftoverKind.Shortcut:
                    File.Delete(node.FilePath);
                    // A Start Menu program group that only held this app's links disappears too.
                    RemoveEmptyParents(Path.GetDirectoryName(node.FilePath) ?? string.Empty, node.RootPath);
                    return new LeftoverCleanupResult(node, true, AppendNote("已删除该快捷方式。", lockNote));
                case LeftoverKind.FileItem:
                    File.Delete(node.FilePath);
                    RemoveEmptyParents(node.FilePath, node.RootPath);
                    return new LeftoverCleanupResult(node, true, AppendNote("已删除该文件。", lockNote));
                default:
                    Directory.Delete(node.FilePath, recursive: false);
                    RemoveEmptyParents(Path.GetDirectoryName(node.FilePath) ?? string.Empty, node.RootPath);
                    return new LeftoverCleanupResult(node, true, "已删除空文件夹。");
            }
        }
        catch (Exception exception) when (exception is UnauthorizedAccessException or SecurityException)
        {
            if (TryScheduleForReboot(node))
            {
                return new LeftoverCleanupResult(
                    node,
                    LeftoverCleanupDisposition.ScheduledForReboot,
                    AppendNote("当前无法删除，已安排在下次 Windows 重启时自动删除。", lockNote));
            }

            return new LeftoverCleanupResult(
                node,
                LeftoverCleanupDisposition.Failed,
                AppendNote("拒绝访问，且无法安排重启删除；请重启 Windows 后重试，或以管理员身份运行本应用。", lockNote));
        }
        catch (Exception exception) when (exception is DirectoryNotFoundException or FileNotFoundException)
        {
            return new LeftoverCleanupResult(node, true, "目标已不存在。");
        }
        catch (Exception exception) when (exception is IOException or NotSupportedException
                                              or ArgumentException)
        {
            if (TryScheduleForReboot(node))
            {
                return new LeftoverCleanupResult(
                    node,
                    LeftoverCleanupDisposition.ScheduledForReboot,
                    AppendNote("无法立即删除（文件被占用或目录非空），已安排在下次 Windows 重启时自动删除。", lockNote));
            }

            return new LeftoverCleanupResult(
                node,
                LeftoverCleanupDisposition.Failed,
                AppendNote("删除失败，且无法安排重启删除；请重启 Windows 后重试。", lockNote));
        }
    }

    private static string AppendNote(string message, string note)
        => note.Length == 0 ? message : $"{message}（{note}）";

    /// <summary>
    /// Registers the path in Windows' pending-rename list, the same delayed removal the target
    /// program uses (<c>MoveFileEx(..., MOVEFILE_DELAY_UNTIL_REBOOT)</c>). Anything that cannot be
    /// removed now is scheduled and reported as such; if Windows refuses the registration the
    /// caller reports a normal failure.
    /// </summary>
    private static bool TryScheduleForReboot(LeftoverNode node)
    {
        if (node.Kind is not (LeftoverKind.FileItem or LeftoverKind.Shortcut or LeftoverKind.FolderItem
            or LeftoverKind.InstallFolder or LeftoverKind.AppDataFolder))
        {
            return false;
        }

        string path = Normalize(node.FilePath);
        if (path.Length == 0)
        {
            return false;
        }

        if (!MoveFileEx(path, null, MoveFileDelayUntilReboot))
        {
            return false;
        }

        return true;
    }

    /// <summary>Removes folders that became empty after deleting their files, up to the scan root.</summary>
    private static void RemoveEmptyParents(string directory, string root)
    {
        string normalizedRoot = Normalize(root);
        string? current = Normalize(directory);
        while (current is not null &&
               normalizedRoot.Length > 0 &&
               IsUnder(current, normalizedRoot) &&
               Directory.Exists(current))
        {
            try
            {
                if (Directory.EnumerateFileSystemEntries(current).Any())
                {
                    return;
                }

                Directory.Delete(current, recursive: false);
            }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                                  or SecurityException or DirectoryNotFoundException)
            {
                return;
            }

            current = Path.GetDirectoryName(current);
        }
    }

    private static string Normalize(string path)
    {
        try
        {
            return Path.GetFullPath(path.Trim()).TrimEnd('\\');
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException
                                              or PathTooLongException or SecurityException)
        {
            return string.Empty;
        }
    }

    private static bool IsUnder(string path, string root)
    {
        string normalizedRoot = root.TrimEnd('\\');
        return normalizedRoot.Length > 0 &&
               path.Length > normalizedRoot.Length &&
               path.StartsWith(normalizedRoot + "\\", StringComparison.OrdinalIgnoreCase);
    }

    private static bool IsDeepDrivePath(string full)
    {
        string root = Path.GetPathRoot(full) ?? string.Empty;
        if (root.Length == 0)
        {
            return false;
        }

        string[] parts = full[root.Length..].Split('\\', StringSplitOptions.RemoveEmptyEntries);
        return parts.Length >= 2;
    }
}
