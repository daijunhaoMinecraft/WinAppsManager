using System.IO;
using Microsoft.Win32;
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
        // 商店包用 Get-AppxPackage 核对（单一 net6 构建没有 WinRT PackageManager），
        // 不支持商店应用的系统或查询失败时返回 null，按“无法判定”处理。
        return application.IsStoreApplication
            ? StoreApplicationProvider.IsPackageInstalled(application.PackageFullName)
            : IsRegistryEntryPresent(application);
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
}
