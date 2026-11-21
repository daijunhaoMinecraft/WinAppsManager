using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
using System.Threading;

namespace WinAppsManager.Services;

/// <summary>
/// 在任务计划程序（<c>taskschd.msc</c>）里展开到指定任务所在的文件夹，并选中任务行。
/// <para>
/// 任务计划程序的左侧目录树是 MMC 里的 <c>SysTreeView32</c>：它没有 UI Automation 提供程序，
/// <c>AutomationElement</c> 在这棵树上只能拿到一个空的 Pane（既没有 Tree 也没有 TreeItem），
/// 所以这里走 MSAA（<c>oleacc.dll</c>）：用 <c>get_accName</c> / <c>get_accState</c> 读节点，
/// 用 <c>accSelect</c> 选中节点、<c>accDoDefaultAction</c> 展开节点。
/// </para>
/// <para>
/// MMC 的树只显示文件夹，任务本身是选中文件夹后中间列表（<c>SysListView32</c>）里的行，
/// 因此定位目标是“任务所在文件夹”，随后在列表里按名字选中任务行。
/// </para>
/// </summary>
internal static class TaskSchedulerNavigator
{
    /// <summary>定位结果，页面据此决定提示文案。</summary>
    public enum NavigationResult
    {
        /// <summary>已展开到任务所在文件夹，任务出现在中间列表里。</summary>
        FolderSelected,

        /// <summary>已展开到文件夹，并在中间列表里选中了任务行。</summary>
        TaskSelected,

        /// <summary>找不到任务计划程序窗口（还没启动完或被最小化）。</summary>
        WindowNotFound,

        /// <summary>找到 MMC 窗口，但左侧目录树不可访问。</summary>
        TreeNotFound,

        /// <summary>任务计划程序里没有该任务所在的文件夹（任务可能已被删除）。</summary>
        FolderNotFound
    }

    private const string TreeWindowClass = "SysTreeView32";
    private const string MainWindowClass = "MMCMainFrame";
    private const string ListWindowClass = "SysListView32";

    private const int SelFlagTakeFocus = 0x1;
    private const int SelFlagTakeSelection = 0x2;

    private const int VkHome = 0x24;
    private const int VkDown = 0x28;
    private const int WmKeyDown = 0x0100;
    private const int WmKeyUp = 0x0101;
    private const uint SmtoBlock = 0x0001;
    private const uint SmtoAbortIfHung = 0x0002;

    // MSAA 状态位（oleacc.h / WinUser.h）
    private const int StateSelected = 0x00000002;
    private const int StateExpanded = 0x00000200;

    private const int SwRestore = 9;
    private const int SwShow = 5;

    private const int ObjidClient = unchecked((int)0xFFFFFFFC);

    private static readonly Guid AccessibleIid = new("618736E0-3C3D-11CF-810C-00AA00389B71");

    /// <summary>“任务计划程序库”节点名；其他语言的系统退化为“根节点后面的第一个节点”。</summary>
    private static readonly string[] LibraryNodeNames = ["任务计划程序库", "Task Scheduler Library"];

    private static readonly string[] WindowTitleKeywords = ["任务计划程序", "Task Scheduler"];


