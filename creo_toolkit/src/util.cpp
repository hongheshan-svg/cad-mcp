// 公用辅助实现。注意：本文件凭 Creo TOOLKIT API 记忆编写，无法在 macOS 上编译；
// 不确定处以 TODO(实机核对) 标注，汇总在 creo_toolkit/README.md。
#include <windows.h>

#include <algorithm>
#include <cmath>
#include <cwchar>
#include <map>

extern "C" {
#include <ProArray.h>
#include <ProContour.h>
#include <ProEdge.h>
#include <ProFeature.h>
#include <ProGeomitem.h>
#include <ProParamval.h>
#include <ProParameter.h>
#include <ProSurface.h>
#include <ProUnit.h>
}

#include "util.h"

namespace cadmcp {

// ====================== 错误 ======================

const char* pro_err_name(ProError e) {
    switch (e) {
        case PRO_TK_NO_ERROR: return "PRO_TK_NO_ERROR";
        case PRO_TK_GENERAL_ERROR: return "PRO_TK_GENERAL_ERROR";
        case PRO_TK_BAD_INPUTS: return "PRO_TK_BAD_INPUTS";
        case PRO_TK_USER_ABORT: return "PRO_TK_USER_ABORT";
        case PRO_TK_E_NOT_FOUND: return "PRO_TK_E_NOT_FOUND";
        case PRO_TK_E_FOUND: return "PRO_TK_E_FOUND";
        case PRO_TK_LINE_TOO_LONG: return "PRO_TK_LINE_TOO_LONG";
        case PRO_TK_CONTINUE: return "PRO_TK_CONTINUE";
        case PRO_TK_BAD_CONTEXT: return "PRO_TK_BAD_CONTEXT";
        case PRO_TK_NOT_IMPLEMENTED: return "PRO_TK_NOT_IMPLEMENTED";
        case PRO_TK_OUT_OF_MEMORY: return "PRO_TK_OUT_OF_MEMORY";
        case PRO_TK_COMM_ERROR: return "PRO_TK_COMM_ERROR";
        case PRO_TK_NO_CHANGE: return "PRO_TK_NO_CHANGE";
        case PRO_TK_SUPPRESSED: return "PRO_TK_SUPPRESSED";
        case PRO_TK_NO_LICENSE: return "PRO_TK_NO_LICENSE";
        case PRO_TK_E_AMBIGUOUS: return "PRO_TK_E_AMBIGUOUS";
        case PRO_TK_E_DEADLOCK: return "PRO_TK_E_DEADLOCK";
        case PRO_TK_E_BUSY: return "PRO_TK_E_BUSY";
        case PRO_TK_INVALID_DIR: return "PRO_TK_INVALID_DIR";
        case PRO_TK_INVALID_FILE: return "PRO_TK_INVALID_FILE";
        case PRO_TK_INVALID_TYPE: return "PRO_TK_INVALID_TYPE";
        case PRO_TK_INVALID_PTR: return "PRO_TK_INVALID_PTR";
        case PRO_TK_UNAV_SEC: return "PRO_TK_UNAV_SEC";
        case PRO_TK_UNSUPPORTED: return "PRO_TK_UNSUPPORTED";
        // TODO(实机核对): 其余枚举（PRO_TK_BAD_SRFS_ORIENT / PRO_TK_NOT_EXIST 等）按需补；未列出的回落为数字
        default: break;
    }
    static thread_local char buf[40];
    std::snprintf(buf, sizeof buf, "PRO_TK_ERR_%d", static_cast<int>(e));
    return buf;
}

void check(ProError e, const char* what) {
    if (e != PRO_TK_NO_ERROR) throw CmdError(pro_err_name(e), what);
}

// ====================== 参数 ======================

static const json& need(const json& a, const char* key) {
    if (!a.is_object() || !a.contains(key) || a[key].is_null())
        throw CmdError("BAD_ARGS", std::string("缺少参数: ") + key);
    return a[key];
}

std::string arg_str(const json& a, const char* key) {
    const json& v = need(a, key);
    if (!v.is_string()) throw CmdError("BAD_ARGS", std::string("参数应为字符串: ") + key);
    return v.get<std::string>();
}

double arg_num(const json& a, const char* key) {
    const json& v = need(a, key);
    if (!v.is_number()) throw CmdError("BAD_ARGS", std::string("参数应为数字: ") + key);
    return v.get<double>();
}

static std::vector<double> num_list(const json& a, const char* key, size_t n) {
    const json& v = need(a, key);
    if (!v.is_array() || v.size() != n)
        throw CmdError("BAD_ARGS", std::string("参数应为 ") + std::to_string(n) + " 个数字的数组: " + key);
    std::vector<double> out;
    for (const auto& x : v) {
        if (!x.is_number()) throw CmdError("BAD_ARGS", std::string("数组元素应为数字: ") + key);
        out.push_back(x.get<double>());
    }
    return out;
}

std::array<double, 2> arg_pt2(const json& a, const char* key) {
    auto v = num_list(a, key, 2);
    return {v[0], v[1]};
}

std::array<double, 3> arg_pt3(const json& a, const char* key) {
    auto v = num_list(a, key, 3);
    return {v[0], v[1], v[2]};
}

bool has(const json& a, const char* key) {
    return a.is_object() && a.contains(key) && !a[key].is_null();
}

// ====================== 字符串 ======================

std::wstring widen(const std::string& utf8) {
    if (utf8.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, utf8.data(), static_cast<int>(utf8.size()), nullptr, 0);
    if (n <= 0) throw CmdError("BAD_ARGS", "字符串不是合法 UTF-8");
    std::wstring w(static_cast<size_t>(n), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, utf8.data(), static_cast<int>(utf8.size()), &w[0], n);
    return w;
}

std::string narrow(const wchar_t* w) {
    if (!w || !*w) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w, -1, nullptr, 0, nullptr, nullptr);
    if (n <= 1) return std::string();
    std::string s(static_cast<size_t>(n - 1), '\0');
    WideCharToMultiByte(CP_UTF8, 0, w, -1, &s[0], n, nullptr, nullptr);
    return s;
}

