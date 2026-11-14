using System.Globalization;
using System.IO;
using System.Windows.Data;
using System.Windows.Media.Imaging;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

public sealed class CachedIconConverter : IMultiValueConverter
{
    public object? Convert(object[] values, Type targetType, object parameter, CultureInfo culture)
    {
        if (values.Length < 2 || values[0] is not string source || string.IsNullOrWhiteSpace(source)) return null;
        try
        {
            return IconImageDecoder.Decode(ReadIconBytes(source));
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            // Drop the broken entry so the next catalog refresh can fetch a clean copy.
            if (values[1] is AppItem app) _ = AppCatalogService.DiscardIconAsync(app);
            return null;
        }
    }

    public object[] ConvertBack(object value, Type[] targetTypes, object parameter, CultureInfo culture) => throw new NotSupportedException();

    private static byte[] ReadIconBytes(string source)
    {
        // Icons that could not be written to disk are served from memory instead of degrading to a letter badge.
        if (AppCatalogService.TryGetMemoryIcon(source, out var memory)) return memory;
        var info = new FileInfo(source);
        if (!info.Exists) throw new IOException("Cached icon is missing.");
        if (info.Length > IconImageDecoder.MaxIconBytes) throw new IOException("Cached icon is too large.");
        return File.ReadAllBytes(source);
    }
}

/// <summary>
/// Decodes cached PNG/ICO payloads. ICO containers are inspected directly so the best frame is chosen explicitly,
/// instead of relying on the WPF icon decoder to pick a frame from the whole file.
/// </summary>
internal static class IconImageDecoder
{
    public const int MaxIconBytes = 5 * 1024 * 1024;

    public static BitmapSource Decode(byte[] bytes)
    {
        if (bytes.Length == 0) throw new InvalidDataException("Icon data is empty.");
        var frames = IconImageFormat.ReadIcoFrames(bytes);
        var best = IconImageFormat.SelectBestIcoFrame(frames);
        if (best is not null)
        {
            try
            {
                if (best.IsPng)
                {
                    var png = IconImageFormat.ExtractIcoFrame(bytes, best);
                    if (png is not null) return DecodeBitmap(png);
                }
                else
                {
                    var wrapped = IconImageFormat.WrapIcoFrame(bytes, best);
                    if (wrapped is not null) return DecodeWithIconDecoder(wrapped);
                }
            }
            catch (Exception error) when (error is not OutOfMemoryException)
            {
                // Fall back to the whole container below.
            }
        }
        return DecodeBitmap(bytes);
    }

    private static BitmapSource DecodeBitmap(byte[] bytes)
    {
        using var stream = new MemoryStream(bytes, writable: false);
        var decoder = BitmapDecoder.Create(stream, BitmapCreateOptions.IgnoreImageCache, BitmapCacheOption.OnLoad);
        return LargestFrozenFrame(decoder);
    }

    private static BitmapSource DecodeWithIconDecoder(byte[] bytes)
    {
        using var stream = new MemoryStream(bytes, writable: false);
        var decoder = new IconBitmapDecoder(stream, BitmapCreateOptions.IgnoreImageCache, BitmapCacheOption.OnLoad);
        return LargestFrozenFrame(decoder);
    }

    private static BitmapSource LargestFrozenFrame(BitmapDecoder decoder)
    {
        var frame = decoder.Frames.OrderByDescending(item => (long)item.PixelWidth * item.PixelHeight)
            .ThenByDescending(item => item.PixelWidth).First();
        frame.Freeze();
        return frame;
    }
}
