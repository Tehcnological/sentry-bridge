cmake_minimum_required(VERSION 3.15)
project(sentry_bridge C)

set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
# Windows: link the C runtime statically so sentry-bridge.exe runs without any installer
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

if(MSVC)
  add_compile_options(/W3 /D_CRT_SECURE_NO_WARNINGS)
else()
  add_compile_options(-Wall -Wextra)
endif()

# The portable core: used by the CLI and the Android app (via JNI); no sockets or heap, so it also suits an ESP32
add_library(sbcore STATIC core/sbcore.c)
# distinct file name: on Windows the DLL's import library would otherwise also be sbcore.lib
set_target_properties(sbcore PROPERTIES OUTPUT_NAME sbcore_static POSITION_INDEPENDENT_CODE ON)
target_include_directories(sbcore PUBLIC core)
if(NOT MSVC)
  target_link_libraries(sbcore PUBLIC m)
endif()

# Shared build of the core for the Python parity tests (ctypes)
add_library(sbcore_shared SHARED core/sbcore.c)
set_target_properties(sbcore_shared PROPERTIES OUTPUT_NAME sbcore WINDOWS_EXPORT_ALL_SYMBOLS ON)
if(NOT MSVC)
  target_link_libraries(sbcore_shared PRIVATE m)
endif()

add_executable(sentry-bridge cli/sentry_bridge.c)
target_link_libraries(sentry-bridge PRIVATE sbcore)
if(WIN32)
  target_sources(sentry-bridge PRIVATE cli/sentry_bridge.rc)
  target_link_libraries(sentry-bridge PRIVATE ws2_32)
else()
  # Sentry web requests run on a worker thread
  find_package(Threads REQUIRED)
  target_link_libraries(sentry-bridge PRIVATE Threads::Threads)
endif()

# Desktop JNI replay exercises the Android wrapper; no Java desktop graphics are needed.
find_package(JNI QUIET COMPONENTS JVM)
if(JNI_FOUND)
  add_library(sbjni SHARED android/app/src/main/cpp/sb_jni.c)
  target_include_directories(sbjni PRIVATE ${JNI_INCLUDE_DIRS})
  target_link_libraries(sbjni PRIVATE sbcore)
endif()

add_executable(fuzz-core tools/fuzz_core.c core/sbcore.c)
target_include_directories(fuzz-core PRIVATE core)
if(NOT MSVC)
  target_link_libraries(fuzz-core PRIVATE m)
endif()
option(SB_FUZZ_ASAN "Instrument the corruption harness with AddressSanitizer" OFF)
if(SB_FUZZ_ASAN)
  if(MSVC)
    target_compile_options(fuzz-core PRIVATE /fsanitize=address /Zi)
    target_link_options(fuzz-core PRIVATE /INCREMENTAL:NO)
  else()
    target_compile_options(fuzz-core PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
    target_link_options(fuzz-core PRIVATE -fsanitize=address,undefined)
  endif()
endif()
