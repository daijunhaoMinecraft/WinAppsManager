using System.Globalization;

namespace WinAppsManager.Models;

/// <summary>
/// 分类筛选项。<see cref="Name"/> 是界面显示与导航使用的分类名，<see cref="Id"/> 是服务端分类 ID，
/// 有值时优先按 ID 匹配目录条目（更稳定），否则按显示名匹配。
/// </summary>
public sealed record CategoryOption(string Name, int Count, string? Id = null)
{
    public string CountText => Count.ToString(CultureInfo.CurrentCulture);
}
