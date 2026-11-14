using System.Text.RegularExpressions;

namespace WinAppsManager.Services;

/// <summary>
/// Splits a Windows uninstall command line into the executable and its arguments.
/// ShellExecute needs these separately; passing the complete command line as FileName
/// fails for the common <c>"C:\Program Files\App\uninstall.exe" /S</c> form.
/// </summary>
public static partial class UninstallCommandParser
{
    private static readonly Regex ExecutablePattern = new(
        @"^(?<file>.+?\.(?:exe|com|bat|cmd|msi))(?=\s|$)",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant);

    public static bool TryParse(string? commandLine, out UninstallCommand? command, out string? error)
    {
        command = null;
        error = null;
        string expanded = Environment.ExpandEnvironmentVariables(commandLine?.Trim() ?? string.Empty);
        if (expanded.Length == 0)
        {
            error = "卸载命令为空。";
            return false;
        }

        string fileName;
        string arguments;
        if (expanded[0] == '"')
        {
            int closingQuote = expanded.IndexOf('"', 1);
            if (closingQuote <= 1)
            {
                error = "卸载命令中的路径引号不完整。";
                return false;
            }

            fileName = expanded[1..closingQuote].Trim();
            arguments = expanded[(closingQuote + 1)..].TrimStart();
        }
        else
        {
            var match = ExecutablePattern.Match(expanded);
            if (match.Success)
            {
                fileName = match.Groups["file"].Value.Trim();
                arguments = expanded[match.Length..].TrimStart();
            }
            else
            {
                int separator = expanded.IndexOfAny([' ', '\t']);
                fileName = (separator < 0 ? expanded : expanded[..separator]).Trim();
                arguments = separator < 0 ? string.Empty : expanded[(separator + 1)..].TrimStart();
            }
        }

        if (fileName.Length == 0)
        {
            error = "卸载命令缺少程序路径。";
            return false;
        }

        if (fileName.IndexOf('"') >= 0)
        {
            error = "卸载命令中的路径包含无效引号。";
            return false;
        }

        command = new UninstallCommand(fileName, arguments);
        return true;
    }
}

public sealed record UninstallCommand(string FileName, string Arguments);