void to_proname(const std::string& utf8, ProName out) {
    std::wstring w = widen(utf8);
    if (w.size() > PRO_NAME_SIZE - 1)  // ProName 最长 31 字符
        throw CmdError("BAD_ARGS", "名称超过 31 字符: " + utf8);
    std::wmemcpy(out, w.c_str(), w.size() + 1);
}

void to_line(const std::string& utf8, ProLine out) {
    std::wstring w = widen(utf8);
    if (w.size() > PRO_LINE_SIZE - 1) throw CmdError("BAD_ARGS", "文本超过 ProLine 长度（79 字符）");
    std::wmemcpy(out, w.c_str(), w.size() + 1);
}

// ====================== 模型 ======================

static std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

// 去掉 .prt/.asm/.drw 及其后的版本号（.prt.3），返回基名；hint 返回扩展名暗示的类型（无则 -1）
static std::string strip_ext(const std::string& name, int* hint) {
    *hint = -1;
    std::string s = name;
    // 去掉版本号 ".<数字>"
    size_t dot = s.rfind('.');
    if (dot != std::string::npos && dot + 1 < s.size() &&
        std::all_of(s.begin() + dot + 1, s.end(), [](unsigned char c) { return std::isdigit(c); }))
        s = s.substr(0, dot);
    dot = s.rfind('.');
    if (dot != std::string::npos) {
        std::string ext = lower(s.substr(dot + 1));
        if (ext == "prt") *hint = 0;
        else if (ext == "asm") *hint = 1;
        else if (ext == "drw") *hint = 2;
        if (*hint >= 0) s = s.substr(0, dot);
    }
    return s;
}

