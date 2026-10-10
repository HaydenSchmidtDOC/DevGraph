#include "conn.h"
#include "log.h"
#include "platform.h"

#include <string>
#include <utility>

namespace net {
Connection::Connection(std::string host) : host_(std::move(host)) {}

bool Connection::open() {
    log_info("opening " + host_ + " at " + std::to_string(now_ms()));
    open_ = true;
    return open_;
}

void Connection::close() {
    if (open_) log_info("closing " + host_);
    open_ = false;
}
}  // namespace net
