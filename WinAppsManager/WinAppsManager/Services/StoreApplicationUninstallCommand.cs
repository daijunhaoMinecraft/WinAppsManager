using System.Text;

namespace WinAppsManager.Services;

/// <summary>
/// Builds a current-user Appx removal command for one exact package full name. A package can
/// declare several application entries, but Remove-AppxPackage removes the containing package.
/// </summary>
public static class StoreApplicationUninstallCommand
{
    public static string Build(string packageName, string packageFullName)
    {
        if (string.IsNullOrWhiteSpace(packageName))
        {
            throw new ArgumentException("包名称不能为空。", nameof(packageName));
        }

        if (packageName.Any(char.IsControl) || packageName.Contains('"') || packageName.Contains('\''))
        {
            throw new ArgumentException("包名称包含无效字符。", nameof(packageName));
        }

        if (string.IsNullOrWhiteSpace(packageFullName) ||
            packageFullName.Any(character => !char.IsLetterOrDigit(character) && character is not '.' and not '_' and not '-'))
        {
            throw new ArgumentException("包全名包含无效字符。", nameof(packageFullName));
        }

        var command = new StringBuilder();
        command.Append("powershell.exe -NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command \"");
        command.Append("try { Get-AppxPackage -Name '");
        command.Append(packageName);
        command.Append("' -ErrorAction Stop | Where-Object { $_.PackageFullName -ieq '");
        command.Append(packageFullName);
        command.Append("' } | Remove-AppxPackage -Confirm:$false -ErrorAction Stop } ");
        command.Append("catch { Write-Error $_.Exception.Message; exit 1 }\"");
        return command.ToString();
    }
}
