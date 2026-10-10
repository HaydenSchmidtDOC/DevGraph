#pragma once
#include <string>

namespace net {
class Connection {
public:
    explicit Connection(std::string host);
    bool open();
    void close();

private:
    std::string host_;
    bool open_ = false;
};
}  // namespace net
