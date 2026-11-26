// 公用辅助（实现在 util.cpp，由核心模块负责）。所有长度入参/出参为 mm，角度为 deg。
#pragma once

#include <array>
#include <string>
#include <vector>

extern "C" {
#include <ProToolkit.h>
#include <ProMdl.h>
#include <ProSolid.h>
#include <ProDrawing.h>
#include <ProSelection.h>
#include <ProModelitem.h>
}

#include "registry.h"

namespace cadmcp {

// ---- 错误 ----
const char* pro_err_name(ProError e);                 // PRO_TK_E_NOT_FOUND 等
void check(ProError e, const char* what);             // e != PRO_TK_NO_ERROR → throw CmdError(pro_err_name(e), what)

// ---- 参数读取（缺失/类型错 → CmdError("BAD_ARGS")）----
std::string arg_str(const json& a, const char* key);
double arg_num(const json& a, const char* key);
std::array<double, 2> arg_pt2(const json& a, const char* key);   // [x, y]
std::array<double, 3> arg_pt3(const json& a, const char* key);   // [x, y, z]
bool has(const json& a, const char* key);                        // 存在且非 null

// ---- 字符串 ----
std::wstring widen(const std::string& utf8);
std::string narrow(const wchar_t* w);
void to_proname(const std::string& utf8, ProName out);           // 超 31 字符 → BAD_ARGS
void to_line(const std::string& utf8, ProLine out);

// ---- 模型 ----
ProMdl find_model(const std::string& name);            // 已在会话中的模型（名可带/不带扩展名）；找不到 → PRO_TK_E_NOT_FOUND
ProMdl find_part(const std::string& name);
ProDrawing find_drawing(const std::string& name);
double model_mm_factor(ProMdl m);                       // 1 mm 等于多少模型长度单位（mmNs = 1.0，inch = 1/25.4）

// ---- 特征命名 ----
void set_feature_name(ProFeature* f, const std::string& name);
bool find_feature(ProSolid s, const std::string& name, ProFeature* out);
bool find_datum_plane(ProSolid s, const std::string& name, ProModelitem* surf_out);  // TOP/FRONT/RIGHT 或用户名

// ---- 图纸坐标 ----
// 图纸 mm（左下角原点）↔ 图纸单位坐标（ProPoint3d，sheet 坐标系）
void sheet_mm_to_drw(ProDrawing d, const std::array<double, 2>& mm, ProPoint3d out);
ProView find_view(ProDrawing d, const std::string& view_name);   // 视图名，如 V_FRONT
// 在视图内找投影离图纸点最近的模型边（容差 tol_mm 图纸 mm），构造可用于尺寸/注释附着的 ProSelection
// 找不到 → PRO_TK_E_NOT_FOUND。调用方负责 ProSelectionFree。
ProSelection pick_edge_at(ProDrawing d, ProView v, const std::array<double, 2>& sheet_mm, double tol_mm = 1.5);

}  // namespace cadmcp
