using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace WinAppsManager.Dialogs;

/// <summary>
/// “自定义主题色”调色盘：色相 / 饱和度 / 亮度三个滑杆 + 十六进制输入，改动实时预览。
/// 只负责挑颜色，写入设置与重绘主题资源由 <c>SettingsPage</c> 完成。
/// </summary>
public partial class AccentColorPickerWindow : Window
{
    /// <summary>正在由代码同步控件，忽略控件引发的事件，避免互相触发。</summary>
    private bool _isSynchronizing;

    public AccentColorPickerWindow()
        : this(null)
    {
    }

    public AccentColorPickerWindow(string? initialColor)
    {
        InitializeComponent();
        ApplyInitialColor(initialColor);
    }

    /// <summary>确认后的颜色（<c>#RRGGBB</c>，大写）。</summary>
    public string SelectedColor { get; private set; } = "#2563EB";

    private Color CurrentColor => FromHsv(HueSlider.Value, SaturationSlider.Value / 100.0, ValueSlider.Value / 100.0);

    private void ApplyInitialColor(string? color)
    {
        Color start = TryParseHex(color, out Color parsed) ? parsed : Color.FromRgb(0x25, 0x63, 0xEB);
        var (hue, saturation, value) = ToHsv(start);
        _isSynchronizing = true;
        try
        {
            HueSlider.Value = hue;
            SaturationSlider.Value = saturation * 100;
            ValueSlider.Value = Math.Max(value * 100, ValueSlider.Minimum);
        }
        finally
        {
            _isSynchronizing = false;
        }

        RefreshPreview();
    }

    private void OnSliderChanged(object sender, RoutedPropertyChangedEventArgs<double> args)
    {
        if (HueText is null || _isSynchronizing)
        {
            return;
        }

        RefreshPreview();
    }

    private void HexBox_OnTextChanged(object sender, TextChangedEventArgs args)
    {
        if (_isSynchronizing || PreviewSwatch is null)
        {
            return;
        }

        if (!TryParseHex(HexBox.Text, out Color color))
        {
            ConfirmButton.IsEnabled = false;
            return;
        }

        ConfirmButton.IsEnabled = true;
        var (hue, saturation, value) = ToHsv(color);
        _isSynchronizing = true;
        try
        {
            HueSlider.Value = hue;
            SaturationSlider.Value = saturation * 100;
            ValueSlider.Value = Math.Max(value * 100, ValueSlider.Minimum);
        }
        finally
        {
            _isSynchronizing = false;
        }

        RefreshPreview();
    }

    /// <summary>按当前滑杆位置更新预览色、三根滑杆的渐变背景、数值标签与十六进制文本。</summary>
    private void RefreshPreview()
    {
        Color color = CurrentColor;
        SelectedColor = ToHex(color);
        PreviewSwatch.Background = new SolidColorBrush(color);
        HueText.Text = $"{Math.Round(HueSlider.Value)}°";
        SaturationText.Text = $"{Math.Round(SaturationSlider.Value)}%";
        ValueText.Text = $"{Math.Round(ValueSlider.Value)}%";

        double hue = HueSlider.Value;
        double saturation = SaturationSlider.Value / 100.0;
        double value = ValueSlider.Value / 100.0;
        SaturationSlider.Background = CreateGradient(
            FromHsv(hue, 0, value),
            FromHsv(hue, 1, value));
        ValueSlider.Background = CreateGradient(
            Colors.Black,
            FromHsv(hue, saturation, 1));

        if (!string.Equals(HexBox.Text, SelectedColor, StringComparison.OrdinalIgnoreCase))
        {
            _isSynchronizing = true;
            try
            {
                HexBox.Text = SelectedColor;
            }
            finally
            {
                _isSynchronizing = false;
            }
        }
    }

    private static LinearGradientBrush CreateGradient(Color from, Color to)
    {
        var brush = new LinearGradientBrush(from, to, new Point(0, 0.5), new Point(1, 0.5));
        brush.Freeze();
        return brush;
    }

    private static string ToHex(Color color) => $"#{color.R:X2}{color.G:X2}{color.B:X2}";

    private static bool TryParseHex(string? text, out Color color)
    {
        string value = (text ?? string.Empty).Trim().Trim('"');
        if (value.StartsWith('#'))
        {
            value = value[1..];
        }

        color = Colors.Transparent;
        if (value.Length != 6 || !value.All(Uri.IsHexDigit))
        {
            return false;
        }

        color = Color.FromRgb(
            Convert.ToByte(value[..2], 16),
            Convert.ToByte(value.Substring(2, 2), 16),
            Convert.ToByte(value.Substring(4, 2), 16));
        return true;
    }

    private static (double Hue, double Saturation, double Value) ToHsv(Color color)
    {
        double red = color.R / 255.0, green = color.G / 255.0, blue = color.B / 255.0;
        double maximum = Math.Max(red, Math.Max(green, blue));
        double minimum = Math.Min(red, Math.Min(green, blue));
        double delta = maximum - minimum;

        double hue = 0;
        if (delta > 0)
        {
            if (maximum == red)
            {
                hue = 60 * (((green - blue) / delta) % 6);
            }
            else if (maximum == green)
            {
                hue = 60 * (((blue - red) / delta) + 2);
            }
            else
            {
                hue = 60 * (((red - green) / delta) + 4);
            }
        }

        if (hue < 0)
        {
            hue += 360;
        }

        double saturation = maximum <= 0 ? 0 : delta / maximum;
        return (hue, saturation, maximum);
    }

    private static Color FromHsv(double hue, double saturation, double value)
    {
        double h = ((hue % 360) + 360) % 360 / 60.0;
        double s = Math.Clamp(saturation, 0, 1);
        double v = Math.Clamp(value, 0, 1);
        int sector = (int)Math.Floor(h) % 6;
        double fraction = h - Math.Floor(h);
        double p = v * (1 - s);
        double q = v * (1 - (s * fraction));
        double t = v * (1 - (s * (1 - fraction)));
        (double red, double green, double blue) = sector switch
        {
            0 => (v, t, p),
            1 => (q, v, p),
            2 => (p, v, t),
            3 => (p, q, v),
            4 => (t, p, v),
            _ => (v, p, q)
        };

        return Color.FromRgb(
            (byte)Math.Round(red * 255),
            (byte)Math.Round(green * 255),
            (byte)Math.Round(blue * 255));
    }

    private void ConfirmButton_OnClick(object sender, RoutedEventArgs args)
    {
        if (TryParseHex(HexBox.Text, out Color color))
        {
            SelectedColor = ToHex(color);
        }

        DialogResult = true;
    }

    private void CancelButton_OnClick(object sender, RoutedEventArgs args) => DialogResult = false;
}
