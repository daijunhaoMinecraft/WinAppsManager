using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Fills in the metadata the uninstall key does not have from the file system: the install folder
/// is measured recursively, and the install date falls back to the earlier of the uninstall key's
/// last write time (<c>ZwQueryKey</c>) and the install folder's last write time.
/// </summary>
public static partial class InstalledApplicationEnricher
{
    /// <summary>Hard caps so a huge or slow folder cannot stall the background loader.</summary>
    private const int MaxEntries = 8000;

    private static readonly TimeSpan MeasureBudget = TimeSpan.FromMilliseconds(400);

    /// <summary>Measures the folder, skipping work the caller already has.</summary>
    public static (long? SizeBytes, DateOnly? InstallDate) Probe(
        InstalledApplication application,
        bool needSize,
        bool needDate,
        CancellationToken cancellationToken = default)
    {
        // The size is only measured on the declared install location: a folder guessed from
        // DisplayIcon may be a shared package cache, and its total would be misleading.
        long? size = needSize ? MeasureFolder(application.InstallLocation, cancellationToken) : null;

        DateOnly? date = null;
        if (needDate)
        {
            // The date starts from the uninstall key's own last write time (ZwQueryKey is used when
            // the key has no usable FILETIME) and picks the earliest of that and the install
            // folder's last write time. A later update of the folder therefore keeps the older,
            // install-looking date, and an application without InstallLocation still gets the key
            // time instead of an empty cell.
            date = Earliest(ReadRegistryKeyDate(application), ReadInstallFolderDate(application));
        }

        return (size, date);
    }

    /// <summary>Earlier of the two dates; the one that exists when the other is missing.</summary>
    private static DateOnly? Earliest(DateOnly? left, DateOnly? right)
        => left is { } first && right is { } second
            ? (first <= second ? first : second)
            : left ?? right;

    /// <summary>Last write time of the application's install folder, from any resolved candidate.</summary>
    private static DateOnly? ReadInstallFolderDate(InstalledApplication application)
        => ReadFolderDate(InstallLocationResolver.FindExistingFolder(application));

    private static DateOnly? ReadFolderDate(string? folder)
    {
        if (string.IsNullOrWhiteSpace(folder) || !Directory.Exists(folder))
        {
            return null;
        }

        try
        {
            return DateOnly.FromDateTime(Directory.GetLastWriteTime(folder));
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or System.Security.SecurityException or ArgumentException)
        {
            return null;
        }
    }

    /// <summary>
    /// Last write time of the uninstall registry key — read through
    /// <c>ZwQueryKey</c>/<c>KEY_BASIC_INFORMATION</c>; the "install date" starts from it.
    /// </summary>
    public static DateOnly? ReadRegistryKeyDate(InstalledApplication application)
    {
        if (!application.HasRegistryEntry)
        {
            return null;
        }

        try
        {
            using var baseKey = RegistryKey.OpenBaseKey(application.RegistryHive, application.RegistryView);
            using var key = baseKey.OpenSubKey(application.RegistryPath);
            if (key is null)
            {
                return null;
            }

            int result = RegQueryInfoKey(
                key.Handle.DangerousGetHandle(), IntPtr.Zero, IntPtr.Zero, IntPtr.Zero,
                IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero,
                IntPtr.Zero, out FileTime lastWrite);
            if (result != 0 || (lastWrite.Low == 0 && lastWrite.High == 0))
            {
                return null;
            }

            long fileTime = ((long)lastWrite.High << 32) | lastWrite.Low;
            return fileTime <= 0 ? null : DateOnly.FromDateTime(DateTime.FromFileTime(fileTime));
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or System.Security.SecurityException or ArgumentException
                                              or InvalidOperationException)
        {
            return null;
        }
    }

