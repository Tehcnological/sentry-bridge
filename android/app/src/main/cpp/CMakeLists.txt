cmake_minimum_required(VERSION 3.18)
project(sbjni C)

# The same portable core as the Windows/Linux bridge and the parity tests
set(CORE_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../../../../../core)

add_library(sbjni SHARED sb_jni.c ${CORE_DIR}/sbcore.c)
target_include_directories(sbjni PRIVATE ${CORE_DIR})
# Reproducible builds: no build paths, build IDs or compiler identity strings in the library, so any
# checkout location and host gives the same bytes (the Windows and Linux NDK compilers name themselves differently).
get_filename_component(REPO_DIR ${CMAKE_CURRENT_SOURCE_DIR}/../../../../.. ABSOLUTE)
target_compile_options(sbjni PRIVATE -ffile-prefix-map=${REPO_DIR}=. -fno-ident)
target_link_options(sbjni PRIVATE -Wl,--build-id=none)
target_link_libraries(sbjni m)
