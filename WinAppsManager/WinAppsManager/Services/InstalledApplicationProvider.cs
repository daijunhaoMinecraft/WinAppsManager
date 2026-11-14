using System.Globalization;
using System.IO;
using System.Security;
using Microsoft.Win32;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Reads the classic desktop application uninstall entries that Programs and Features
/// uses. Microsoft Store packages are intentionally out of scope for the first version.
/// </summary>
public static class InstalledApplicationProvider
{
    private const string UninstallKeyPath = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall";
    private const string MachineLabel = "本机安装";
    private const string UserLabel = "当前用户";
    private static readonly StringComparer NameComparer =
        StringComparer.Create(CultureInfo.GetCultureInfo("zh-CN"), true);

    public static Task<IReadOnlyList<InstalledApplication>> GetInstalledApplicationsAsync(
        CancellationToken cancellationToken = default)
        => Task.Run<IReadOnlyList<InstalledApplication>>(() => Scan(cancellationToken), cancellationToken);

    public static IReadOnlyList<InstalledApplication> Scan(CancellationToken cancellationToken = default)
    {
        var entries = new List<UninstallRegistryEntry>();
        ReadHive(RegistryHive.LocalMachine, RegistryView.Registry64, "hklm64", MachineLabel, entries, cancellationToken);
        if (Environment.Is64BitOperatingSystem)
        {
            ReadHive(RegistryHive.LocalMachine, RegistryView.Registry32, "hklm32", MachineLabel, entries, cancellationToken);
        }

        // HKCU\Software is shared between 32-bit and 64-bit desktop applications.
        ReadHive(RegistryHive.CurrentUser, RegistryView.Default, "hkcu", UserLabel, entries, cancellationToken);

        var applications = new List<InstalledApplication>(entries.Count);
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in entries)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var application = InstalledApplication.TryCreate(entry);
            if (application is null)
            {
                continue;
            }

            string signature = string.Join(
                '\u001f',
                application.Name.ToUpperInvariant(),
                application.Version.ToUpperInvariant(),
                NormalizeCommand(application.UninstallString));
            if (seen.Add(signature))
            {
                applications.Add(application);
            }
        }

        applications.Sort((left, right) =>
        {
            int byName = NameComparer.Compare(left.Name, right.Name);
            return byName != 0 ? byName : StringComparer.Ordinal.Compare(left.Id, right.Id);
        });
        return applications;
    }

    private static void ReadHive(
        RegistryHive hive,
        RegistryView view,
        string idPrefix,
        string hiveLabel,
        ICollection<UninstallRegistryEntry> entries,
        CancellationToken cancellationToken)
    {
        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(hive, view);
            using var uninstallKey = baseKey.OpenSubKey(UninstallKeyPath);
            if (uninstallKey is null)
            {
                return;
            }

            foreach (string subKeyName in uninstallKey.GetSubKeyNames())
            {
                cancellationToken.ThrowIfCancellationRequested();
                try
                {
                    using var subKey = uninstallKey.OpenSubKey(subKeyName);
                    if (subKey is null)
                    {
                        continue;
                    }

                    entries.Add(new UninstallRegistryEntry(
                        Id: $"{idPrefix}:{subKeyName}",
                        HiveLabel: hiveLabel,
                        RegistryPath: $"{UninstallKeyPath}\\{subKeyName}",
                        DisplayName: ReadString(subKey, "DisplayName"),
                        DisplayVersion: ReadString(subKey, "DisplayVersion"),
                        Publisher: ReadString(subKey, "Publisher"),
                        UninstallString: ReadString(subKey, "UninstallString"),
                        InstallLocation: ReadInstallLocation(subKey),
                        InstallDate: ReadString(subKey, "InstallDate"),
                        EstimatedSizeBytes: ReadEstimatedSizeBytes(subKey),
                        IsSystemComponent: ReadFlag(subKey, "SystemComponent"),
                        NoRemove: ReadFlag(subKey, "NoRemove"),
                        ReleaseType: ReadString(subKey, "ReleaseType"),
                        ParentKeyName: ReadString(subKey, "ParentKeyName"),
                        DisplayIcon: ReadString(subKey, "DisplayIcon"),
                        Hive: hive,
                        View: view,
                        UrlInfoAbout: ReadFirstString(subKey, "URLInfoAbout", "HelpLink")));
                }
                catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException or IOException)
                {
                    // A single protected or malformed key must not hide the rest of the list.
                }
            }
        }
        catch (Exception exception) when (exception is SecurityException or UnauthorizedAccessException or IOException
            or PlatformNotSupportedException)
        {
            // The 64-bit view is unavailable on 32-bit Windows and some policy-locked systems.
        }
    }

    private static string? ReadString(RegistryKey key, string name)
    {
        object? value = key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
        return value switch
        {
            string text => text,
            int number => number.ToString(CultureInfo.InvariantCulture),
            long number => number.ToString(CultureInfo.InvariantCulture),
            _ => null
        };
    }

    /// <summary>
    /// Reads the folder the application is installed in: <c>InstallLocation</c> falls back to the
    /// values Inno Setup installers write instead
    /// (<c>Inno Setup: App Path</c>, then <c>InstallDir</c>), and expands environment variables the
    /// way <c>RegQueryValueExW</c> with <c>REG_EXPAND_SZ</c> does; without that a vendor path such as
    /// <c>%ProgramFiles%\App</c> cannot be probed for the install date.
    /// </summary>
    private static string? ReadInstallLocation(RegistryKey key)
        => SelectInstallLocation(
            ReadString(key, "InstallLocation"),
            ReadString(key, "Inno Setup: App Path"),
            ReadString(key, "InstallDir"));

    /// <summary>The first non-empty candidate, with environment variables expanded.</summary>
    public static string? SelectInstallLocation(string? installLocation, string? innoSetupAppPath, string? installDir)
    {
        foreach (string? candidate in new[] { installLocation, innoSetupAppPath, installDir })
        {
            if (string.IsNullOrWhiteSpace(candidate))
            {
                continue;
            }

            string expanded = Environment.ExpandEnvironmentVariables(candidate.Trim()).Trim();
            if (expanded.Length > 0)
            {
                return expanded;
            }
        }

        return null;
    }

    /// <summary>Reads the first non-empty string value, falling back to HelpLink.</summary>
    private static string? ReadFirstString(RegistryKey key, string primary, string fallback)
    {
        string? value = ReadString(key, primary);
        return string.IsNullOrWhiteSpace(value) ? ReadString(key, fallback) : value;
    }

    private static bool ReadFlag(RegistryKey key, string name)
    {
        object? value = key.GetValue(name, null, RegistryValueOptions.DoNotExpandEnvironmentNames);
        return value switch
        {
            int number => number != 0,
            long number => number != 0,
            string text when int.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out int number)
                => number != 0,
            _ => false
        };
    }

    private static long? ReadEstimatedSizeBytes(RegistryKey key)
    {
        object? value = key.GetValue("EstimatedSize", null, RegistryValueOptions.DoNotExpandEnvironmentNames);
        long? kilobytes = value switch
        {
            int number => number,
            long number => number,
            string text when long.TryParse(text, NumberStyles.Integer, CultureInfo.InvariantCulture, out long number)
                => number,
            _ => null
        };
        return kilobytes is > 0 ? kilobytes.Value * 1024L : null;
    }

    private static string NormalizeCommand(string command) => command.Trim().ToUpperInvariant();
}
