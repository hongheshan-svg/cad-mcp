# cadmcp_tk —— Creo TOOLKIT 服务

Creo TOOLKIT **异步模式**独立程序：连到同机已运行的 Creo 10.0 会话，在 `127.0.0.1:9057` 收 JSON-lines 请求，
负责 CREOSON 做不了的 **从零建模**（拉伸/旋转/孔/圆角倒角/基准）和 **出图细节**（尺寸、注释、表格、基准/公差/粗糙度符号、剖视、球标）。
协议与命令表：[`docs/CREO_TOOLKIT.md`](../docs/CREO_TOOLKIT.md)。Python 端：`src/cadmcp/adapters/creo/toolkit.py`。

> 状态：源码已写完，**尚未在装有 TOOLKIT 的机器上编译**。凡是本机无法核对的 TOOLKIT 函数签名、元素树 id、枚举名，
> 代码里都标了 `TODO(实机核对)`——第一次编译时编译器会指出其中写错的部分，按 TOOLKIT 头文件改正即可。

## 编译（Windows，VS 2022）

```bat
cmake -S creo_toolkit -B build -G "Visual Studio 17 2022" -A x64 ^
      -DPROTOOLKIT_DIR="C:/Program Files/PTC/Creo 10.0.0.0/Common Files/protoolkit/x86e_win64"
cmake --build build --config Release
```

- 链接库默认 `protoolkit.lib;ucore.lib;udata.lib`。先看 `%PROTOOLKIT_DIR%\protk_appls\` 下示例 makefile 中**异步**目标链接哪些库，
  不一致时加 `-DPROTK_LIBS="a.lib;b.lib"`。
- 依赖 nlohmann/json 3.11.3（CMake FetchContent 自动下载；离线时把源码放到本地并用 `FETCHCONTENT_SOURCE_DIR_NLOHMANN_JSON` 指过去）。

## 运行

1. 启动 Creo 10.0（异步程序要连到已运行的会话；`PRO_COMM_MSG_EXE` 环境变量须指向 `<Creo>\Common Files\x86e_win64\obj\pro_comm_msg.exe`）。
2. `build\Release\cadmcp_tk.exe --port 9057 --log cadmcp_tk.log`
3. `config.yaml` 的 creo 目标加：
   ```yaml
   toolkit: {host: 127.0.0.1, port: 9057, timeout: 60}
   ```
   配置后 `build_part`（拉伸件、轴类）走 TOOLKIT，出图细节由 `ext.cadmcp_*` 改为逐项 `tk.drw.*`。
4. 自检：`python -c "from cadmcp.adapters.creo.toolkit import ToolkitClient as C; print(C().call('ping'))"`

## 源码

| 文件 | 内容 |
|---|---|
| `src/main.cpp` | 参数解析、`ProEngineerConnect`、主循环 |
| `src/server.cpp` | 单线程 TCP + JSON-lines，空闲时 `ProEventProcess` |
| `src/registry.cpp` | 命令表、try/catch、错误码 |
| `src/util.cpp` | 参数读取、宽字符、模型/特征/基准面/视图查找、单位换算、图纸选边 |
| `src/cmd_session.cpp` | `ping` / `model.*` |
| `src/cmd_model.cpp` | `feat.*` / `datum.*`（IR 坐标 → Creo 坐标 `(x, z, −y)`） |
| `src/cmd_drawing.cpp` | `drw.*` |

## 首期限制（服务端如实报错，Python 端记警告、不中断）

- `feat.hole` 用拉伸切除实现，不建 118° 钻尖。
- `feat.cosmetic_thread` 只建简单螺纹（不查 .hol 螺纹表），深度方向先试正向、失败再反向。
- `drw.section` 的 half：分界面固定取垂直剖切线的默认基准面，保留哪一半用 Creo 默认；local 需要 Python 端给出 `boundary`
  （规划层局部剖区域的图纸坐标），目前 Python 端仍跳过局部剖。
- 钣金、焊接件、装配建模仍不走 TOOLKIT。

## 实机核对顺序（建议）

1. `ping` → `model.new_part` → `datum.plane` → `model.save`（验证连接、命名、单位）。
2. `feat.extrude`：TOP 面上画矩形，确认草绘坐标（u, v）落在预期位置、拉伸方向为 +Z（IR）。方向反了改 `cmd_model.cpp` 的 SIDE_ONE/SIDE_TWO。
3. `feat.hole`、`feat.revolve`（轴）、`feat.round` / `feat.chamfer`（边选取容差 0.5 mm）。
4. 出图：先 CREOSON 建视图，再逐条 `drw.note` → `drw.dim` → `drw.table` → 符号 → `drw.section`。
5. 每步把 `<name>.oplog.json` 和 `cadmcp_tk.log` 发回。