static const ProMdlfileType kFileTypes[3] = {PRO_MDLFILE_PART, PRO_MDLFILE_ASSEMBLY, PRO_MDLFILE_DRAWING};

ProMdl find_model(const std::string& name) {
    int hint;
    std::string base = strip_ext(name, &hint);
    ProName pn;
    to_proname(base, pn);
    // 有扩展名则只试对应类型，否则依次试 part/assembly/drawing
    for (int i = 0; i < 3; ++i) {
        if (hint >= 0 && hint != i) continue;
        ProMdl m = nullptr;
        // TODO(实机核对): ProMdlnameInit(ProName, ProMdlfileType, ProMdl*)；老接口为 ProMdlInit(name, ProMdlType, &mdl)
        if (ProMdlnameInit(pn, kFileTypes[i], &m) == PRO_TK_NO_ERROR && m) return m;
    }
    throw CmdError("PRO_TK_E_NOT_FOUND", "会话中找不到模型: " + name);
}

std::string model_name(ProMdl m) {
    ProName n;
    // TODO(实机核对): ProMdlMdlnameGet(ProMdl, ProName)
    if (ProMdlMdlnameGet(m, n) != PRO_TK_NO_ERROR) return std::string();
    return narrow(n);
}

std::string model_type_name(ProMdl m) {
    ProMdlType t;
    if (ProMdlTypeGet(m, &t) != PRO_TK_NO_ERROR) return "other";
    switch (t) {
        case PRO_MDL_PART: return "part";
        case PRO_MDL_ASSEMBLY: return "assembly";
        case PRO_MDL_DRAWING: return "drawing";
        default: return "other";
    }
}

static ProMdl find_part_or_asm_check(ProMdl m, const char* want, const std::string& name) {
    if (model_type_name(m) != want)
        throw CmdError("PRO_TK_E_NOT_FOUND", "模型 " + name + " 不是 " + want);
    return m;
}

ProMdl find_part(const std::string& name) {
    return find_part_or_asm_check(find_model(name), "part", name);
}

ProDrawing find_drawing(const std::string& name) {
    ProMdl m = find_part_or_asm_check(find_model(name), "drawing", name);
    return reinterpret_cast<ProDrawing>(m);  // ProDrawing 与 ProMdl 同为不透明句柄
}

std::string creo_release() {
    // TODO(实机核对): ProEngineerReleaseGet 是否存在及其参数类型（此处假设 ProLine，宽字符）；取不到则回落 unknown
#ifdef CADMCP_NO_RELEASE_GET
    return "unknown";
#else
    ProLine rel;
    if (ProEngineerReleaseGet(rel) == PRO_TK_NO_ERROR) return narrow(rel);
    return "unknown";
#endif
}

static void unit_mm(ProMdl owner, ProUnititem* mm) {
    ProName n;
    to_proname("mm", n);
    // TODO(实机核对): ProUnitInit(ProMdl, ProName, ProUnititem*)
    check(ProUnitInit(owner, n, mm), "ProUnitInit(mm)");
}

double model_mm_factor(ProMdl m) {
    ProUnitsystem us;
    check(ProMdlPrincipalunitsystemGet(m, &us), "ProMdlPrincipalunitsystemGet");
    ProUnititem len;
    check(ProUnitsystemUnitGet(&us, PRO_UNITTYPE_LENGTH, &len), "ProUnitsystemUnitGet(LENGTH)");
    ProUnititem mm;
    unit_mm(m, &mm);
    // mm → 模型长度单位：value_model = value_mm * scale + offset（长度 offset=0）
    ProUnitConversion conv;
    check(ProUnitConversionGet(&mm, &conv, &len), "ProUnitConversionGet");
    return conv.scale;
}

// ====================== 特征 ======================

struct FeatCollect {
    std::vector<ProFeature> v;
};

