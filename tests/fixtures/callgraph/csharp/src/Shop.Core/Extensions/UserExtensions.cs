namespace Shop.Domain;

public static class UserExtensions
{
    public static string Initials(this User user) => user.Name.Substring(0, 1);
}
