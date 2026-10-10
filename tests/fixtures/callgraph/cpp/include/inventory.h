#pragma once
#include <string>
#include <vector>

struct Item {
    std::string name;
    int qty;
};

class Inventory {
public:
    void add(const std::string& name, int qty);
    int total() const;
    const Item* find(const std::string& name) const;
    int size() const { return static_cast<int>(items_.size()); }

private:
    std::vector<Item> items_;
};
