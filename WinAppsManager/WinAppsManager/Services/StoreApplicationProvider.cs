#if STORE_APPS
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Xml.Linq;
using Windows.ApplicationModel;
using Windows.Management.Deployment;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Enumerates Microsoft Store / MSIX packages registered for the current Windows user.
/// Only packages whose install location is under WindowsApps are considered; frameworks,
/// resource packages, bundles and stubs are hidden because they are not user-facing apps.
/// </summary>
public static partial class StoreApplicationProvider
{
    /// <summary>
    /// <c>true</c>：当前系统支持枚举 MSIX / 商店应用。编译期需要 STORE_APPS（net8 目标），
    /// 运行期还要求 Windows 10 2004（10.0.19041）及以上；Windows 7 / 8.1 或更旧的 Win10 上返回
    /// <c>false</c>，由界面提示“当前系统不支持商店应用”，而不是抛平台异常。
    /// </summary>
    public static bool IsSupported => OperatingSystem.IsWindowsVersionAtLeast(10, 0, 19041);

    /// <summary>不支持时的原因说明；支持时为空字符串。</summary>
    public static string UnsupportedReason => IsSupported
        ? string.Empty
        : "当前系统不支持商店应用（需要 Windows 10 2004 及以上）";

    private const string StoreSource = "商店应用";
    private static readonly StringComparer NameComparer =
        StringComparer.Create(CultureInfo.GetCultureInfo("zh-CN"), true);

    public static Task<IReadOnlyList<InstalledApplication>> GetInstalledStoreApplicationsAsync(
        CancellationToken cancellationToken = default)
        => Task.Run<IReadOnlyList<InstalledApplication>>(() => Scan(cancellationToken), cancellationToken);

    public static IReadOnlyList<InstalledApplication> Scan(CancellationToken cancellationToken = default)
    {
        string? securityId = WindowsIdentity.GetCurrent().User?.Value;
        if (string.IsNullOrWhiteSpace(securityId))
        {
            return Array.Empty<InstalledApplication>();
        }

        var manager = new PackageManager();
        var applications = new List<InstalledApplication>();
        foreach (var package in manager.FindPackagesForUser(securityId))
        {
            cancellationToken.ThrowIfCancellationRequested();
            try
            {
                var application = CreateApplication(package);
                if (application is not null)
                {
                    applications.Add(application);
                }
            }
            catch (Exception exception) when (exception is COMException or UnauthorizedAccessException
                or System.Security.SecurityException or IOException or InvalidOperationException or ArgumentException)
            {
                // A broken package should not hide the rest of the Store app list.
                Debug.WriteLine(exception);
            }
        }

        applications.Sort((left, right) =>
        {
            int byName = NameComparer.Compare(left.Name, right.Name);
            return byName != 0 ? byName : StringComparer.Ordinal.Compare(left.Id, right.Id);
        });
        return applications;
    }

