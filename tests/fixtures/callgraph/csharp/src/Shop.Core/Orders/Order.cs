namespace Shop.Sales;

public partial class Order
{
    private readonly List<decimal> _lines = new();

    public void AddLine(decimal price)
    {
        _lines.Add(price);
    }

    public decimal Total() => Subtotal() + Tax(Subtotal());
}
