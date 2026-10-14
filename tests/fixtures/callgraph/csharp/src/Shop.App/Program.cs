using Shop.Domain.Services;
using Shop.Mapping;
using Shop.Sales;
using LegacyUser = Legacy.Accounts.User;

namespace Shop.Cli;

public static class Program
{
    public static void Main(string[] args)
    {
        var service = new UserService();
        var legacy = LegacyUser.Parse(args.Length > 0 ? args[0] : "guest");
        var user = UserMapper.ToDomain(legacy);
        service.Register(user);
        Console.WriteLine(UserMapper.Describe(legacy));
        var order = new Order();
        order.AddLine(9.99m);
        Console.WriteLine(order.Total());
        Console.WriteLine(Banner.Render(service.Count()));
    }
}
