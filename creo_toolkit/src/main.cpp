// cadmcp_tk：Creo TOOLKIT 异步模式入口。
// 用法：cadmcp_tk.exe [--port 9057] [--creo-cmd "<启动 Creo 的命令>"] [--log <文件>]
// 运行前需设置环境变量 PRO_COMM_MSG_EXE 指向 <Creo>\Common Files\x86e_win64\obj\pro_comm_msg.exe（异步模式必需）。
#include <windows.h>

#include <cstdlib>
#include <string>

#include "registry.h"
#include "server.h"
#include "util.h"

using namespace cadmcp;

static BOOL WINAPI on_ctrl(DWORD) {
    request_stop();
    return TRUE;
}

// 连接/启动 Creo。把可能与头文件不符的 TOOLKIT 调用集中在这里，方便实机核对。
static ProError connect_creo(const std::string& creo_cmd) {
    if (!creo_cmd.empty()) {
        // 由我们拉起 Creo：ProEngineerStart(prm_cmd, prm_textpath)，之后会话属于本进程
        // TODO(实机核对): ProEngineerStart 的第二参数（textpath，异步模式可传空串）；以及它是否在连接成功后才返回
        return ProEngineerStart(const_cast<char*>(creo_cmd.c_str()), const_cast<char*>(""));
    }
    // 连接已运行的 Creo 会话。
    // 签名（TOOLKIT API 参考）：ProEngineerConnect(char* identity, char* display, char* user, char* textpath,
    //   ProBoolean random_choice, int timeout_sec, ProBoolean* p_random_choice, ProProcessHandle* p_handle)
    // identity/display/user 为空串 = 本机当前用户的任一会话；random_choice=PRO_B_TRUE：有多个会话时任选一个。
    // TODO(实机核对): 参数个数与顺序以所用 Creo 版本头文件 ProCore.h 为准。
    ProBoolean chosen = PRO_B_FALSE;
    ProProcessHandle handle;
    return ProEngineerConnect(const_cast<char*>(""), const_cast<char*>(""), const_cast<char*>(""),
                              const_cast<char*>(""), PRO_B_TRUE, 30, &chosen, &handle);
}

int main(int argc, char** argv) {
    int port = 9057;
    std::string creo_cmd, log_path;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&](const char* name) -> std::string {
            if (i + 1 >= argc) {
                fprintf(stderr, "参数 %s 缺少值\n", name);
                std::exit(1);
            }
            return argv[++i];
        };
        if (a == "--port") port = std::atoi(next("--port").c_str());
        else if (a == "--creo-cmd") creo_cmd = next("--creo-cmd");
        else if (a == "--log") log_path = next("--log");
        else if (a == "-h" || a == "--help") {
            printf("cadmcp_tk [--port 9057] [--creo-cmd <cmd>] [--log <file>]\n");
            return 0;
        } else {
            fprintf(stderr, "未知参数: %s\n", a.c_str());
            return 1;
        }
    }
    if (port <= 0 || port > 65535) {
        fprintf(stderr, "端口无效\n");
        return 1;
    }
    if (!log_path.empty()) set_log_file(log_path);
    if (!std::getenv("PRO_COMM_MSG_EXE"))
        log_line("警告: 未设置环境变量 PRO_COMM_MSG_EXE，异步模式连接 Creo 很可能失败");

    SetConsoleCtrlHandler(on_ctrl, TRUE);

    ProError e = connect_creo(creo_cmd);
    if (e != PRO_TK_NO_ERROR) {
        log_line(std::string("连接 Creo 失败: ") + pro_err_name(e));
        return 2;
    }
    log_line("已连接 Creo: " + creo_release());

    register_session_cmds();
    register_model_cmds();
    register_drawing_cmds();

    int rc = run_server(port);

    ProEngineerDisconnect();  // TODO(实机核对): 若本进程用 ProEngineerStart 拉起 Creo，应改用 ProEngineerEnd()
    log_line("已断开 Creo，退出");
    return rc;
}
