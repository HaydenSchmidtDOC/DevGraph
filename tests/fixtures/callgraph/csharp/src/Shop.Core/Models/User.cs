namespace Shop.Domain;

public class User
{
    public User(string name)
    {
        Name = name;
    }

    public string Name { get; }

    public string Display() => Formatting.Title(Name);

    public bool IsValid() => !string.IsNullOrWhiteSpace(Name);
}
