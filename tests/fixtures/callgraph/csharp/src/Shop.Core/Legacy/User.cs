namespace Legacy.Accounts
{
    public class User
    {
        public string Login { get; set; } = "";

        public string Display() => Login.ToUpperInvariant();

        public static User Parse(string raw) => new User { Login = raw.Trim() };
    }
}
