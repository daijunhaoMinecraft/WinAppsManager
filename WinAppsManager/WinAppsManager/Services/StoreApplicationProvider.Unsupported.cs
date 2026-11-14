#if !STORE_APPS
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Windows 7 / 8 / 8.1 目标（net6.0-windows7.0）没有 WinRT <c>PackageManager</c>，
/// 因此 MSIX / Microsoft Store 应用管理不可用：项目统一通过 <see cref="IsSupported"/>
/// 判断并给出明确的“当前系统不支持”提示，而不是让调用方抛平台异常。
/// </summary>
public static partial class StoreApplicationProvider
{
    /// <summary><c>false</c>：当前目标不支持枚举商店应用。</summary>
    public static bool IsSupported => false;

    /// <summary>不支持时的原因说明（Windows 7 兼容构建不含 WinRT 商店应用管理）。</summary>
    public static string UnsupportedReason =>
        "当前为 Windows 7 兼容构建，不包含商店应用管理；在 Windows 10 2004 及以上请使用完整版客户端。";

    /// <summary>目标平台不支持商店应用时返回空集合（不会抛异常）。</summary>
    public static Task<IReadOnlyList<InstalledApplication>> GetInstalledStoreApplicationsAsync(
        CancellationToken cancellationToken = default)
        => Task.FromResult<IReadOnlyList<InstalledApplication>>(Array.Empty<InstalledApplication>());

    /// <summary>目标平台不支持商店应用时返回空集合（不会抛异常）。</summary>
    public static IReadOnlyList<InstalledApplication> Scan(CancellationToken cancellationToken = default)
        => Array.Empty<InstalledApplication>();
}
#endif
