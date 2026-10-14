using Legacy.Accounts;

namespace Shop.Mapping;

// Nothing named Mappers here: the file holds UserMapper.
public static class UserMapper
{
    public static Shop.Domain.User ToDomain(User legacy) => new Shop.Domain.User(legacy.Login);

    public static string Describe(User legacy) => legacy.Display();
}
