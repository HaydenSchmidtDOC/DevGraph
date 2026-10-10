namespace Shop.Interop;

public static class DynamicBridge
{
    // Late-bound: the runtime type of `account` decides which Display runs.
    public static string Show(dynamic account) => account.Display();
}
