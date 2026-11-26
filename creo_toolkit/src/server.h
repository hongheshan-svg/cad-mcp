// TCP 服务：127.0.0.1:port，每行一个 JSON，单线程，同时只服务一个客户端。
#pragma once

#include <string>

namespace cadmcp {

// 日志：写 stderr，若 set_log_file 设置过则同时追加到文件
void set_log_file(const std::string& path);
void log_line(const std::string& msg);

// 阻塞运行直到 request_stop() 被调用或 Creo 连接断开。返回进程退出码。
int run_server(int port);
void request_stop();

}  // namespace cadmcp