    /// <summary>
    /// 展开到 <paramref name="taskPath"/>（形如 <c>\ASUS\P508PowerAgent_sdk</c>）所在的文件夹。
    /// </summary>
    /// <param name="taskPath">任务在任务计划程序里的完整路径。</param>
    /// <param name="preferredProcessId">刚刚启动的 MMC 进程 ID；0 表示不指定。</param>
    /// <param name="detail">失败时给出可读的原因（或没能到达的层级）。</param>
    public static NavigationResult TryNavigate(string taskPath, int preferredProcessId, out string detail)
    {
        detail = string.Empty;
        string[] segments = taskPath.Split('\\', StringSplitOptions.RemoveEmptyEntries);
        if (segments.Length == 0)
        {
            detail = "任务路径为空。";
            return NavigationResult.FolderNotFound;
        }

        string taskName = segments[^1];
        string[] folders = segments[..^1];

        TreeSession? session = WaitForTreeSession(preferredProcessId, TimeSpan.FromSeconds(8));
        if (session is null)
        {
            detail = "找不到任务计划程序窗口。";
            return NavigationResult.WindowNotFound;
        }

        try
        {
            ActivateWindow(session.Window);

            // 1. 先选中“任务计划程序库”并展开，后面的文件夹节点才会出现在树里。
            List<AccessibleNode> nodes = ReadNodes(session.Accessible);
            AccessibleNode? library = FindLibraryNode(nodes);
            if (library is null)
            {
                detail = "左侧目录树里找不到“任务计划程序库”。";
                return NavigationResult.TreeNotFound;
            }

            if (!SelectNode(session, library.Value))
            {
                detail = "无法选中“任务计划程序库”。";
                return NavigationResult.TreeNotFound;
            }

            ExpandNode(session, library.Value);

            // 2. 逐级展开文件夹：中间层级要展开才能看到下一级，最后一级只需要选中。
            AccessibleNode current = library.Value;
            for (int level = 0; level < folders.Length; level++)
            {
                AccessibleNode? next = WaitForChildNode(session, current.Id, folders[level]);
                if (next is null || !SelectNode(session, next.Value))
                {
                    detail = "\\" + string.Join('\\', folders[..(level + 1)]);
                    return NavigationResult.FolderNotFound;
                }

                current = next.Value;
                if (level < folders.Length - 1)
                {
                    ExpandNode(session, current);
                }
            }

            // 3. 任务行在中间的列表里；能选中就顺手选中，选不中也已经展开到位。
            return SelectTaskRow(session.Window, taskName)
                ? NavigationResult.TaskSelected
                : NavigationResult.FolderSelected;
        }
        finally
        {
            session.Release();
        }
    }

    #region MMC 窗口与目录树

    private sealed class TreeSession
    {
        public IntPtr Window;

        public IntPtr Tree;

        public IAccessible Accessible = null!;

        public void Release()
        {
            if (Accessible is not null && Marshal.IsComObject(Accessible))
            {
                Marshal.ReleaseComObject(Accessible);
            }

            Accessible = null!;
        }
    }

    private static TreeSession? WaitForTreeSession(int preferredProcessId, TimeSpan timeout)
    {
        DateTime deadline = DateTime.UtcNow + timeout;
        bool waitForPreferred = preferredProcessId != 0;
        do
        {
            List<(IntPtr Window, int ProcessId)> candidates = EnumerateTaskSchedulerWindows();

            // 刚启动的那个 MMC 进程的窗口优先：任务计划程序每启动一次都会新开一个 MMC 窗口，
            // 先连旧窗口会让用户看到“选定成功”却落在另一个窗口里。进程已经退出（例如
            // MMC 把请求转交给已有实例）时才退回到现有的窗口。
            if (waitForPreferred)
            {
                foreach ((IntPtr window, int processId) in candidates)
                {
                    if (processId == preferredProcessId && TryOpenTree(window, out TreeSession? session))
                    {
                        return session;
                    }
                }

                if (!IsProcessAlive(preferredProcessId))
                {
                    waitForPreferred = false;
                }
            }
            else
            {
                foreach ((IntPtr window, _) in candidates)
                {
                    if (TryOpenTree(window, out TreeSession? session))
                    {
                        return session;
                    }
                }
            }

            Thread.Sleep(200);
        }
        while (DateTime.UtcNow < deadline);

        // 等不到指定的 MMC（进程号拿不到、或被系统转交给别的实例）时，退回到任意一个任务计划程序窗口。
        foreach ((IntPtr window, _) in EnumerateTaskSchedulerWindows())
        {
            if (TryOpenTree(window, out TreeSession? session))
            {
                return session;
            }
        }

        return null;
    }

    private static bool IsProcessAlive(int processId)
    {
        try
        {
            using Process process = Process.GetProcessById(processId);
            return !process.HasExited;
        }
        catch (Exception exception) when (exception is ArgumentException or InvalidOperationException)
        {
            return false;
        }
    }

    private static List<(IntPtr Window, int ProcessId)> EnumerateTaskSchedulerWindows()
    {
        var candidates = new List<(IntPtr Window, int ProcessId)>();
        EnumWindows((window, _) =>
        {
            if (IsWindowVisible(window) && ClassName(window) == MainWindowClass)
            {
                candidates.Add((window, ProcessIdOf(window)));
            }

            return true;
        }, IntPtr.Zero);

        return candidates;
    }

