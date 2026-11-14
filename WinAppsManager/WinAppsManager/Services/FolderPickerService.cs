using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;

namespace WinAppsManager.Services;

/// <summary>
/// 调用 Windows 原生的“选择文件夹”对话框（<c>IFileOpenDialog</c> + <c>FOS_PICKFOLDERS</c>）。
/// 不依赖 WinForms，net6（Windows 7 SP1）与 net8 双目标都可用。
/// </summary>
internal static class FolderPickerService
{
    private const uint FosPickFolders = 0x00000020;
    private const uint FosForceFileSystem = 0x00000040;
    private const uint FosPathMustExist = 0x00000800;
    private const uint SigdnFileSystemPath = 0x80058000;

    /// <summary>
    /// 显示文件夹选择对话框；用户取消或对话框不可用时返回 <see langword="null"/>。
    /// </summary>
    public static string? PickFolder(string? initialDirectory, string title)
    {
        if (!OperatingSystem.IsWindowsVersionAtLeast(6, 0))
        {
            return null;
        }

        IFileOpenDialog? dialog = null;
        try
        {
            // ComImport coclass 与接口没有编译期继承关系，经 object 转换让 CLR 走 QueryInterface。
            dialog = (IFileOpenDialog)(object)new FileOpenDialog();
            dialog.GetOptions(out uint options);
            dialog.SetOptions(options | FosPickFolders | FosForceFileSystem | FosPathMustExist);
            dialog.SetTitle(title);

            if (!string.IsNullOrWhiteSpace(initialDirectory) && Directory.Exists(initialDirectory)
                && TryCreateShellItem(initialDirectory, out var initialItem))
            {
                try
                {
                    dialog.SetFolder(initialItem!);
                }
                finally
                {
                    Marshal.ReleaseComObject(initialItem!);
                }
            }

            // 0 = 用户确认；取消或出错时返回非 0 HRESULT，这里统一当作“没有选择”。
            if (dialog.Show(GetOwnerHandle()) != 0)
            {
                return null;
            }

            dialog.GetResult(out var resultItem);
            try
            {
                resultItem.GetDisplayName(SigdnFileSystemPath, out IntPtr buffer);
                try
                {
                    return Marshal.PtrToStringUni(buffer);
                }
                finally
                {
                    if (buffer != IntPtr.Zero) Marshal.FreeCoTaskMem(buffer);
                }
            }
            finally
            {
                Marshal.ReleaseComObject(resultItem);
            }
        }
        catch (COMException)
        {
            return null;
        }
        catch (InvalidCastException)
        {
            return null;
        }
        finally
        {
            if (dialog is not null) Marshal.ReleaseComObject(dialog);
        }
    }

    private static IntPtr GetOwnerHandle()
    {
        Window? window = Application.Current?.MainWindow;
        if (window is null) return IntPtr.Zero;
        try
        {
            return new WindowInteropHelper(window).EnsureHandle();
        }
        catch (InvalidOperationException)
        {
            return IntPtr.Zero;
        }
    }

    private static bool TryCreateShellItem(string path, out IShellItem? item)
    {
        item = null;
        try
        {
            Guid iid = typeof(IShellItem).GUID;
            SHCreateItemFromParsingName(path, IntPtr.Zero, ref iid, out IShellItem? created);
            item = created;
            return created is not null;
        }
        catch (COMException)
        {
            return false;
        }
    }

    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void SHCreateItemFromParsingName(
        [MarshalAs(UnmanagedType.LPWStr)] string pszPath,
        IntPtr pbc,
        ref Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out IShellItem? ppv);

    [ComImport]
    [Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
    private sealed class FileOpenDialog
    {
    }

    [ComImport]
    [Guid("42f85136-db7e-439c-85f1-e4075d135fc8")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileDialog
    {
        [PreserveSig] int Show(IntPtr parent);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex(uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOptions(uint fos);
        void GetOptions(out uint pfos);
        void SetDefaultFolder(IShellItem psi);
        void SetFolder(IShellItem psi);
        void GetFolder(out IShellItem ppsi);
        void GetCurrentSelection(out IShellItem ppsi);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
        void GetFileName(out IntPtr pszName);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string pszText);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
        void GetResult(out IShellItem ppsi);
        void AddPlace(IShellItem psi, int fdap);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
        void Close([MarshalAs(UnmanagedType.Error)] int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr pFilter);
    }

    [ComImport]
    [Guid("d57c7288-d4ad-4768-be02-9d969532d960")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileOpenDialog : IFileDialog
    {
        void GetResults(out IntPtr ppenum);
        void GetSelectedItems(out IntPtr ppsai);
    }

    [ComImport]
    [Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItem
    {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint sigdnName, out IntPtr ppszName);
        void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItem psi, uint hint, out int piOrder);
    }
}