    /// <summary>
    /// Recursive size of a folder in bytes using <c>FindFirstFileExW</c> / <c>FindNextFileW</c>.
    /// The directory enumeration already carries each file size, so this avoids the per-file
    /// <c>NtQueryFullAttributesFile</c> round trip that <c>FileInfo.Length</c> costs.
    /// </summary>
    public static long? MeasureFolder(string folder, CancellationToken cancellationToken = default)
    {
        if (string.IsNullOrWhiteSpace(folder) || !Directory.Exists(folder))
        {
            return null;
        }

        var budget = Stopwatch.StartNew();
        long total = 0;
        int visited = 0;
        var pending = new Stack<string>();
        pending.Push(folder);
        while (pending.Count > 0)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (visited >= MaxEntries || budget.Elapsed >= MeasureBudget)
            {
                break;
            }

            string current = pending.Pop();
            IntPtr handle = FindFirstFileEx(
                EnsureTrailingSeparator(current) + "*",
                FindExInfoBasic,
                out Win32FindData entry,
                FindExSearchNameMatch,
                IntPtr.Zero,
                FindFirstExLargeFetch);
            if (handle == InvalidHandleValue)
            {
                continue;
            }

            try
            {
                do
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    string name = entry.FileName;
                    if (name is "." or "..")
                    {
                        continue;
                    }

                    if ((entry.FileAttributes & FileAttributeDirectory) != 0)
                    {
                        // Reparse points are skipped so a directory loop cannot explode the walk.
                        if ((entry.FileAttributes & FileAttributeReparsePoint) == 0)
                        {
                            pending.Push(Path.Combine(current, name));
                        }
                    }
                    else
                    {
                        total += ((long)entry.FileSizeHigh << 32) | entry.FileSizeLow;
                    }

                    if (++visited >= MaxEntries)
                    {
                        break;
                    }

                    if ((visited & 63) == 0 && budget.Elapsed >= MeasureBudget)
                    {
                        break;
                    }
                }
                while (FindNextFile(handle, out entry));
            }
            finally
            {
                FindClose(handle);
            }
        }

        return total > 0 ? total : null;
    }

    private static string EnsureTrailingSeparator(string path)
        => path.EndsWith(Path.DirectorySeparatorChar) ? path : path + Path.DirectorySeparatorChar;

    private const int FindExInfoBasic = 1;
    private const int FindExSearchNameMatch = 0;
    private const int FindFirstExLargeFetch = 2;
    private const uint FileAttributeDirectory = 0x10;
    private const uint FileAttributeReparsePoint = 0x400;
    private static readonly IntPtr InvalidHandleValue = new(-1);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct Win32FindData
    {
        public uint FileAttributes;
        public FileTime CreationTime;
        public FileTime LastAccessTime;
        public FileTime LastWriteTime;
        public uint FileSizeHigh;
        public uint FileSizeLow;
        public uint Reserved0;
        public uint Reserved1;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string FileName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 14)]
        public string AlternateFileName;
    }

    [DllImport("kernel32.dll", EntryPoint = "FindFirstFileExW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr FindFirstFileEx(
        string fileName,
        int infoLevel,
        out Win32FindData findFileData,
        int searchOp,
        IntPtr searchFilter,
        int additionalFlags);

    [DllImport("kernel32.dll", EntryPoint = "FindNextFileW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool FindNextFile(IntPtr findHandle, out Win32FindData findFileData);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool FindClose(IntPtr findHandle);
    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime
    {
        public uint Low;
        public uint High;
    }

    [DllImport("advapi32.dll", EntryPoint = "RegQueryInfoKeyW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern int RegQueryInfoKey(
        IntPtr key,
        IntPtr className,
        IntPtr classNameLength,
        IntPtr reserved,
        IntPtr subKeyCount,
        IntPtr maxSubKeyLength,
        IntPtr maxClassLength,
        IntPtr valueCount,
        IntPtr maxValueNameLength,
        IntPtr maxValueLength,
        IntPtr securityDescriptorLength,
        out FileTime lastWriteTime);
}
