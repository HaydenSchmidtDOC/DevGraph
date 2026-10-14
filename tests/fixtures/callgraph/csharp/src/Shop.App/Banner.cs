namespace Shop.Cli
{
    internal static class Banner
    {
        public static string Render(int users) => $"{Formatting.Title("users")}: {users} ({Stamp()})";

        private static string Stamp() => DateTime.UtcNow.ToString("u");
    }
}