    private static InstalledApplication? CreateApplication(Package package)
    {
        if (package.IsFramework || package.IsResourcePackage || package.IsBundle || package.IsStub)
        {
            return null;
        }

        string installPath = package.InstalledLocation?.Path ?? string.Empty;
        if (installPath.Length == 0 || !installPath.Contains(@"\WindowsApps\", StringComparison.OrdinalIgnoreCase))
        {
            return null;
        }

        if (!HasApplication(installPath))
        {
            return null;
        }

        string fullName = package.Id.FullName;
        string packageName = package.Id.Name?.Trim() ?? string.Empty;
        string familyName = package.Id.FamilyName?.Trim() ?? string.Empty;
        if (string.IsNullOrWhiteSpace(fullName) || packageName.Length == 0)
        {
            return null;
        }

        string name = ReadDisplayName(package);
        if (name.Length == 0)
        {
            return null;
        }

        var version = package.Id.Version;
        DateOnly? installedDate = null;
        try
        {
            installedDate = DateOnly.FromDateTime(package.InstalledDate.LocalDateTime);
        }
        catch (Exception exception) when (exception is ArgumentException or InvalidOperationException or COMException)
        {
            Debug.WriteLine(exception);
        }

        string uninstallCommand;
        try
        {
            uninstallCommand = StoreApplicationUninstallCommand.Build(packageName);
        }
        catch (ArgumentException exception)
        {
            Debug.WriteLine(exception);
            return null;
        }

        return new InstalledApplication
        {
            Id = "store:" + fullName,
            Name = name,
            Version = $"{version.Major}.{version.Minor}.{version.Build}.{version.Revision}",
            Publisher = ReadPublisher(package),
            SourceText = StoreSource,
            Category = ApplicationCategory.StoreApplication,
            UninstallString = uninstallCommand,
            DisplayIcon = ReadLogoPath(package, installPath) ?? string.Empty,
            InstallLocation = installPath,
            InstallDate = installedDate,
            PackageFullName = fullName,
            PackageName = packageName,
            PackageFamilyName = familyName
        };
    }

    private static bool HasApplication(string installPath)
    {
        string manifestPath = Path.Combine(installPath, "AppxManifest.xml");
        if (!File.Exists(manifestPath))
        {
            return true;
        }

        try
        {
            var document = XDocument.Load(manifestPath);
            var root = document.Root;
            if (root is null)
            {
                return false;
            }

            var applications = root.Element(root.Name.Namespace + "Applications");
            return applications?.Elements(root.Name.Namespace + "Application").Any() == true;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
            or System.Security.SecurityException or System.Xml.XmlException)
        {
            // Keep the package if the manifest cannot be inspected; metadata may still be valid.
            Debug.WriteLine(exception);
            return true;
        }
    }

    private static string ReadDisplayName(Package package)
    {
        try
        {
            string? displayName = package.DisplayName?.Trim();
            if (!string.IsNullOrWhiteSpace(displayName) &&
                !displayName.StartsWith("ms-resource:", StringComparison.OrdinalIgnoreCase) &&
                !displayName.StartsWith("@{", StringComparison.Ordinal))
            {
                return displayName;
            }
        }
        catch (Exception exception) when (exception is COMException or IOException or UnauthorizedAccessException)
        {
            Debug.WriteLine(exception);
        }

        return package.Id.Name?.Trim() ?? string.Empty;
    }

    private static string ReadPublisher(Package package)
    {
        try
        {
            string? publisher = package.PublisherDisplayName?.Trim();
            if (!string.IsNullOrWhiteSpace(publisher) &&
                !publisher.StartsWith("ms-resource:", StringComparison.OrdinalIgnoreCase) &&
                !publisher.StartsWith("@{", StringComparison.Ordinal))
            {
                return publisher;
            }
        }
        catch (Exception exception) when (exception is COMException or IOException or UnauthorizedAccessException)
        {
            Debug.WriteLine(exception);
        }

        return package.Id.Publisher?.Trim() ?? string.Empty;
    }

    /// <summary>
    /// Picks the best scaled UWP asset for the manifest logo: looks for <c>.targetsize-N.png</c> /
    /// <c>.scale-N.png</c> siblings (and <c>_altform-unplated</c>), because the manifest usually
    /// points at the 100% asset.
    /// </summary>
    private static string ResolveBestLogo(string logoPath)
    {
        string? directory = Path.GetDirectoryName(logoPath);
        if (directory is null)
        {
            return logoPath;
        }

        string stem = ScaleSuffix().Replace(Path.GetFileNameWithoutExtension(logoPath), string.Empty);
        foreach (int size in new[] { 48, 44, 64, 32, 24, 256, 150, 100 })
        {
            string plain = Path.Combine(directory, $"{stem}.targetsize-{size}.png");
            if (File.Exists(plain))
            {
                return plain;
            }

            string unplatedVariant = Path.Combine(directory, $"{stem}.targetsize-{size}_altform-unplated.png");
            if (File.Exists(unplatedVariant))
            {
                return unplatedVariant;
            }
        }

        foreach (int scale in new[] { 200, 150, 125, 400, 100 })
        {
            string scaled = Path.Combine(directory, $"{stem}.scale-{scale}.png");
            if (File.Exists(scaled))
            {
                return scaled;
            }
        }

        string unplated = Path.Combine(directory, $"{stem}_altform-unplated.png");
        return File.Exists(unplated) ? unplated : logoPath;
    }

    private static string? ReadLogoPath(Package package, string installPath)
    {
        try
        {
            Uri? logo = package.Logo;
            if (logo is null)
            {
                return null;
            }

            if (logo.IsFile && File.Exists(logo.LocalPath))
            {
                return ResolveBestLogo(logo.LocalPath);
            }

            const string appxPrefix = "ms-appx:///";
            if (logo.OriginalString.StartsWith(appxPrefix, StringComparison.OrdinalIgnoreCase))
            {
                string relative = Uri.UnescapeDataString(logo.OriginalString[appxPrefix.Length..])
                    .Replace('/', Path.DirectorySeparatorChar);
                string root = Path.GetFullPath(installPath);
                string candidate = Path.GetFullPath(Path.Combine(root, relative));
                if (candidate.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase) &&
                    File.Exists(candidate))
                {
                    return ResolveBestLogo(candidate);
                }
            }
        }
        catch (Exception exception) when (exception is COMException or IOException or UnauthorizedAccessException
            or ArgumentException or UriFormatException)
        {
            Debug.WriteLine(exception);
        }

        return null;
    }

    [System.Text.RegularExpressions.GeneratedRegex(
        @"\.(?:scale-\d+|targetsize-\d+|contrast-[a-zA-Z]+)$",
        System.Text.RegularExpressions.RegexOptions.IgnoreCase | System.Text.RegularExpressions.RegexOptions.CultureInvariant)]
    private static partial System.Text.RegularExpressions.Regex ScaleSuffix();
}
#endif