static ProError feat_collect_cb(ProFeature* f, ProError, ProAppData d) {
    static_cast<FeatCollect*>(d)->v.push_back(*f);
    return PRO_TK_NO_ERROR;
}

std::vector<ProFeature> list_features(ProSolid s) {
    FeatCollect c;
    // TODO(实机核对): ProSolidFeatVisit(ProSolid, visit, filter, appdata)；filter 传 NULL 表示全部
    check(ProSolidFeatVisit(s, feat_collect_cb, nullptr, &c), "ProSolidFeatVisit");
    return c.v;
}

std::string feature_name(ProFeature* f) {
    ProName n;
    if (ProModelitemNameGet(reinterpret_cast<ProModelitem*>(f), n) != PRO_TK_NO_ERROR) return std::string();
    return narrow(n);
}

void set_feature_name(ProFeature* f, const std::string& name) {
    ProName n;
    to_proname(name, n);
    check(ProModelitemNameSet(reinterpret_cast<ProModelitem*>(f), n), "ProModelitemNameSet");
}

bool find_feature(ProSolid s, const std::string& name, ProFeature* out) {
    for (auto& f : list_features(s)) {
        if (feature_name(&f) == name) {
            *out = f;
            return true;
        }
    }
    return false;
}

// 取基准特征下的曲面 modelitem
struct SurfPick {
    ProModelitem item;
    bool found = false;
};

static ProError geom_surf_cb(ProGeomitem* g, ProError, ProAppData d) {
    auto* p = static_cast<SurfPick*>(d);
    if (!p->found) {
        p->item = *reinterpret_cast<ProModelitem*>(g);
        p->found = true;
    }
    return PRO_TK_NO_ERROR;
}

static bool datum_surface_of_feature(ProFeature* f, ProModelitem* out) {
    SurfPick sp;
    // TODO(实机核对): ProFeatureGeomitemVisit(ProFeature*, ProType, action, filter, appdata)
    ProFeatureGeomitemVisit(f, PRO_SURFACE, geom_surf_cb, nullptr, &sp);
    if (sp.found) *out = sp.item;
    return sp.found;
}

bool find_datum_plane(ProSolid s, const std::string& name, ProModelitem* surf_out) {
    // 1) 直接按名字找曲面（基准面的曲面名通常即基准名）
    ProName pn;
    to_proname(name, pn);
    // TODO(实机核对): ProModelitemByNameInit(ProMdl, ProType, ProName, ProModelitem*) 对基准面是否能用 PRO_SURFACE
    ProModelitem it;
    if (ProModelitemByNameInit(reinterpret_cast<ProMdl>(s), PRO_SURFACE, pn, &it) == PRO_TK_NO_ERROR) {
        *surf_out = it;
        return true;
    }
    // 2) 按特征名找基准特征，再取其曲面
    ProFeature f;
    if (find_feature(s, name, &f) && datum_surface_of_feature(&f, surf_out)) return true;
    // 3) Creo 默认模板基准面特征名为 DTM1/DTM2/DTM3（对应 RIGHT/TOP/FRONT）时的别名
    //    TODO(实机核对): 不同模板顺序可能不同；Creo 10 英文模板直接叫 RIGHT/TOP/FRONT，此分支多为兼容旧模板
    std::string alias;
    if (name == "RIGHT") alias = "DTM1";
    else if (name == "TOP") alias = "DTM2";
    else if (name == "FRONT") alias = "DTM3";
    if (!alias.empty() && find_feature(s, alias, &f) && datum_surface_of_feature(&f, surf_out)) return true;
    return false;
}

// ====================== 图纸 ======================

