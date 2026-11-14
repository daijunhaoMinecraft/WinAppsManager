using System.Globalization;

namespace WinAppsManager.Models;

public sealed record CategoryOption(string Name, int Count)
{
    public string CountText => Count.ToString(CultureInfo.CurrentCulture);
}
