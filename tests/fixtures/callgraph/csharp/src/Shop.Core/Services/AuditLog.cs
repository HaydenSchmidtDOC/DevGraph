namespace Shop.Domain.Services
{
    internal static class AuditLog
    {
        public static void Write(string message) => Console.WriteLine(message);
    }
}