double sheet_mm_factor(ProDrawing d) {
    int sheet = 1;
    ProDrawingCurrentSheetGet(d, &sheet);  // 失败则按第 1 页
    ProUnititem su;
    // TODO(实机核对): ProDrawingSheetUnitsGet(ProDrawing, int sheet, ProUnititem*) 的签名与是否存在；
    //   失败时回落为图纸模型自身的主单位制（图纸单位一般等于其主单位）
    if (ProDrawingSheetUnitsGet(d, sheet, &su) == PRO_TK_NO_ERROR) {
        ProUnititem mm;
        unit_mm(reinterpret_cast<ProMdl>(d), &mm);
        ProUnitConversion conv;
        check(ProUnitConversionGet(&mm, &conv, &su), "ProUnitConversionGet(sheet)");
        return conv.scale;
    }
    return model_mm_factor(reinterpret_cast<ProMdl>(d));
}

void sheet_mm_to_drw(ProDrawing d, const std::array<double, 2>& mm, ProPoint3d out) {
    // TODO(实机核对): 假定图纸坐标原点在图纸左下角（Creo 绘图“屏幕坐标”约定），单位为图纸单位
    double f = sheet_mm_factor(d);
    out[0] = mm[0] * f;
    out[1] = mm[1] * f;
    out[2] = 0.0;
}

static std::map<std::string, ProView>& view_aliases() {
    static std::map<std::string, ProView> m;
    return m;
}

void register_view_alias(const std::string& alias, ProView v) { view_aliases()[alias] = v; }

ProView find_view(ProDrawing d, const std::string& view_name) {
    auto it = view_aliases().find(view_name);  // 别名优先（TOOLKIT 建的视图名由 Creo 指定，不一定能改）
    if (it != view_aliases().end()) return it->second;
    ProView* views = nullptr;
    // TODO(实机核对): ProDrawingViewsCollect(ProDrawing, ProView** array)；用 ProArrayFree 释放
    check(ProDrawingViewsCollect(d, &views), "ProDrawingViewsCollect");
    int n = 0;
    ProArraySizeGet(views, &n);
    ProView result;
    bool ok = false;
    for (int i = 0; i < n && !ok; ++i) {
        ProName nm;
        // TODO(实机核对): ProDrawingViewNameGet(ProDrawing, ProView, ProName)
        if (ProDrawingViewNameGet(d, views[i], nm) == PRO_TK_NO_ERROR && narrow(nm) == view_name) {
            result = views[i];
            ok = true;
        }
    }
    ProArrayFree(reinterpret_cast<ProArray*>(&views));
    if (!ok) throw CmdError("PRO_TK_E_NOT_FOUND", "找不到视图: " + view_name);
    return result;
}

// ---- 边拾取 ----

struct PickCtx {
    ProMatrix mat;      // 模型 → 图纸（行向量约定：p' = [x y z 1] * M）
    double px, py;      // 目标点（图纸单位）
    ProSolid solid;
    double best = 1e300;
    ProEdge best_edge;
    bool found = false;
};

static void project(const ProMatrix m, const double p[3], double* x, double* y) {
    *x = p[0] * m[0][0] + p[1] * m[1][0] + p[2] * m[2][0] + m[3][0];
    *y = p[0] * m[0][1] + p[1] * m[1][1] + p[2] * m[2][1] + m[3][1];
}

static double dist_pt_seg(double px, double py, double ax, double ay, double bx, double by) {
    double dx = bx - ax, dy = by - ay;
    double l2 = dx * dx + dy * dy;
    double t = l2 > 0 ? ((px - ax) * dx + (py - ay) * dy) / l2 : 0.0;
    t = std::max(0.0, std::min(1.0, t));
    double cx = ax + t * dx - px, cy = ay + t * dy - py;
    return std::sqrt(cx * cx + cy * cy);
}

