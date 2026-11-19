using System.IO;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Resolves the folder an application lives in: the registry <c>InstallLocation</c> value first,
/// then the folder the <c>DisplayIcon</c> path points into and finally the folder of the uninstall
/// executable. Many installers (Electron, NSIS, burn bundles) only write <c>DisplayIcon</c>, so
/// without those fallbacks the install date probe has nothing to look at (the size probe
/// deliberately stays on the declared location) and the cell stays empty.
/// </summary>
public static class InstallLocationResolver
{
    /// <summary>
    /// Folders that are shared by every application: probing their last write time would produce a
    /// date that has nothing to do with the application, so they are never used as install folders.
    /// </summary>
    private static readonly Lazy<string[]> SharedRoots = new(CreateSharedRoots);

    /// <summary>Candidate folders in resolution order; they may not exist.</summary>
    public static IReadOnlyList<string> GetCandidateFolders(InstalledApplication application)
    {
        ArgumentNullException.ThrowIfNull(application);

        var candidates = new List<string>(3);
        AddCandidate(candidates, NormalizeFolder(application.InstallLocation, isFilePathHint: false));
        AddCandidate(candidates, NormalizeFolder(DisplayIconPath(application.DisplayIcon), isFilePathHint: true));
        AddCandidate(candidates, NormalizeFolder(UninstallExecutablePath(application.UninstallString), isFilePathHint: true));
        return candidates;
    }

    /// <summary>First candidate folder that exists on disk, or <c>null</c> when none does.</summary>
    public static string? FindExistingFolder(InstalledApplication application)
    {
        foreach (string folder in GetCandidateFolders(application))
        {
            if (Directory.Exists(folder))
            {
                return folder;
            }
        }

        return null;
    }

    private static void AddCandidate(List<string> folders, string? folder)
    {
        if (folder is null)
        {
            return;
        }

        if (SharedRoots.Value.Contains(folder, StringComparer.OrdinalIgnoreCase))
        {
            return;
        }

        if (!folders.Contains(folder, StringComparer.OrdinalIgnoreCase))
        {
            folders.Add(folder);
        }
    }

    /// <summary>
    /// Normalizes one raw value into a fully qualified folder. Values that name a file (icon and
    /// uninstall command) always contribute their parent folder; <c>InstallLocation</c> keeps the
    /// value as a folder even when the application was removed from disk already.
    /// </summary>
    private static string? NormalizeFolder(string? path, bool isFilePathHint)
    {
        string text = Environment.ExpandEnvironmentVariables((path ?? string.Empty).Trim().Trim('"')).Trim();
        // UNC and device paths are skipped: probing them can block on the network, and their date
        // says nothing about when the application was installed here.
        if (text.Length == 0 ||
            !Path.IsPathFullyQualified(text) ||
            text.StartsWith(@"\\", StringComparison.Ordinal))
        {
            return null;
        }

        bool isFolder = Directory.Exists(text) || (!isFilePathHint && !Path.HasExtension(text));
        if (!isFolder)
        {
            text = Path.GetDirectoryName(text) ?? string.Empty;
        }

        if (text.Length == 0 || !Path.IsPathFullyQualified(text))
        {
            return null;
        }

        // "C:\" is a drive root, never an application folder.
        string trimmed = Path.TrimEndingDirectorySeparator(text);
        string? root = Path.GetPathRoot(trimmed);
        if (root is not null &&
            string.Equals(Path.TrimEndingDirectorySeparator(root), trimmed, StringComparison.OrdinalIgnoreCase))
        {
            return null;
        }

        return trimmed;
    }

    /// <summary>Same parsing the icon loader uses: quotes, environment variables and the ",index" suffix.</summary>
    private static string? DisplayIconPath(string? displayIcon)
        => InstalledApplicationIconService.TryParse(displayIcon, out string path, out _) ? path : null;

    /// <summary>The executable named by <c>UninstallString</c>; msiexec and friends carry no folder.</summary>
    private static string? UninstallExecutablePath(string? uninstallString)
    {
        if (string.IsNullOrWhiteSpace(uninstallString) ||
            !UninstallCommandParser.TryParse(uninstallString, out var command, out _) ||
            command is null)
        {
            return null;
        }

        string fileName = command.FileName;
        if (!Path.IsPathFullyQualified(fileName))
        {
            // "msiexec.exe /X{...}" and other PATH-relative tools do not describe an install folder.
            return null;
        }

        return fileName;
    }

    private static string[] CreateSharedRoots()
    {
        var roots = new List<string>
        {
            Environment.GetFolderPath(Environment.SpecialFolder.Windows),
            Environment.GetFolderPath(Environment.SpecialFolder.System),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
            Environment.GetFolderPath(Environment.SpecialFolder.CommonProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.CommonProgramFilesX86),
            Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.UserProfile)
        };

        return roots
            .Where(root => !string.IsNullOrWhiteSpace(root))
            .Select(root => root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar))
            .Where(root => root.Length > 0)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
    }
}