    private static bool TryOpenTree(IntPtr window, out TreeSession? session)
    {
        session = null;
        IntPtr tree = FindDescendant(window, TreeWindowClass);
        if (tree == IntPtr.Zero || !IsWindowVisible(tree))
        {
            return false;
        }

        IAccessible? accessible = GetAccessible(tree);
        if (accessible is null)
        {
            return false;
        }

        if (!LooksLikeTaskSchedulerTree(window, accessible))
        {
            Marshal.ReleaseComObject(accessible);
            return false;
        }

        session = new TreeSession { Window = window, Tree = tree, Accessible = accessible };
        return true;
    }

    private static bool LooksLikeTaskSchedulerTree(IntPtr window, IAccessible accessible)
    {
        List<AccessibleNode> nodes = ReadNodes(accessible);
        if (nodes.Count < 2)
        {
            return false;
        }

        // 目录树的第 2 个节点固定是“任务计划程序库”：中文 / 英文系统直接按名字确认，
        // 其他语言再看窗口标题，避免把事件查看器、服务等 MMC 控制台误认成任务计划程序。
        foreach (AccessibleNode node in nodes)
        {
            foreach (string name in LibraryNodeNames)
            {
                if (string.Equals(node.Name, name, StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
        }

        string title = WindowTitle(window);
        foreach (string keyword in WindowTitleKeywords)
        {
            if (title.Contains(keyword, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
        }

        return false;
    }

    private static AccessibleNode? FindLibraryNode(List<AccessibleNode> nodes)
    {
        foreach (AccessibleNode node in nodes)
        {
            foreach (string name in LibraryNodeNames)
            {
                if (string.Equals(node.Name, name, StringComparison.OrdinalIgnoreCase))
                {
                    return node;
                }
            }
        }

        // 根节点后面紧跟的就是库节点。
        if (nodes.Count >= 2)
        {
            return nodes[1];
        }

        return nodes.Count >= 1 ? nodes[0] : null;
    }

    /// <summary>在 <paramref name="afterId"/> 之后按名字找节点：展开后子节点紧跟在父节点后面。</summary>
    private static AccessibleNode? FindNodeAfter(List<AccessibleNode> nodes, int afterId, string name)
    {
        int start = 0;
        for (int i = 0; i < nodes.Count; i++)
        {
            if (nodes[i].Id == afterId)
            {
                start = i + 1;
                break;
            }
        }

        for (int i = start; i < nodes.Count; i++)
        {
            if (string.Equals(nodes[i].Name.Trim(), name.Trim(), StringComparison.OrdinalIgnoreCase))
            {
                return nodes[i];
            }
        }

        return null;
    }

    /// <summary>等父节点把子节点装载出来：选中或展开后 MMC 会异步填树。</summary>
    private static AccessibleNode? WaitForChildNode(TreeSession session, int afterId, string name)
    {
        DateTime deadline = DateTime.UtcNow.AddSeconds(2);
        while (true)
        {
            AccessibleNode? node = FindNodeAfter(ReadNodes(session.Accessible), afterId, name);
            if (node is not null || DateTime.UtcNow >= deadline)
            {
                return node;
            }

            Thread.Sleep(100);
        }
    }

    private static bool SelectNode(TreeSession session, AccessibleNode node)
    {
        int result = session.Accessible.accSelect(SelFlagTakeFocus | SelFlagTakeSelection, node.Id);
        if (result != 0)
        {
            return false;
        }

        // 选择是同步的，但 MMC 会在选中后才装载子节点，留一点时间让树刷新。
        Thread.Sleep(120);
        return true;
    }

    private static void ExpandNode(TreeSession session, AccessibleNode node)
    {
        if ((SafeState(session.Accessible, node.Id) & StateExpanded) != 0)
        {
            return;
        }

        // 目录树节点的默认动作就是“展开 / 折叠”，展开后再等 MMC 把子节点读出来。
        session.Accessible.accDoDefaultAction(node.Id);

        DateTime deadline = DateTime.UtcNow.AddSeconds(2);
        while (DateTime.UtcNow < deadline)
        {
            if ((SafeState(session.Accessible, node.Id) & StateExpanded) != 0)
            {
                return;
            }

            Thread.Sleep(100);
        }
    }

    #endregion

    #region 读取 MSAA 节点

    private readonly record struct AccessibleNode(int Id, string Name, int State)
    {
        public bool IsSelected => (State & StateSelected) != 0;

        public bool IsExpanded => (State & StateExpanded) != 0;
    }

    private static List<AccessibleNode> ReadNodes(IAccessible accessible)
    {
        var nodes = new List<AccessibleNode>();
        int count = ReadChildCount(accessible);
        if (count <= 0)
        {
            return nodes;
        }

        var children = new object[count];
        int obtained;
        try
        {
            if (AccessibleChildren(accessible, 0, count, children, out obtained) != 0)
            {
                return nodes;
            }
        }
        catch (COMException)
        {
            return nodes;
        }

        for (int i = 0; i < obtained; i++)
        {
            // 目录树 / 列表的条目通常以 childId 形式出现，个别提供程序会直接给出 IAccessible 对象。
            if (children[i] is int childId)
            {
                nodes.Add(new AccessibleNode(childId, SafeName(accessible, childId), SafeState(accessible, childId)));
            }
            else if (children[i] is IAccessible child)
            {
                nodes.Add(new AccessibleNode(0, SafeName(child, 0), SafeState(child, 0)));
            }
        }

        return nodes;
    }

    private static int ReadChildCount(IAccessible accessible)
    {
        try
        {
            return accessible.get_accChildCount(out int count) == 0 ? count : 0;
        }
        catch (Exception exception) when (exception is COMException or InvalidCastException)
        {
            return 0;
        }
    }

    private static string SafeName(IAccessible accessible, int childId)
    {
        try
        {
            return accessible.get_accName(childId, out string name) == 0 ? name ?? string.Empty : string.Empty;
        }
        catch (Exception exception) when (exception is COMException or InvalidCastException)
        {
            return string.Empty;
        }
    }

    private static int SafeState(IAccessible accessible, int childId)
    {
        try
        {
            if (accessible.get_accState(childId, out object state) != 0 || state is null)
            {
                return 0;
            }

            return Convert.ToInt32(state);
        }
        catch (Exception exception) when (exception is COMException or InvalidCastException)
        {
            return 0;
        }
    }

    #endregion

    #region 中间任务列表

    /// <summary>
    /// 在中间的“所选项”列表里选中任务行。列表是 MMC 里的 <c>SysListView32</c>，
    /// 任何一步失败都只返回 false，不影响已经展开到文件夹。
    /// </summary>
    private static bool SelectTaskRow(IntPtr window, string taskName)
    {
        // 切到文件夹后 MMC 会重建中间的视图并异步填表，所以要一边等一边重新找窗口：
        // 旧的 SysListView32 可能已经销毁，留着它的 IAccessible 永远读不到新行。
        DateTime deadline = DateTime.UtcNow.AddSeconds(2.5);
        while (DateTime.UtcNow < deadline)
        {
            foreach (IntPtr list in FindDescendants(window, ListWindowClass))
            {
                if (!IsWindowVisible(list))
                {
                    continue;
                }

                IAccessible? accessible = GetAccessible(list);
                if (accessible is null)
                {
                    continue;
                }

                try
                {
                    List<AccessibleNode> rows = ReadNodes(accessible);
                    int rowIndex = -1;
                    for (int i = 0; i < rows.Count; i++)
                    {
                        if (rows[i].Id != 0 && string.Equals(rows[i].Name.Trim(), taskName, StringComparison.OrdinalIgnoreCase))
                        {
                            rowIndex = i;
                            break;
                        }
                    }

                    if (rowIndex < 0)
                    {
                        continue;
                    }

                    // 列表的 WinForms 提供程序对 accSelect 只返回 S_OK 而不真正改变选择，
                    // 所以这里聚焦列表后用方向键选中目标行（行序与 MSAA 枚举顺序一致）。
                    FocusChild(window, list);
                    PressKey(list, VkHome);
                    Thread.Sleep(60);
                    for (int i = 0; i < rowIndex; i++)
                    {
                        PressKey(list, VkDown);
                        Thread.Sleep(25);
                    }

                    Thread.Sleep(120);
                    return (SafeState(accessible, rows[rowIndex].Id) & StateSelected) != 0;
                }
                finally
                {
                    Release(accessible);
                }
            }

            Thread.Sleep(150);
        }

        return false;
    }

    #endregion

    #region 窗口与互操作

    private static void FocusChild(IntPtr window, IntPtr child)
    {
        // 跨进程 SetFocus 必须先附加输入队列，否则焦点还在原窗口上，方向键会被忽略。
        uint targetThread = GetWindowThreadProcessId(window, out _);
        uint currentThread = GetCurrentThreadId();
        bool attached = targetThread != currentThread && AttachThreadInput(currentThread, targetThread, true);
        try
        {
            SetFocus(child);
        }
        finally
        {
            if (attached)
            {
                AttachThreadInput(currentThread, targetThread, false);
            }
        }
    }

    private static void PressKey(IntPtr window, int virtualKey)
    {
        SendMessageTimeout(window, WmKeyDown, (IntPtr)virtualKey, IntPtr.Zero, SmtoBlock | SmtoAbortIfHung, 2000, out _);
        SendMessageTimeout(window, WmKeyUp, (IntPtr)virtualKey, IntPtr.Zero, SmtoBlock | SmtoAbortIfHung, 2000, out _);
    }

    private static void ActivateWindow(IntPtr window)
    {
        if (IsIconic(window))
        {
            ShowWindow(window, SwRestore);
        }

        ShowWindow(window, SwShow);
        SetForegroundWindow(window);
    }

    private static void Release(IAccessible? accessible)
    {
        if (accessible is not null && Marshal.IsComObject(accessible))
        {
            Marshal.ReleaseComObject(accessible);
        }
    }

    private delegate bool EnumWindowsProc(IntPtr window, IntPtr parameter);

    /// <summary>
    /// IAccessible 的完整声明：先补齐 IDispatch 的 4 个槽位，再按 oleacc.h 的顺序列出全部成员，
    /// vtable 槽位必须与原生接口一致。声明成“只列出用到的方法”的迷你接口会让 get_accState 拿到
    /// <c>null</c>（状态位全 0），accSelect 甚至会访问越界——不要删减成员，也不要改回 InterfaceIsDual。
    /// </summary>
    [ComImport]
    [Guid("618736E0-3C3D-11CF-810C-00AA00389B71")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IAccessible
    {
        [PreserveSig] int GetTypeInfoCount(out uint infoCount);

        [PreserveSig] int GetTypeInfo(uint typeInfo, uint locale, out IntPtr typeInfoPointer);

        [PreserveSig] int GetIDsOfNames(
            ref Guid riid,
            [MarshalAs(UnmanagedType.LPArray, ArraySubType = UnmanagedType.LPWStr)] string[] names,
            uint nameCount,
            uint locale,
            [MarshalAs(UnmanagedType.LPArray)] int[] dispatchIds);

        [PreserveSig] int Invoke(
            int dispatchId,
            ref Guid riid,
            uint locale,
            ushort flags,
            ref DISPPARAMS parameters,
            [MarshalAs(UnmanagedType.Struct)] out object result,
            ref EXCEPINFO exceptionInfo,
            out uint argumentError);

        [PreserveSig] int get_accParent(out IntPtr parent);

        [PreserveSig] int get_accChildCount(out int count);

        [PreserveSig] int get_accChild([MarshalAs(UnmanagedType.Struct)] object varChild, out IntPtr child);

        [PreserveSig] int get_accName([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.BStr)] out string name);

        [PreserveSig] int get_accValue([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.BStr)] out string value);

        [PreserveSig] int get_accDescription([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.BStr)] out string description);

        [PreserveSig] int get_accRole([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.Struct)] out object role);

        [PreserveSig] int get_accState([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.Struct)] out object state);

        [PreserveSig] int get_accHelp([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.BStr)] out string help);

        [PreserveSig] int get_accHelpTopic([MarshalAs(UnmanagedType.BStr)] out string helpFile, [MarshalAs(UnmanagedType.Struct)] object varChild, out int helpTopic);

        [PreserveSig] int get_accKeyboardShortcut([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.BStr)] out string shortcut);

