using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;

namespace WinAppsManager.Services;

/// <summary>
/// Resolves <c>.lnk</c> targets so shortcuts can be matched by what they point at instead of by
/// file name (through IShellLink). WScript.Shell is apartment
/// threaded, so all resolutions run on one dedicated STA thread.
/// </summary>
public static class ShortcutResolver
{
    private static readonly TimeSpan ResolveTimeout = TimeSpan.FromSeconds(10);

    public static string? Resolve(string shortcutPath)
        => ResolveMany([shortcutPath]).TryGetValue(shortcutPath, out string? target) ? target : null;

    /// <summary>Resolves several shortcuts in one STA thread; missing or broken links are skipped.</summary>
    public static IReadOnlyDictionary<string, string> ResolveMany(IEnumerable<string> shortcutPaths)
    {
        ArgumentNullException.ThrowIfNull(shortcutPaths);
        string[] paths = shortcutPaths
            .Where(path => !string.IsNullOrWhiteSpace(path))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        var results = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (paths.Length == 0)
        {
            return results;
        }

        var thread = new Thread(() => ResolveCore(paths, results))
        {
            IsBackground = true,
            Name = "Wam.ShrotcutResolver"
        };
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        if (!thread.Join(ResolveTimeout))
        {
            return results;
        }

        return results;
    }

    private static void ResolveCore(IReadOnlyList<string> paths, IDictionary<string, string> results)
    {
        Type? shellType = Type.GetTypeFromProgID("WScript.Shell");
        if (shellType is null)
        {
            return;
        }

        object? shell = null;
        try
        {
            shell = Activator.CreateInstance(shellType);
            if (shell is null)
            {
                return;
            }

            dynamic wsh = shell;
            foreach (string path in paths)
            {
                object? link = null;
                try
                {
                    if (!File.Exists(path))
                    {
                        continue;
                    }

                    link = wsh.CreateShortcut(path);
                    dynamic shortcut = link;
                    string target = (string)shortcut.TargetPath;
                    target = Environment.ExpandEnvironmentVariables(target?.Trim() ?? string.Empty).Trim('"');
                    if (target.Length > 0)
                    {
                        results[path] = target;
                    }
                }
                catch (Exception exception) when (exception is COMException or ArgumentException
                                                      or InvalidCastException or Microsoft.CSharp.RuntimeBinder.RuntimeBinderException)
                {
                    Debug.WriteLine(exception);
                }
                finally
                {
                    Release(link);
                }
            }
        }
        catch (Exception exception) when (exception is COMException or ArgumentException
                                              or InvalidCastException or Microsoft.CSharp.RuntimeBinder.RuntimeBinderException)
        {
            Debug.WriteLine(exception);
        }
        finally
        {
            Release(shell);
        }
    }

    private static void Release(object? instance)
    {
        if (instance is not null && Marshal.IsComObject(instance))
        {
            Marshal.FinalReleaseComObject(instance);
        }
    }
}
