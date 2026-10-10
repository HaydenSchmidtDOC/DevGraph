#include "inventory.h"
#include "net/conn.h"
#include "report.h"
#include "util/log.h"

#include <algorithm>
#include <string>
#include <vector>

static bool known(const std::vector<std::string>& names, const std::string& n) {
    return std::find(names.begin(), names.end(), n) != names.end();
}

int main() {
    Inventory inv;
    inv.add("  bolt ", 4);
    inv.add("nut", 10);
    if (inv.find("bolt") && known({"bolt", "nut"}, "nut")) util::log_info("stock ok");
    net::Connection conn("db.local");
    conn.open();
    print_report(inv);
    conn.close();
    return 0;
}