        [PreserveSig] int get_accFocus([MarshalAs(UnmanagedType.Struct)] out object focus);

        [PreserveSig] int get_accSelection([MarshalAs(UnmanagedType.Struct)] out object selection);

        [PreserveSig] int get_accDefaultAction([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.BStr)] out string action);

        [PreserveSig] int accSelect(int flagsSelect, [MarshalAs(UnmanagedType.Struct)] object varChild);

        [PreserveSig] int accLocation(out int left, out int top, out int width, out int height, [MarshalAs(UnmanagedType.Struct)] object varChild);

        [PreserveSig] int accNavigate(int direction, [MarshalAs(UnmanagedType.Struct)] object start, [MarshalAs(UnmanagedType.Struct)] out object end);

        [PreserveSig] int accHitTest(int x, int y, [MarshalAs(UnmanagedType.Struct)] out object hit);

        [PreserveSig] int accDoDefaultAction([MarshalAs(UnmanagedType.Struct)] object varChild);

        [PreserveSig] int put_accName([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.BStr)] string name);

        [PreserveSig] int put_accValue([MarshalAs(UnmanagedType.Struct)] object varChild, [MarshalAs(UnmanagedType.BStr)] string value);
    }

    [DllImport("oleacc.dll")]
    private static extern int AccessibleObjectFromWindow(
        IntPtr hwnd,
        uint objectId,
        ref Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out object accessibleObject);

    [DllImport("oleacc.dll")]
    private static extern int AccessibleChildren(
        IAccessible accessible,
        int startIndex,
        int count,
        [Out] object[] children,
        out int obtained);

    [DllImport("user32.dll")]
    private static extern bool EnumWindows(EnumWindowsProc callback, IntPtr parameter);

    [DllImport("user32.dll")]
    private static extern bool EnumChildWindows(IntPtr parent, EnumWindowsProc callback, IntPtr parameter);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassName(IntPtr window, StringBuilder className, int capacity);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetWindowText(IntPtr window, StringBuilder text, int capacity);

    [DllImport("user32.dll")]
    private static extern bool IsWindowVisible(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool IsIconic(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr window, int command);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr window);

    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

    [DllImport("user32.dll")]
    private static extern IntPtr SetFocus(IntPtr window);

    [DllImport("user32.dll")]
    private static extern bool AttachThreadInput(uint attach, uint attachTo, bool doAttach);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll")]
    private static extern IntPtr SendMessageTimeout(
        IntPtr window,
        int message,
        IntPtr wParam,
        IntPtr lParam,
        uint flags,
        uint timeout,
        out IntPtr result);

    private static string ClassName(IntPtr window)
    {
        var buffer = new StringBuilder(64);
        return GetClassName(window, buffer, buffer.Capacity) > 0 ? buffer.ToString() : string.Empty;
    }

    private static string WindowTitle(IntPtr window)
    {
        var buffer = new StringBuilder(256);
        return GetWindowText(window, buffer, buffer.Capacity) > 0 ? buffer.ToString() : string.Empty;
    }

    private static int ProcessIdOf(IntPtr window)
    {
        GetWindowThreadProcessId(window, out uint processId);
        return unchecked((int)processId);
    }

    /// <summary>按窗口类名找后代窗口；类名可能是 <c>WindowsForms10.SysListView32.app...</c> 这类前缀形式。</summary>
    private static IntPtr FindDescendant(IntPtr parent, string classFragment)
    {
        IntPtr found = IntPtr.Zero;
        EnumChildWindows(parent, (child, _) =>
        {
            if (!ClassName(child).Contains(classFragment, StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }

            found = child;
            return false;
        }, IntPtr.Zero);

        return found;
    }

    /// <summary>按窗口类名找全部后代窗口（MMC 的视图会在切换节点时重建，可能同时存在多个）。</summary>
    private static List<IntPtr> FindDescendants(IntPtr parent, string classFragment)
    {
        var found = new List<IntPtr>();
        EnumChildWindows(parent, (child, _) =>
        {
            if (ClassName(child).Contains(classFragment, StringComparison.OrdinalIgnoreCase))
            {
                found.Add(child);
            }

            return true;
        }, IntPtr.Zero);

        return found;
    }

    private static IAccessible? GetAccessible(IntPtr window)
    {
        Guid iid = AccessibleIid;
        try
        {
            if (AccessibleObjectFromWindow(window, unchecked((uint)ObjidClient), ref iid, out object accessible) != 0)
            {
                return null;
            }

            return accessible as IAccessible;
        }
        catch (COMException)
        {
            return null;
        }
    }

    #endregion
}

