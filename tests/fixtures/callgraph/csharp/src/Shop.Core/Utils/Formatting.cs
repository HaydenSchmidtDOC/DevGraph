namespace Shop.Text;

public static class Formatting
{
    public static string Title(string s) => s.Length == 0 ? s : char.ToUpper(s[0]) + s.Substring(1);

    public static string Mask(string s) => new string('*', s.Length);
}
