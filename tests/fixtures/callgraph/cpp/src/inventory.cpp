#include "inventory.h"
#include "util/log.h"
#include "util/strings.h"

#include <algorithm>

void Inventory::add(const std::string& name, int qty) {
    items_.push_back(Item{util::trim(name), qty});
    util::log_info("added " + name);
}

int Inventory::total() const {
    int sum = 0;
    for (const auto& item : items_) sum += item.qty;
    return sum;
}

const Item* Inventory::find(const std::string& name) const {
    auto it = std::find_if(items_.begin(), items_.end(), [&](const Item& i) { return i.name == name; });
    return it == items_.end() ? nullptr : &*it;
}
