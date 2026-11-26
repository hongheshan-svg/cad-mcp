// cadmcp_tk 命令注册（协议见 docs/CREO_TOOLKIT.md）。
// 各 cmd_*.cpp 在 register_xxx_cmds() 中调用 register_cmd 注册处理函数；main.cpp 启动时依次调用。
#pragma once

#include <functional>
#include <stdexcept>
#include <string>

#include "json.hpp"  // nlohmann/json（CMake FetchContent 拉取，见 CMakeLists.txt）

namespace cadmcp {

using json = nlohmann::json;

// 命令失败：code 为 ProError 名（如 "PRO_TK_E_NOT_FOUND"）或 "BAD_ARGS" / "UNKNOWN_CMD" / "EXCEPTION"
struct CmdError : std::runtime_error {
    std::string code;
    CmdError(std::string c, const std::string& msg) : std::runtime_error(msg), code(std::move(c)) {}
};

using Handler = std::function<json(const json& args)>;

void register_cmd(const std::string& name, Handler h);
// 分发：未知命令抛 CmdError("UNKNOWN_CMD")；由 server 捕获并组装应答
json dispatch(const std::string& name, const json& args);

// 各模块注册入口（main.cpp 调用）
void register_session_cmds();   // cmd_session.cpp
void register_model_cmds();     // cmd_model.cpp
void register_drawing_cmds();   // cmd_drawing.cpp

}  // namespace cadmcp
