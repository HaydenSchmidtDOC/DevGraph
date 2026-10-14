#include "inventory.h"

#include <cstdio>

int main() {
    Inventory inv;
    inv.add("a", 2);
    if (inv.total() != 2 || inv.size() != 1) {
        std::puts("FAIL");
        return 1;
    }
    return 0;
}
