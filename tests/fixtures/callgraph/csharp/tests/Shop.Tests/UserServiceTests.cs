using Shop.Domain;
using Shop.Domain.Services;
using Xunit;

namespace Shop.Tests;

public class UserServiceTests
{
    [Fact]
    public void RegisterAddsUser()
    {
        var service = new UserService();
        service.Register(new User("ada"));
        Assert.Equal(1, service.Count());
    }
}
