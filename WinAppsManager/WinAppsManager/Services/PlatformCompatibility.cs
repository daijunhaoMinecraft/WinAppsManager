using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// 判断目录条目是否可以在当前系统上安装。目录条目用 <c>minimum_windows</c> 与
/// <c>architectures</c> 声明兼容范围（由后端从官方系统要求核实后写入）；没有声明时按兼容处理，
/// 保证旧目录依然可用。
/// </summary>
internal static class PlatformCompatibility
{
    /// <summary>当前系统架构是否在该软件声明的架构里。</summary>
    public static bool SupportsArchitecture(AppItem app, string architecture)
    {
        if (app.Architectures is not { Count: > 0 } architectures)
        {
            return true;
        }

        foreach (var item in architectures)
        {
            if (string.Equals(item, architecture, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
    }

    /// <summary>当前 Windows 版本是否满足软件声明的最低版本。</summary>
    public static bool SupportsWindowsVersion(AppItem app, Version osVersion)
        => IsWindowsVersionAtLeast(osVersion, app.MinimumWindows);

    public static bool IsCompatible(AppItem app, string architecture, Version osVersion)
        => SupportsArchitecture(app, architecture) && SupportsWindowsVersion(app, osVersion);

    private static bool IsWindowsVersionAtLeast(Version osVersion, string? minimum)
    {
        if (string.IsNullOrWhiteSpace(minimum))
        {
            return true;
        }

        // Windows 11 与 Windows 10 的主次版本都是 10.0，只能靠 build 号区分（Win11 = 22000+）。
        switch (minimum.Trim())
        {
            case "7":
                return osVersion >= new Version(6, 1);
            case "8":
                return osVersion >= new Version(6, 2);
            case "8.1":
                return osVersion >= new Version(6, 3);
            case "10":
                return osVersion.Major >= 10;
            case "11":
                return osVersion.Major > 10 || (osVersion.Major == 10 && osVersion.Build >= 22000);
            default:
                return true;
        }
    }
}
