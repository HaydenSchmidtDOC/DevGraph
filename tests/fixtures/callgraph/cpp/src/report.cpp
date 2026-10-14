#include "report.h"
#include "util/strings.h"

#include <cstdio>

void print_report(const Inventory& inv) {
    std::printf("%s: %d items (%d units)\n", util::upper("inventory").c_str(), inv.size(), inv.total());
}