static ProError edge_cb(ProEdge edge, ProError, ProAppData d) {
    auto* c = static_cast<PickCtx*>(d);
    const int N = 24;  // 每条边采样点数；直线/圆弧足够
    double px = 0, py = 0, ax = 0, ay = 0;
    double mind = 1e300;
    for (int i = 0; i <= N; ++i) {
        double t = static_cast<double>(i) / N;
        ProVector xyz, d1, d2, nrm;
        // TODO(实机核对): ProEdgeXyzdataEval 的参数表（此处：edge, 归一化参数 t∈[0,1], 点, 一阶导, 二阶导, 法向）
        if (ProEdgeXyzdataEval(edge, t, xyz, d1, d2, nrm) != PRO_TK_NO_ERROR) return PRO_TK_NO_ERROR;
        project(c->mat, xyz, &px, &py);
        if (i > 0) mind = std::min(mind, dist_pt_seg(c->px, c->py, ax, ay, px, py));
        ax = px;
        ay = py;
    }
    if (mind < c->best) {
        c->best = mind;
        c->best_edge = edge;
        c->found = true;
    }
    return PRO_TK_NO_ERROR;
}

static ProError contour_cb(ProContour contour, ProError, ProAppData d) {
    auto* c = static_cast<PickCtx*>(d);
    // TODO(实机核对): ProContourEdgeVisit(ProSolid, ProContour, visit, filter, appdata)
    ProContourEdgeVisit(c->solid, contour, edge_cb, nullptr, d);
    return PRO_TK_NO_ERROR;
}

static ProError surface_cb(ProSurface surf, ProError, ProAppData d) {
    ProSurfaceContourVisit(surf, contour_cb, nullptr, d);
    return PRO_TK_NO_ERROR;
}

ProSelection pick_edge_at(ProDrawing d, ProView v, const std::array<double, 2>& sheet_mm, double tol_mm) {
    PickCtx ctx;
    // 视图对应的模型
    // TODO(实机核对): ProDrawingViewSolidGet? 取视图所显示的 solid；首期只支持零件
    ProSolid solid = nullptr;
    check(ProDrawingViewSolidGet(d, v, &solid), "ProDrawingViewSolidGet");
    if (model_type_name(reinterpret_cast<ProMdl>(solid)) != "part")
        // TODO: 装配需遍历组件并为每个组件构造 ProAsmcomppath 传给 ProSelectionAlloc
        throw CmdError("PRO_TK_NOT_IMPLEMENTED", "pick_edge_at 首期仅支持零件视图");
    ctx.solid = solid;

    // TODO(实机核对): ProDrawingViewTransformGet(ProDrawing, ProView, ProBoolean, ProMatrix)；
    //   假定输出把模型坐标（模型单位）变换到图纸单位坐标，行向量约定
    check(ProDrawingViewTransformGet(d, v, PRO_B_FALSE, ctx.mat), "ProDrawingViewTransformGet");

    double f = sheet_mm_factor(d);
    ctx.px = sheet_mm[0] * f;
    ctx.py = sheet_mm[1] * f;

    // TODO(实机核对): ProSolidSurfaceVisit(ProSolid, visit, filter, appdata)
    check(ProSolidSurfaceVisit(solid, surface_cb, nullptr, &ctx), "ProSolidSurfaceVisit");

    if (!ctx.found || ctx.best > tol_mm * f)
        throw CmdError("PRO_TK_E_NOT_FOUND", "视图内该点附近没有模型边（容差 " + std::to_string(tol_mm) + " mm）");

    ProSelection sel = nullptr;
    // TODO(实机核对): ProSelectionAlloc(ProAsmcomppath*, ProModelitem*, ProSelection*)，零件传 NULL 路径
    check(ProSelectionAlloc(nullptr, reinterpret_cast<ProModelitem*>(&ctx.best_edge), &sel), "ProSelectionAlloc");
    // TODO(实机核对): ProSelectionViewSet(ProSelection, ProView)
    ProError e = ProSelectionViewSet(sel, v);
    if (e != PRO_TK_NO_ERROR) {
        ProSelectionFree(&sel);
        check(e, "ProSelectionViewSet");
    }
    return sel;
}

}  // namespace cadmcp
