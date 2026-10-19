# Sentry Bridge for Raspberry Pi Pico W and Pico 2 W. Build with pico-sdk 2.1:
#   cmake -S . -B build -DPICO_BOARD=pico_w   (or pico2_w) && cmake --build build
cmake_minimum_required(VERSION 3.13)
include($ENV{PICO_SDK_PATH}/pico_sdk_init.cmake)
project(sentry_bridge C CXX ASM)
set(CMAKE_C_STANDARD 11)
pico_sdk_init()

add_executable(sentry_bridge main.c ../common/fw_bridge.c ../../core/sbcore.c)
target_include_directories(sentry_bridge PRIVATE ${CMAKE_CURRENT_LIST_DIR} ../common ../../core)
target_link_libraries(sentry_bridge pico_stdlib pico_cyw43_arch_lwip_poll)
# The core's sb_feed keeps about 3 KB of frame buffers on the stack; the SDK default is 2 KB.
# 8 KB fills exactly the two 4 KB scratch banks (the second core's is unused here), so the stack
# never reaches main RAM, where the heap grows.
target_compile_definitions(sentry_bridge PRIVATE PICO_STACK_SIZE=0x2000)
pico_enable_stdio_usb(sentry_bridge 1)
pico_enable_stdio_uart(sentry_bridge 0)
pico_add_extra_outputs(sentry_bridge)
