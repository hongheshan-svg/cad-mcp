# cadmcp_tk —— Creo TOOLKIT 异步模式独立程序（协议见 docs/CREO_TOOLKIT.md）
# 仅在装有 Creo 10.0 + TOOLKIT 的 Windows 上构建：
#   cmake -S creo_toolkit -B build -G "Visual Studio 17 2022" -A x64 -DPROTOOLKIT_DIR="C:/Program Files/PTC/Creo 10.0.0.0/Common Files/protoolkit/x86e_win64"
#   cmake --build build --config Release
cmake_minimum_required(VERSION 3.20)
project(cadmcp_tk LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# TOOLKIT 的静态库以 /MD（动态 CRT）构建
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreadedDLL")

set(PROTOOLKIT_DIR "C:/Program Files/PTC/Creo 10.0.0.0/Common Files/protoolkit/x86e_win64"
    CACHE PATH "Creo TOOLKIT 目录（含 includes/ 与 obj/）")

# ---- TOOLKIT 库 ----
# 来源：<PROTOOLKIT_DIR>/protk_appls/ 下示例的 makefile（Makefile.nt / *.mak）中
# 异步（asynchronous）目标的 LIBS 部分；TOOLKIT 库一般在 <PROTOOLKIT_DIR>/obj/。
# 核对方法：在 PROTOOLKIT_DIR 下 `dir obj\*.lib`，对照示例 makefile 里异步程序实际链接的库；
#   与下面默认值不一致时用 -DPROTK_LIBS="a.lib;b.lib" 覆盖。
# TODO(实机核对)：默认值按“异步程序链接静态 protoolkit.lib（不是 DLL 模式的 protk_dll*.lib）+ ucore/udata”假设。
#   若你环境里只有 protk_dllmd_NU.lib 之类，请改 PROTK_LIBS。
set(PROTK_LIB_DIR "${PROTOOLKIT_DIR}/obj" CACHE PATH "TOOLKIT 库目录")
set(PROTK_LIBS "protoolkit.lib;ucore.lib;udata.lib" CACHE STRING "TOOLKIT 异步模式库列表（见上方注释核对）")

# ---- nlohmann/json v3.11.3 ----
include(FetchContent)
set(JSON_BuildTests OFF CACHE INTERNAL "")
set(JSON_Install OFF CACHE INTERNAL "")
FetchContent_Declare(nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG v3.11.3
    GIT_SHALLOW TRUE)
FetchContent_MakeAvailable(nlohmann_json)

# cmd_model.cpp / cmd_drawing.cpp 由其他人提供，这里统一收集
file(GLOB CADMCP_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/src/*.cpp")
add_executable(cadmcp_tk ${CADMCP_SOURCES})

target_include_directories(cadmcp_tk PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/src"
    "${PROTOOLKIT_DIR}/includes"
    # 使 #include "json.hpp" 可用
    "${nlohmann_json_SOURCE_DIR}/single_include/nlohmann")

target_compile_definitions(cadmcp_tk PRIVATE
    PRO_USE_VAR_ARGS _CRT_SECURE_NO_WARNINGS WIN32_LEAN_AND_MEAN NOMINMAX)
target_compile_options(cadmcp_tk PRIVATE /utf-8 /W3 /EHsc)

target_link_directories(cadmcp_tk PRIVATE "${PROTK_LIB_DIR}")
target_link_libraries(cadmcp_tk PRIVATE ${PROTK_LIBS}
    ws2_32.lib wsock32.lib mpr.lib psapi.lib netapi32.lib
    kernel32.lib user32.lib gdi32.lib advapi32.lib shell32.lib comdlg32.lib winspool.lib)
