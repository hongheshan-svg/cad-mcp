// Winsock TCP 服务（协议见 docs/CREO_TOOLKIT.md §2）。
#include <winsock2.h>
#include <ws2tcpip.h>

#include <atomic>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <mutex>

#include "server.h"
#include "util.h"  // 引入 ProToolkit.h（ProEventProcess）与 registry.h

namespace cadmcp {

// ---------------- 日志 ----------------
static std::string g_log_path;
static std::mutex g_log_mu;

void set_log_file(const std::string& path) { g_log_path = path; }

void log_line(const std::string& msg) {
    std::lock_guard<std::mutex> lk(g_log_mu);
    char ts[32];
    std::time_t t = std::time(nullptr);
    std::tm tmv;
    localtime_s(&tmv, &t);
    std::strftime(ts, sizeof ts, "%H:%M:%S", &tmv);
    std::string line = std::string("[") + ts + "] " + msg + "\n";
    std::fputs(line.c_str(), stderr);
    std::fflush(stderr);
    if (!g_log_path.empty()) {
        std::ofstream f(g_log_path, std::ios::app | std::ios::binary);
        if (f) f << line;
    }
}

static std::atomic<bool> g_stop{false};
void request_stop() { g_stop = true; }

// ---------------- 请求处理 ----------------
static json make_error(const json& id, const std::string& code, const std::string& msg) {
    return json{{"id", id}, {"ok", false}, {"error", msg}, {"code", code}};
}

// 处理一行请求，返回应答 JSON；任何异常都转成 ok=false，绝不外抛
static json handle_line(const std::string& line) {
    json id = nullptr;
    try {
        json req = json::parse(line);
        if (!req.is_object()) return make_error(id, "BAD_ARGS", "请求必须是 JSON 对象");
        if (req.contains("id")) id = req["id"];
        if (!req.contains("cmd") || !req["cmd"].is_string())
            return make_error(id, "BAD_ARGS", "缺少字符串字段 cmd");
        std::string cmd = req["cmd"].get<std::string>();
        json args = (req.contains("args") && !req["args"].is_null()) ? req["args"] : json::object();
        if (!args.is_object()) return make_error(id, "BAD_ARGS", "args 必须是对象");
        log_line("-> " + cmd);
        json result = dispatch(cmd, args);
        if (result.is_null()) result = json::object();
        return json{{"id", id}, {"ok", true}, {"result", result}};
    } catch (const CmdError& e) {
        return make_error(id, e.code, e.what());
    } catch (const json::parse_error& e) {
        return make_error(id, "BAD_ARGS", std::string("JSON 解析失败: ") + e.what());
    } catch (const json::exception& e) {  // type_error / out_of_range 等：多为入参类型不对
        return make_error(id, "BAD_ARGS", std::string("参数错误: ") + e.what());
    } catch (const std::exception& e) {
        return make_error(id, "EXCEPTION", e.what());
    } catch (...) {
        return make_error(id, "EXCEPTION", "未知 C++ 异常");
    }
}

static bool send_all(SOCKET s, const std::string& data) {
    size_t off = 0;
    while (off < data.size()) {
        int n = send(s, data.data() + off, static_cast<int>(data.size() - off), 0);
        if (n == SOCKET_ERROR || n == 0) return false;
        off += static_cast<size_t>(n);
    }
    return true;
}

static void reply(SOCKET s, const json& resp) {
    // error_handler_replace：非法 UTF-8 不致让 dump 抛异常
    std::string out;
    try {
        out = resp.dump(-1, ' ', false, json::error_handler_t::replace);
    } catch (...) {
        out = R"({"id":null,"ok":false,"error":"应答序列化失败","code":"EXCEPTION"})";
    }
    out.push_back('\n');
    send_all(s, out);
}

// ---------------- 主循环 ----------------
static const size_t kMaxLine = 16u * 1024 * 1024;

int run_server(int port) {
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        log_line("WSAStartup 失败");
        return 2;
    }
    SOCKET ls = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (ls == INVALID_SOCKET) {
        log_line("socket 创建失败");
        WSACleanup();
        return 2;
    }
    BOOL excl = TRUE;  // Windows 上避免端口被他人复用
    setsockopt(ls, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, reinterpret_cast<const char*>(&excl), sizeof excl);
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<u_short>(port));
    inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);  // 只听本机回环
    if (bind(ls, reinterpret_cast<sockaddr*>(&addr), sizeof addr) == SOCKET_ERROR ||
        listen(ls, 1) == SOCKET_ERROR) {
        log_line("绑定/监听 127.0.0.1:" + std::to_string(port) + " 失败，错误码 " +
                 std::to_string(WSAGetLastError()));
        closesocket(ls);
        WSACleanup();
        return 2;
    }
    log_line("监听 127.0.0.1:" + std::to_string(port));

    SOCKET cs = INVALID_SOCKET;
    std::string buf;
    int rc = 0;

    while (!g_stop) {
        fd_set rd;
        FD_ZERO(&rd);
        // 同时只服务一个客户端：已有连接时不监听 ls（新连接排在 backlog 里，避免 select 空转）
        if (cs == INVALID_SOCKET) FD_SET(ls, &rd);
        else FD_SET(cs, &rd);
        timeval tv{0, 100 * 1000};  // 100ms 超时：空闲时轮询 ProEventProcess
        int n = select(0, &rd, nullptr, nullptr, &tv);
        if (n == SOCKET_ERROR) {
            log_line("select 失败，错误码 " + std::to_string(WSAGetLastError()));
            rc = 2;
            break;
        }
        if (n == 0) {
            // 空闲：让 TOOLKIT 处理与 Creo 之间的消息。
            // TODO(实机核对): 异步模式下 ProEventProcess 无事件时应立即返回；Creo 退出后通常返回非 NO_ERROR（如 PRO_TK_COMM_ERROR）
            ProError pe = ProEventProcess();
            if (pe == PRO_TK_COMM_ERROR) {
                log_line("与 Creo 的连接已断开（PRO_TK_COMM_ERROR），退出");
                rc = 3;
                break;
            }
            continue;
        }
        if (cs == INVALID_SOCKET && FD_ISSET(ls, &rd)) {
            cs = accept(ls, nullptr, nullptr);
            if (cs != INVALID_SOCKET) {
                BOOL nodelay = TRUE;
                setsockopt(cs, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&nodelay), sizeof nodelay);
                buf.clear();
                log_line("客户端已连接");
            }
            continue;
        }
        if (cs != INVALID_SOCKET && FD_ISSET(cs, &rd)) {
            char tmp[65536];
            int r = recv(cs, tmp, sizeof tmp, 0);
            if (r <= 0) {
                log_line("客户端断开");
                closesocket(cs);
                cs = INVALID_SOCKET;
                buf.clear();
                continue;
            }
            buf.append(tmp, static_cast<size_t>(r));
            size_t pos;
            while ((pos = buf.find('\n')) != std::string::npos) {
                std::string line = buf.substr(0, pos);
                buf.erase(0, pos + 1);
                if (!line.empty() && line.back() == '\r') line.pop_back();
                if (line.find_first_not_of(" \t") == std::string::npos) continue;  // 空行忽略
                reply(cs, handle_line(line));
            }
            if (buf.size() > kMaxLine) {
                reply(cs, make_error(nullptr, "BAD_ARGS", "请求行过长"));
                closesocket(cs);
                cs = INVALID_SOCKET;
                buf.clear();
            }
        }
    }

    if (cs != INVALID_SOCKET) closesocket(cs);
    closesocket(ls);
    WSACleanup();
    return rc;
}

}  // namespace cadmcp
