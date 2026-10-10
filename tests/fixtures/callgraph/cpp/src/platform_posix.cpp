#include "platform.h"

#include <chrono>

long now_ms() {
    using namespace std::chrono;
    return static_cast<long>(duration_cast<milliseconds>(steady_clock::now().time_since_epoch()).count());
}
