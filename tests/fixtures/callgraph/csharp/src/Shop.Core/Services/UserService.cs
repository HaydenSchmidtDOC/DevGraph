using Legacy.Accounts;

namespace Shop.Domain.Services;

// `User` below is Shop.Domain.User: members of the enclosing namespace
// Shop.Domain are found before the compilation unit's using directives.
public class UserService
{
    private readonly List<User> _users = new();

    public void Register(User user)
    {
        if (!user.IsValid())
        {
            throw new ArgumentException("invalid user");
        }
        _users.Add(user);
        AuditLog.Write(user.Display());
    }

    public int Count() => _users.Count;

    public User? Find(string name) => _users.FirstOrDefault(u => u.Name == name);

    public string Badge(User user) => user.Initials() + Formatting.Mask(user.Name);
}
