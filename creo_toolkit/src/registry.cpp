// 命令注册表与分发（接口见 registry.h）。
#include "registry.h"

#include <map>

namespace cadmcp {

static std::map<std::string, Handler>& table() {
    static std::map<std::string, Handler> t;  // 函数内静态：避免静态初始化顺序问题
    return t;
}

void register_cmd(const std::string& name, Handler h) {
    table()[name] = std::move(h);
}

json dispatch(const std::string& name, const json& args) {
    auto it = table().find(name);
    if (it == table().end()) throw CmdError("UNKNOWN_CMD", "未知命令: " + name);
    return it->second(args);
}

}  // namespace cadmcp
