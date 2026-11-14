using System.Collections.Concurrent;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace WinAppsManager.Services;

/// <summary>
/// Extracts the icon referenced by the uninstall registry value <c>DisplayIcon</c>.
/// A registry value normally looks like <c>"C:\App\app.exe",0</c> or
/// <c>C:\App\app.ico</c>; the index selects an icon resource inside an EXE or DLL.
/// </summary>
public static class InstalledApplicationIconService
{
    private const int IconSize = 48;
    private static readonly ConcurrentDictionary<string, Lazy<ImageSource?>> Cache =
        new(StringComparer.OrdinalIgnoreCase);

    public static bool TryParse(string? displayIcon, out string path, out int iconIndex)
    {
        path = string.Empty;
        iconIndex = 0;
        string text = Environment.ExpandEnvironmentVariables(displayIcon?.Trim() ?? string.Empty);
        if (text.StartsWith('@'))
        {
            // Some shell display-icon values use an indirect-string marker.
            text = text[1..].TrimStart();
        }

        if (text.Length == 0)
        {
            return false;
        }

        if (text[0] == '"')
        {
            int closingQuote = text.IndexOf('"', 1);
            if (closingQuote <= 1)
            {
                return false;
            }

            path = text[1..closingQuote].Trim();
            string suffix = text[(closingQuote + 1)..].Trim();
            if (suffix.Length > 0)
            {
                if (suffix[0] != ',')
                {
                    return false;
                }

                string indexText = suffix[1..].Trim();
                if (indexText.Length > 0 &&
                    !int.TryParse(indexText, NumberStyles.Integer, CultureInfo.InvariantCulture, out iconIndex))
                {
                    return false;
                }
            }
        }
        else
        {
            int comma = text.LastIndexOf(',');
            if (comma < 0)
            {
                path = text.Trim();
            }
            else
            {
                string indexText = text[(comma + 1)..].Trim();
                if (indexText.Length == 0)
                {
                    path = text[..comma].Trim();
                }
                else if (int.TryParse(indexText, NumberStyles.Integer, CultureInfo.InvariantCulture, out iconIndex))
                {
                    path = text[..comma].Trim();
                }
                else
                {
                    // A comma can be part of a legitimate path when there is no index suffix.
                    path = text.Trim();
                }
            }
        }

        path = path.Trim().Trim('"');
        return path.Length > 0;
    }

    public static ImageSource? GetIcon(string? displayIcon)
    {
        if (!TryParse(displayIcon, out string path, out int iconIndex))
        {
            return null;
        }

        if (Uri.TryCreate(path, UriKind.Absolute, out Uri? fileUri) && fileUri.IsFile)
        {
            path = fileUri.LocalPath;
        }

        string key = path + "\u001f" + iconIndex.ToString(CultureInfo.InvariantCulture);
        var lazy = Cache.GetOrAdd(
            key,
            _ => new Lazy<ImageSource?>(
                () => ExtractIcon(path, iconIndex),
                LazyThreadSafetyMode.ExecutionAndPublication));
        return lazy.Value;
    }

    private static ImageSource? ExtractIcon(string path, int iconIndex)
    {
        try
        {
            if (!File.Exists(path))
            {
                return null;
            }

            if (IsRasterImage(path))
            {
                return LoadRasterImage(path);
            }

            IntPtr handle = ExtractHandle(path, iconIndex);
            if (handle == IntPtr.Zero && iconIndex != 0)
            {
                handle = ExtractHandle(path, 0);
            }

            if (handle == IntPtr.Zero)
            {
                return null;
            }

            try
            {
                var source = Imaging.CreateBitmapSourceFromHIcon(
                    handle,
                    Int32Rect.Empty,
                    BitmapSizeOptions.FromWidthAndHeight(IconSize, IconSize));
                source.Freeze();
                return source;
            }
            finally
            {
                DestroyIcon(handle);
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
            or ArgumentException or NotSupportedException or ExternalException)
        {
            return null;
        }
    }

    private static IntPtr ExtractHandle(string path, int iconIndex)
    {
        var icons = new IntPtr[1];
        var identifiers = new uint[1];
        uint extracted = PrivateExtractIcons(
            path,
            iconIndex,
            IconSize,
            IconSize,
            icons,
            identifiers,
            1,
            0);
        return extracted > 0 ? icons[0] : IntPtr.Zero;
    }

    private static bool IsRasterImage(string path) => Path.GetExtension(path).ToLowerInvariant() switch
    {
        ".png" or ".jpg" or ".jpeg" or ".bmp" or ".gif" => true,
        _ => false
    };

    private static ImageSource LoadRasterImage(string path)
    {
        var image = new BitmapImage();
        using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            image.BeginInit();
            image.CacheOption = BitmapCacheOption.OnLoad;
            image.DecodePixelWidth = IconSize;
            image.StreamSource = stream;
            image.EndInit();
        }

        image.Freeze();
        return image;
    }

    [DllImport("user32.dll", EntryPoint = "PrivateExtractIconsW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint PrivateExtractIcons(
        string fileName,
        int iconIndex,
        int iconWidth,
        int iconHeight,
        [Out] IntPtr[] icons,
        [Out] uint[] iconIdentifiers,
        uint iconCount,
        uint flags);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DestroyIcon(IntPtr icon);
}
