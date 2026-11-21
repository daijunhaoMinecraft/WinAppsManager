using System.ComponentModel;
using System.Windows;

namespace WinAppsManager.Models;

/// <summary>
/// 一组可拖动调整的列宽。页面的表头与每个数据行都绑定同一个实例，
/// 拖动表头分隔条时两边同步更新，不需要重建行或额外遍历可视化树。
/// </summary>
public sealed class ColumnLayout : INotifyPropertyChanged
{
    /// <summary>列宽下限，避免拖到 0 之后再也抓不到分隔条。</summary>
    public const double MinimumWidth = 44;

    /// <summary>列宽上限，防止一次误拖把列撑到几十万像素。</summary>
    public const double MaximumWidth = 900;

    private readonly GridLength[] _widths;

    public ColumnLayout(params double[] widths)
    {
        ArgumentNullException.ThrowIfNull(widths);
        _widths = widths.Select(width => new GridLength(Math.Clamp(width, MinimumWidth, MaximumWidth))).ToArray();
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    public int Count => _widths.Length;

    /// <summary>索引器供 XAML 以 <c>{Binding Columns[0]}</c> 方式绑定到 <c>ColumnDefinition.Width</c>。</summary>
    public GridLength this[int index] => _widths[index];

    /// <summary>把第 <paramref name="index"/> 列加宽 <paramref name="horizontalChange"/> 像素。</summary>
    public void Drag(int index, double horizontalChange)
    {
        if (index < 0 || index >= _widths.Length || horizontalChange == 0)
        {
            return;
        }

        double current = _widths[index].Value;
        double updated = Math.Clamp(current + horizontalChange, MinimumWidth, MaximumWidth);
        if (Math.Abs(updated - current) < 0.5)
        {
            return;
        }

        _widths[index] = new GridLength(updated);
        // "Item[]" 是 WPF 约定的“索引器整体变化”通知，绑定到任意 [n] 的 ColumnDefinition 都会刷新。
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs("Item[]"));
    }

    /// <summary>把列宽恢复成给定值（目前只在测试/重置时使用）。</summary>
    public void SetWidth(int index, double width)
    {
        if (index < 0 || index >= _widths.Length)
        {
            return;
        }

        _widths[index] = new GridLength(Math.Clamp(width, MinimumWidth, MaximumWidth));
        PropertyChanged?.Invoke(this, new PropertyChangedEventArgs("Item[]"));
    }
}

/// <summary>
/// 让 DataTemplate 内部的 ColumnDefinition 能绑定到页面级对象。
/// ColumnDefinition 不在可视化树里，用不了 RelativeSource；把页面本身放进一个
/// Freezable 资源当绑定源，是 WPF 里最稳妥的做法。
/// </summary>
public sealed class BindingProxy : Freezable
{
    public static readonly DependencyProperty DataProperty = DependencyProperty.Register(
        nameof(Data),
        typeof(object),
        typeof(BindingProxy),
        new UIPropertyMetadata(null));

    public object? Data
    {
        get => GetValue(DataProperty);
        set => SetValue(DataProperty, value);
    }

    protected override Freezable CreateInstanceCore() => new BindingProxy();
}
