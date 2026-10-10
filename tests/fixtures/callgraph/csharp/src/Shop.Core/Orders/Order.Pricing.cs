namespace Shop.Sales;

public partial class Order
{
    private decimal Subtotal() => _lines.Sum();

    private static decimal Tax(decimal amount) => Math.Round(amount * 0.2m, 2);
}
