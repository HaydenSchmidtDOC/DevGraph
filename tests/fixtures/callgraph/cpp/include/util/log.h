#pragma once
#include <iostream>
#include <string>

namespace util {
inline void log_info(const std::string& message) {
    std::cout << "[info] " << message << '\n';
}
}  // namespace util
