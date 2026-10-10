#pragma once
#include <iostream>
#include <string>

// Same file name and function name as include/util/log.h, different namespace.
namespace net {
inline void log_info(const std::string& message) {
    std::cerr << "[net] " << message << '\n';
}
}  // namespace net
