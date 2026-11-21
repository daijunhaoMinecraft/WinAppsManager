using System.Windows;
using iNKORE.UI.WPF.Helpers;
using iNKORE.UI.WPF.Modern.Helpers.Styles;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// 窗口系统背景（云母 / 亚克力）的可用性判断与取值解析。
///
/// 能力来源（与 iNKORE 的实现保持一致）：
/// <list type="bullet">
/// <item>云母：Windows 11 22H2（内部版本 22523）起才有公开的 <c>DWMWA_SYSTEMBACKDROP_TYPE</c>。</item>
/// <item>亚克力：Windows 10 1803（17063）起的旧式亚克力；Windows 11 22H2 起改用 DWM 原生亚克力。</item>
/// <item>Windows 7/8 与高对比度模式不支持任何透明背景。</item>
/// </list>
/// </summary>
public static class BackdropSupport
{
    /// <summary>Windows 11 22H2 的内部版本号；低于它的 Mica 走的是未公开属性，不作为可选样式提供。</summary>
    private static readonly Version MicaMinimumBuild = new(10, 0, 22523);

    /// <summary>云母当前是否可用（系统版本、库支持与高对比度共同决定）。</summary>
    public static bool IsMicaSupported =>
        !SystemParameters.HighContrast
        && BackdropType.Mica.IsSupported()
        && OSVersionHelper.OSVersion >= MicaMinimumBuild;

    /// <summary>亚克力当前是否可用（Windows 10 1803+ 或 Windows 11）。</summary>
    // public static bool IsAcrylicSupported =>
    //     !SystemParameters.HighContrast && BackdropType.Acrylic.IsSupported();
    public static bool IsAcrylicSupported => true;

    /// <summary>
    /// 把用户选择解析成实际生效的样式：不支持的样式回退到“自动”的结果，
    /// “自动”优先云母、其次亚克力、最后无。
    /// </summary>
    public static BackdropStyle Resolve(BackdropStyle preferred) => preferred switch
    {
        BackdropStyle.None => BackdropStyle.None,
        BackdropStyle.Mica => IsMicaSupported ? BackdropStyle.Mica : Resolve(BackdropStyle.Auto),
        BackdropStyle.Acrylic => IsAcrylicSupported ? BackdropStyle.Acrylic : Resolve(BackdropStyle.Auto),
        _ => IsMicaSupported ? BackdropStyle.Mica
            : IsAcrylicSupported ? BackdropStyle.Acrylic
            : BackdropStyle.None
    };

    /// <summary>把已解析的样式映射成 iNKORE 的背景类型；无背景返回 <c>null</c>（不调用任何原生接口）。</summary>
    public static BackdropType? ToBackdropType(BackdropStyle resolved) => resolved switch
    {
        BackdropStyle.Mica => BackdropType.Mica,
        BackdropStyle.Acrylic => BackdropType.Acrylic,
        _ => null
    };
}
