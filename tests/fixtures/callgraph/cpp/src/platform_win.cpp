#include "platform.h"

#include <windows.h>

long now_ms() {
    return static_cast<long>(GetTickCount64());
}
