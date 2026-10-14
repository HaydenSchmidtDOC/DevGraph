cmake_minimum_required(VERSION 3.20)
project(inventory CXX)
set(CMAKE_CXX_STANDARD 17)

set(CORE_SOURCES src/inventory.cpp src/report.cpp src/util/strings.cpp src/net/conn.cpp)
if(WIN32)
  list(APPEND CORE_SOURCES src/platform_win.cpp)
else()
  list(APPEND CORE_SOURCES src/platform_posix.cpp)
endif()

add_executable(inventory src/main.cpp ${CORE_SOURCES})
target_include_directories(inventory PRIVATE include)

add_executable(inventory_test tests/inventory_test.cpp ${CORE_SOURCES})
target_include_directories(inventory_test PRIVATE include)
