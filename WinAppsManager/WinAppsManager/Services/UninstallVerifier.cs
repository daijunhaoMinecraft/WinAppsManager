using System.IO;
using System.Runtime.InteropServices;
using System.Security.Principal;
using Microsoft.Win32;
#if STORE_APPS
using Windows.Management.Deployment;
#endif
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Checks whether an application is still registered after its uninstaller finished. This is
/// the ground truth for the result list: exit codes from vendor uninstallers are unreliable,
/// but a missing uninstall registry key (or package) means the software is gone.
/// </summary>
public static class UninstallVerifier
{
    /// <returns>
    /// <c>true</c> when the software is still registered, <c>false</c> when it is gone and
    /// <c>null</c> when the state cannot be determined.
    /// </returns>
    public static bool? IsStillInstalled(InstalledApplication application)
    {
        ArgumentNullException.ThrowIfNull(application);
#if STORE_APPS
        return application.IsStoreApplication
            ? IsStorePackageInstalled(application.PackageFullName)
            : IsRegistryEntryPresent(application);
#else
        // Windows 7 / 8 目标没有 WinRT PackageManager：商店包状态无法核对，按“无法判定”处理。
        return application.IsStoreApplication ? null : IsRegistryEntryPresent(application);
#endif
    }

    private static bool? IsRegistryEntryPresent(InstalledApplication application)
    {
        if (!application.HasRegistryEntry)
        {
            return null;
        }

        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(application.RegistryHive, application.RegistryView);
            using var key = baseKey.OpenSubKey(application.RegistryPath);
            return key is not null;
        }
        catch (Exception exception) when (exception is System.Security.SecurityException
                                              or UnauthorizedAccessException or IOException
                                              or PlatformNotSupportedException)
        {
            AppLogService.Warn(
                $"核对桌面软件是否仍安装失败：{application.Name}（{application.Id}），" +
                $"{exception.GetType().Name}：{exception.Message}，结果按无法判定处理。",
                exception);
            return null;
        }
    }

#if STORE_APPS
    private static bool? IsStorePackageInstalled(string packageFullName)
    {
        if (string.IsNullOrWhiteSpace(packageFullName))
        {
            return null;
        }

        string? securityId = WindowsIdentity.GetCurrent().User?.Value;
        if (string.IsNullOrWhiteSpace(securityId))
        {
            return null;
        }

        try
        {
            var manager = new PackageManager();
            return manager.FindPackagesForUser(securityId, packageFullName).Any();
        }
        catch (Exception exception) when (exception is COMException or UnauthorizedAccessException
                                              or System.Security.SecurityException or InvalidOperationException
                                              or ArgumentException or FileNotFoundException)
        {
            AppLogService.Warn(
                $"核对商店应用是否仍安装失败：{packageFullName}，{exception.GetType().Name}：{exception.Message}，结果按无法判定处理。",
                exception);
            return null;
        }
    }
#endif
}
