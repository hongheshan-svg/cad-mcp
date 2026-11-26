// 建模命令（docs/CREO_TOOLKIT.md §3.2）：拉伸、旋转、孔、圆角/倒角、基准面/轴/点/坐标系。
//
// 坐标约定（关键）：协议用平台 IR 坐标（Z 向上，mm）。Creo 默认 Y 向上，因此在边界做刚体旋转
//   IR (x, y, z)  →  Creo (x, z, −y)
// 这样 IR 的 TOP(法向 +Z) / FRONT(法向 −Y) / RIGHT(法向 +X) 恰好落在 Creo 默认基准面 TOP(+Y) / FRONT(+Z) / RIGHT(+X) 上，
// CREOSON 用命名视图 FRONT/TOP/RIGHT 建的工程图视图也随之与规划一致。
//
// 草绘：不依赖草绘器 H/V 方向的约定——用 ProSectionLocationGet 取"截面坐标 → 模型坐标"矩阵并求逆，
// 把 (u, v) 先按协议映射成 IR 三维点 → Creo 三维点 → 截面二维坐标。
//
// 元素树按 TOOLKIT 用户指南 "Extrude / Revolve / Round / Chamfer / Datum" 章节与示例（UgExtrudeCreate 等）书写；
// 未能在本机编译核对的地方标 TODO(实机核对)。
#define _USE_MATH_DEFINES  // MSVC 的 M_PI
#include <algorithm>
#include <array>
#include <cmath>
#include <string>
#include <vector>

#include "util.h"

extern "C" {
#include <ProElement.h>
#include <ProElemId.h>
#include <ProFeatType.h>
#include <ProFeatForm.h>
#include <ProExtrude.h>
#include <ProRevolve.h>
#include <ProStdSection.h>
#include <ProSection.h>
#include <ProSecdim.h>
#include <ProFeature.h>
#include <ProDtmPln.h>
#include <ProDtmAxis.h>
#include <ProDtmPnt.h>
#include <ProDtmCsys.h>
#include <ProRound.h>
#include <ProChamfer.h>
#include <ProSurface.h>
#include <ProContour.h>
#include <ProEdge.h>
#include <ProCsys.h>
#include <ProGeomitem.h>
#include <ProReference.h>
#include <ProArray.h>
#include <ProUtil.h>
#include <ProThread.h>
#include <ProSurfacedata.h>
#include <ProGeomitemdata.h>
#include <ProElempath.h>
}

namespace cadmcp {
namespace {

using V3 = std::array<double, 3>;

// ---------------- 坐标 ----------------
V3 ir_to_creo(const V3& p) { return {p[0], p[2], -p[1]}; }

struct PlaneFrame {
    std::string base;  // TOP | FRONT | RIGHT（IR 语义）或用户基准面名
    double offset = 0; // 沿 IR 法向的偏移（mm）
    V3 n{}, o{}, u{}, v{};  // IR：法向、原点、草绘 u/v 方向
};

// 协议 §3.2：TOP u=+X v=+Y 法向 +Z；FRONT u=+X v=+Z 法向 −Y；RIGHT u=+Y v=+Z 法向 +X
PlaneFrame frame_of(const std::string& base, double off) {
    PlaneFrame f;
    f.base = base;
    f.offset = off;
    if (base == "TOP") { f.n = {0, 0, 1}; f.u = {1, 0, 0}; f.v = {0, 1, 0}; }
    else if (base == "FRONT") { f.n = {0, -1, 0}; f.u = {1, 0, 0}; f.v = {0, 0, 1}; }
    else if (base == "RIGHT") { f.n = {1, 0, 0}; f.u = {0, 1, 0}; f.v = {0, 0, 1}; }
    else throw CmdError("BAD_ARGS", "plane.ref 首期只支持 TOP/FRONT/RIGHT（用户基准面需给出其坐标系，见 TODO）");
    f.o = {f.n[0] * off, f.n[1] * off, f.n[2] * off};
    return f;
}

V3 uv_to_ir(const PlaneFrame& f, double u, double v) {
    return {f.o[0] + f.u[0] * u + f.v[0] * v, f.o[1] + f.u[1] * u + f.v[1] * v, f.o[2] + f.u[2] * u + f.v[2] * v};
}

std::array<double, 2> ir_to_uv(const PlaneFrame& f, const V3& p) {
    V3 d{p[0] - f.o[0], p[1] - f.o[1], p[2] - f.o[2]};
    return {d[0] * f.u[0] + d[1] * f.u[1] + d[2] * f.u[2], d[0] * f.v[0] + d[1] * f.v[1] + d[2] * f.v[2]};
}

double dot(const V3& a, const V3& b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

// 面 → 孔口所在的草绘平面与切除方向（−面外法向）
V3 face_normal(const std::string& face) {
    if (face == "top") return {0, 0, 1};
    if (face == "bottom") return {0, 0, -1};
    if (face == "front") return {0, -1, 0};
    if (face == "back") return {0, 1, 0};
    if (face == "left") return {-1, 0, 0};
    if (face == "right") return {1, 0, 0};
    throw CmdError("BAD_ARGS", "face 应为 top/bottom/front/back/left/right：" + face);
}

std::string plane_for_face(const std::string& face) {
    if (face == "top" || face == "bottom") return "TOP";
    if (face == "front" || face == "back") return "FRONT";
    return "RIGHT";
}

// ---------------- 元素树小工具 ----------------
ProElement elem(ProElemId id) {
    ProElement e;
    check(ProElementAlloc(id, &e), "ProElementAlloc");
    return e;
}
ProElement add(ProElement parent, ProElement child) {
    check(ProElemtreeElementAdd(parent, NULL, child), "ProElemtreeElementAdd");
    return child;
}
ProElement add_int(ProElement parent, ProElemId id, int v) {
    ProElement e = elem(id);
    check(ProElementIntegerSet(e, v), "ProElementIntegerSet");
    return add(parent, e);
}
ProElement add_double(ProElement parent, ProElemId id, double v) {
    ProElement e = elem(id);
    check(ProElementDoubleSet(e, v), "ProElementDoubleSet");
    return add(parent, e);
}
ProElement add_ref(ProElement parent, ProElemId id, ProSelection sel) {
    ProElement e = elem(id);
    ProReference ref;
    check(ProSelectionToReference(sel, &ref), "ProSelectionToReference");
    check(ProElementReferenceSet(e, ref), "ProElementReferenceSet");  // TODO(实机核对): 设置后是否需 ProReferenceFree
    return add(parent, e);
}
ProElement add_wstr(ProElement parent, ProElemId id, const std::string& s) {
    ProElement e = elem(id);
    std::wstring w = widen(s);
    check(ProElementWstringSet(e, const_cast<wchar_t*>(w.c_str())), "ProElementWstringSet");
    return add(parent, e);
}

struct TreeGuard {  // 出错时释放元素树
    ProElement t;
    ~TreeGuard() { if (t) ProElementFree(&t); }
};

ProSelection model_selection(ProSolid s) {
    ProModelitem mi;
    check(ProMdlToModelitem(reinterpret_cast<ProMdl>(s), &mi), "ProMdlToModelitem");
    ProSelection sel;
    check(ProSelectionAlloc(NULL, &mi, &sel), "ProSelectionAlloc(model)");
    return sel;
}

ProSelection item_selection(ProModelitem* mi) {
    ProSelection sel;
    check(ProSelectionAlloc(NULL, mi, &sel), "ProSelectionAlloc(item)");
    return sel;
}

// 创建特征；incomplete=true 时创建不完整特征（用于随后填草绘再 Redefine）
ProFeature create_feature(ProSolid s, ProElement tree, bool incomplete) {
    ProSelection msel = model_selection(s);
    ProFeatureCreateOptions* opts = NULL;
    check(ProArrayAlloc(1, sizeof(ProFeatureCreateOptions), 1, reinterpret_cast<ProArray*>(&opts)), "ProArrayAlloc");
    opts[0] = incomplete ? PRO_FEAT_CR_INCOMPLETE_FEAT : PRO_FEAT_CR_NO_OPTS;
    ProFeature f;
    ProErrorlist errs;
    // TODO(实机核对): Creo 10 签名 ProFeatureWithoptionsCreate(ProSelection, ProElement, ProFeatureCreateOptions*, int flags, ProFeature*, ProErrorlist*)
    ProError e = ProFeatureWithoptionsCreate(msel, tree, opts, PRO_REGEN_NO_FLAGS, &f, &errs);
    ProArrayFree(reinterpret_cast<ProArray*>(&opts));
    ProSelectionFree(&msel);
    check(e, "ProFeatureWithoptionsCreate");
    return f;
}

void redefine_feature(ProFeature* f, ProElement tree) {
    ProFeatureCreateOptions* opts = NULL;
    check(ProArrayAlloc(1, sizeof(ProFeatureCreateOptions), 1, reinterpret_cast<ProArray*>(&opts)), "ProArrayAlloc");
    opts[0] = PRO_FEAT_CR_NO_OPTS;
    ProErrorlist errs;
    // TODO(实机核对): ProFeatureWithoptionsRedefine(ProSelection comp_model(装配上下文，零件传 NULL), ProFeature*, ProElement, options, flags, ProErrorlist*)
    ProError e = ProFeatureWithoptionsRedefine(NULL, f, tree, opts, PRO_REGEN_NO_FLAGS, &errs);
    ProArrayFree(reinterpret_cast<ProArray*>(&opts));
    check(e, "ProFeatureWithoptionsRedefine");
}

json created(ProFeature& f, const std::string& name) {
    set_feature_name(&f, name);
    return json{{"id", f.id}};
}

// ---------------- 基准面 ----------------
// IR 基准名 → Creo 默认基准面（见文件头：TOP/FRONT/RIGHT 同名）
ProModelitem base_plane(ProSolid s, const std::string& base) {
    ProModelitem surf;
    if (!find_datum_plane(s, base, &surf))
        throw CmdError("PRO_TK_E_NOT_FOUND", "找不到基准面 " + base + "（起始件需含默认基准面 TOP/FRONT/RIGHT）");
    return surf;
}

// 偏移基准面：Creo 偏移方向沿参照面的正法向；IR TOP/FRONT/RIGHT 的法向旋转后即 Creo TOP(+Y)/FRONT(+Z)/RIGHT(+X)
ProFeature make_offset_plane(ProSolid s, const std::string& base, double off_mm, const std::string& name) {
    double k = model_mm_factor(reinterpret_cast<ProMdl>(s));
    ProModelitem surf = base_plane(s, base);
    ProSelection ref = item_selection(&surf);
    TreeGuard g{elem(PRO_E_FEATURE_TREE)};
    add_int(g.t, PRO_E_FEATURE_TYPE, PRO_FEAT_DATUM);
    ProElement cons = add(g.t, elem(PRO_E_DTMPLN_CONSTRAINTS));
    ProElement c = add(cons, elem(PRO_E_DTMPLN_CONSTRAINT));
    add_int(c, PRO_E_DTMPLN_CONSTR_TYPE, PRO_DTMPLN_OFFS);
    add_ref(c, PRO_E_DTMPLN_CONSTR_REF, ref);
    // TODO(实机核对): 偏移正向是否为参照面的正法向（默认基准面通常是）；若反了把 off 取反
    add_double(c, PRO_E_DTMPLN_CONSTR_REF_OFFSET, off_mm * k);
    ProSelectionFree(&ref);
    ProFeature f = create_feature(s, g.t, false);
    set_feature_name(&f, name);
    return f;
}

// 取某基准特征的平面曲面（用于草绘/参照）
ProModelitem plane_of_feature(ProFeature* f) {
    struct Ctx { ProModelitem item; bool found = false; } ctx;
    auto cb = [](ProGeomitem* gi, ProError, ProAppData d) -> ProError {
        auto* c = static_cast<Ctx*>(d);
        c->item = *gi;
        c->found = true;
        return PRO_TK_E_FOUND;  // 停止遍历
    };
    // TODO(实机核对): ProFeatureGeomitemVisit(ProFeature*, ProType, visit, filter, appdata)
    ProFeatureGeomitemVisit(f, PRO_SURFACE, static_cast<ProGeomitemAction>(+cb), NULL, &ctx);
    if (!ctx.found) throw CmdError("PRO_TK_E_NOT_FOUND", "基准面特征没有平面几何");
    return ctx.item;
}

// 草绘平面：offset=0 直接用默认基准面，否则先建偏移基准面 <feature>_PLN
ProModelitem sketch_plane(ProSolid s, const PlaneFrame& fr, const std::string& feature) {
    if (std::fabs(fr.offset) < 1e-9) return base_plane(s, fr.base);
    ProFeature pf = make_offset_plane(s, fr.base, fr.offset, feature + "_PLN");
    return plane_of_feature(&pf);
}

// 草绘方向参照：TOP/FRONT 用 RIGHT（朝右），RIGHT 用 TOP（朝上）
void setup_plane(ProElement std_section, ProSolid s, ProModelitem plane, const PlaneFrame& fr) {
    ProElement setup = add(std_section, elem(PRO_E_STD_SEC_SETUP_PLANE));
    ProSelection psel = item_selection(&plane);
    add_ref(setup, PRO_E_STD_SEC_PLANE, psel);
    ProSelectionFree(&psel);
    add_int(setup, PRO_E_STD_SEC_PLANE_VIEW_DIR, PRO_SEC_VIEW_DIR_SIDE_ONE);
    bool right_ref = fr.base != "RIGHT";
    ProModelitem oref = base_plane(s, right_ref ? "RIGHT" : "TOP");
    add_int(setup, PRO_E_STD_SEC_PLANE_ORIENT_DIR, right_ref ? PRO_SEC_ORIENT_DIR_RIGHT : PRO_SEC_ORIENT_DIR_UP);
    ProSelection osel = item_selection(&oref);
    add_ref(setup, PRO_E_STD_SEC_PLANE_ORIENT_REF, osel);
    ProSelectionFree(&osel);
}

// ---------------- 草绘截面 ----------------
struct SecMap {  // Creo 模型坐标（模型单位）→ 截面二维坐标
    double inv[4][4];
    double k;  // mm → 模型单位
    bool mirrored = false;
};

void invert_rigid(const ProMatrix m, double out[4][4]) {
    // 刚体（可能带缩放 1）：R^T 与 −R^T t。ProMatrix 为行向量约定：前三行为轴，第 4 行为原点
    for (int i = 0; i < 3; ++i)
        for (int j = 0; j < 3; ++j) out[i][j] = m[j][i];
    for (int j = 0; j < 3; ++j) {
        out[3][j] = -(m[3][0] * m[j][0] + m[3][1] * m[j][1] + m[3][2] * m[j][2]);
        out[j][3] = 0;
    }
    out[3][3] = 1;
}

std::array<double, 2> to_sec(const SecMap& sm, const PlaneFrame& fr, double u, double v) {
    V3 c = ir_to_creo(uv_to_ir(fr, u, v));
    for (auto& x : c) x *= sm.k;
    // 行向量 × 矩阵
    double x = c[0] * sm.inv[0][0] + c[1] * sm.inv[1][0] + c[2] * sm.inv[2][0] + sm.inv[3][0];
    double y = c[0] * sm.inv[0][1] + c[1] * sm.inv[1][1] + c[2] * sm.inv[2][1] + sm.inv[3][1];
    return {x, y};
}

double ang_of(double x, double y) { return std::atan2(y, x); }

// entities：协议 §3.2 的 line/arc/circle（以及 revolve 用的中心线 centerline）
void fill_section(ProSection sec, const json& ents, const PlaneFrame& fr, double k, const json* axis) {
    ProMatrix m;
    // TODO(实机核对): ProSectionLocationGet(ProSection, ProMatrix) 返回截面坐标系 → 模型坐标系的变换
    check(ProSectionLocationGet(sec, m), "ProSectionLocationGet");
    SecMap sm;
    sm.k = k;
    invert_rigid(m, sm.inv);
    // 截面坐标系与 (u,v) 是否镜像：看 u×v 映射后的方向
    auto a = to_sec(sm, fr, 0, 0), b = to_sec(sm, fr, 1, 0), c = to_sec(sm, fr, 0, 1);
    double cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
    sm.mirrored = cross < 0;
    int id;
    if (axis) {
        auto p1 = arg_pt2(*axis, "p1"), p2 = arg_pt2(*axis, "p2");
        auto q1 = to_sec(sm, fr, p1[0], p1[1]), q2 = to_sec(sm, fr, p2[0], p2[1]);
        Pro2dClinedef cl;
        cl.type = PRO_2D_CENTER_LINE;
        cl.end1[0] = q1[0]; cl.end1[1] = q1[1];
        cl.end2[0] = q2[0]; cl.end2[1] = q2[1];
        check(ProSectionEntityAdd(sec, reinterpret_cast<Pro2dEntdef*>(&cl), &id), "ProSectionEntityAdd(centerline)");
    }
    for (const auto& e : ents) {
        std::string t = arg_str(e, "type");
        if (t == "line") {
            auto p1 = arg_pt2(e, "p1"), p2 = arg_pt2(e, "p2");
            auto q1 = to_sec(sm, fr, p1[0], p1[1]), q2 = to_sec(sm, fr, p2[0], p2[1]);
            Pro2dLinedef l;
            l.type = PRO_2D_LINE;
            l.end1[0] = q1[0]; l.end1[1] = q1[1];
            l.end2[0] = q2[0]; l.end2[1] = q2[1];
            check(ProSectionEntityAdd(sec, reinterpret_cast<Pro2dEntdef*>(&l), &id), "ProSectionEntityAdd(line)");
        } else if (t == "circle") {
            auto c0 = arg_pt2(e, "center");
            auto q = to_sec(sm, fr, c0[0], c0[1]);
            Pro2dCircledef ci;
            ci.type = PRO_2D_CIRCLE;
            ci.center[0] = q[0]; ci.center[1] = q[1];
            ci.radius = arg_num(e, "r") * k;
            check(ProSectionEntityAdd(sec, reinterpret_cast<Pro2dEntdef*>(&ci), &id), "ProSectionEntityAdd(circle)");
        } else if (t == "arc") {
            auto c0 = arg_pt2(e, "center");
            double r = arg_num(e, "r"), a0 = arg_num(e, "a0") * M_PI / 180, a1 = arg_num(e, "a1") * M_PI / 180;
            auto qc = to_sec(sm, fr, c0[0], c0[1]);
            auto qs = to_sec(sm, fr, c0[0] + r * std::cos(a0), c0[1] + r * std::sin(a0));
            auto qe = to_sec(sm, fr, c0[0] + r * std::cos(a1), c0[1] + r * std::sin(a1));
            double s0 = ang_of(qs[0] - qc[0], qs[1] - qc[1]), s1 = ang_of(qe[0] - qc[0], qe[1] - qc[1]);
            if (sm.mirrored) std::swap(s0, s1);  // 镜像后逆时针变顺时针：交换起止保持同一段弧
            while (s1 <= s0) s1 += 2 * M_PI;
            Pro2dArcdef ar;
            ar.type = PRO_2D_ARC;
            ar.center[0] = qc[0]; ar.center[1] = qc[1];
            ar.start_angle = s0;
            ar.end_angle = s1;
            ar.radius = r * k;
            check(ProSectionEntityAdd(sec, reinterpret_cast<Pro2dEntdef*>(&ar), &id), "ProSectionEntityAdd(arc)");
        } else {
            throw CmdError("BAD_ARGS", "未知草绘实体类型: " + t);
        }
    }
    ProWSecerror errs;
    check(ProSecerrorAlloc(&errs), "ProSecerrorAlloc");
    // TODO(实机核对): 自动标注 → 求解 → 再生；失败时 errs 中有逐条原因，可用 ProSecerrorMsgGet 取出
    ProError e1 = ProSectionAutodim(sec, &errs);
    ProError e2 = e1 == PRO_TK_NO_ERROR ? ProSectionSolve(sec, &errs) : e1;
    ProError e3 = e2 == PRO_TK_NO_ERROR ? ProSectionRegenerate(sec, &errs) : e2;
    ProSecerrorFree(&errs);
    check(e1, "ProSectionAutodim");
    check(e2, "ProSectionSolve");
    check(e3, "ProSectionRegenerate");
}

// 取出不完整特征的草绘截面，填入实体后 Redefine
void complete_sketch(ProFeature* f, const json& ents, const PlaneFrame& fr, double k, const json* axis) {
    ProElement tree;
    check(ProFeatureElemtreeExtract(f, NULL, PRO_FEAT_EXTRACT_NO_OPTS, &tree), "ProFeatureElemtreeExtract");
    TreeGuard g{tree};
    ProElempath path;
    check(ProElempathAlloc(&path), "ProElempathAlloc");
    ProElempathItem items[2];
    items[0].type = PRO_ELEM_PATH_ITEM_TYPE_ID; items[0].path_item.elem_id = PRO_E_STD_SECTION;
    items[1].type = PRO_ELEM_PATH_ITEM_TYPE_ID; items[1].path_item.elem_id = PRO_E_SKETCHER;
    check(ProElempathDataSet(path, items, 2), "ProElempathDataSet");
    ProElement sk;
    ProError e = ProElemtreeElementGet(tree, path, &sk);
    ProElempathFree(&path);
    check(e, "ProElemtreeElementGet(SKETCHER)");
    ProSection sec;
    check(ProElementSpecialvalueGet(sk, NULL, reinterpret_cast<ProAppData*>(&sec)), "ProElementSpecialvalueGet");
    fill_section(sec, ents, fr, k, axis);
    redefine_feature(f, tree);
}

// ---------------- 拉伸 / 旋转 ----------------
// dir_sign：+1 沿 IR 平面法向，−1 反向，0 两侧对称
ProFeature extrude(ProSolid s, const PlaneFrame& fr, const json& ents, double depth_mm, int dir_sign, bool cut,
                   bool through_all, const std::string& name) {
    double k = model_mm_factor(reinterpret_cast<ProMdl>(s));
    ProModelitem plane = sketch_plane(s, fr, name);
    TreeGuard g{elem(PRO_E_FEATURE_TREE)};
    add_int(g.t, PRO_E_FEATURE_TYPE, cut ? PRO_FEAT_CUT : PRO_FEAT_PROTRUSION);
    add_int(g.t, PRO_E_FEATURE_FORM, PRO_EXTRUDE);
    add_int(g.t, PRO_E_EXT_SURF_CUT_SOLID_TYPE, PRO_EXT_FEAT_TYPE_SOLID);
    add_int(g.t, PRO_E_REMOVE_MATERIAL, cut ? PRO_EXT_MATERIAL_REMOVE : PRO_EXT_MATERIAL_ADD);
    add_int(g.t, PRO_E_FEAT_FORM_IS_THIN, PRO_EXT_FEAT_FORM_NO_THIN);
    add_wstr(g.t, PRO_E_STD_FEATURE_NAME, name);
    ProElement ss = add(g.t, elem(PRO_E_STD_SECTION));
    setup_plane(ss, s, plane, fr);
    // 方向：SIDE_ONE = 草绘平面正法向一侧（视图方向 SIDE_ONE 时）。TODO(实机核对): 若方向相反，交换 SIDE_ONE/SIDE_TWO
    add_int(g.t, PRO_E_STD_DIRECTION, dir_sign >= 0 ? PRO_EXT_CR_IN_SIDE_ONE : PRO_EXT_CR_IN_SIDE_TWO);
    if (cut) add_int(g.t, PRO_E_STD_MATRLSIDE, PRO_EXT_MATERIAL_SIDE_ONE);  // 去除截面内部材料
    ProElement dep = add(g.t, elem(PRO_E_STD_EXT_DEPTH));
    ProElement from = add(dep, elem(PRO_E_EXT_DEPTH_FROM));
    add_int(from, PRO_E_EXT_DEPTH_FROM_TYPE, PRO_EXT_DEPTH_FROM_NONE);
    ProElement to = add(dep, elem(PRO_E_EXT_DEPTH_TO));
    if (through_all) {
        add_int(to, PRO_E_EXT_DEPTH_TO_TYPE, PRO_EXT_DEPTH_TO_ALL);
    } else if (dir_sign == 0) {
        add_int(to, PRO_E_EXT_DEPTH_TO_TYPE, PRO_EXT_DEPTH_SYMMETRIC);  // TODO(实机核对): 对称深度的枚举名
        add_double(to, PRO_E_EXT_DEPTH_TO_VALUE, depth_mm * k);
    } else {
        add_int(to, PRO_E_EXT_DEPTH_TO_TYPE, PRO_EXT_DEPTH_TO_BLIND);
        add_double(to, PRO_E_EXT_DEPTH_TO_VALUE, depth_mm * k);
    }
    ProFeature f = create_feature(s, g.t, true);
    complete_sketch(&f, ents, fr, k, nullptr);
    return f;
}

ProFeature revolve(ProSolid s, const PlaneFrame& fr, const json& ents, const json& axis, double angle_deg, bool cut,
                   const std::string& name) {
    double k = model_mm_factor(reinterpret_cast<ProMdl>(s));
    ProModelitem plane = sketch_plane(s, fr, name);
    TreeGuard g{elem(PRO_E_FEATURE_TREE)};
    add_int(g.t, PRO_E_FEATURE_TYPE, cut ? PRO_FEAT_CUT : PRO_FEAT_PROTRUSION);
    add_int(g.t, PRO_E_FEATURE_FORM, PRO_REVOLVE);
    add_int(g.t, PRO_E_EXT_SURF_CUT_SOLID_TYPE, PRO_REV_FEAT_TYPE_SOLID);
    add_int(g.t, PRO_E_REMOVE_MATERIAL, cut ? PRO_REV_MATERIAL_REMOVE : PRO_REV_MATERIAL_ADD);
    add_int(g.t, PRO_E_FEAT_FORM_IS_THIN, PRO_REV_FEAT_FORM_NO_THIN);
    add_wstr(g.t, PRO_E_STD_FEATURE_NAME, name);
    ProElement ss = add(g.t, elem(PRO_E_STD_SECTION));
    setup_plane(ss, s, plane, fr);
    if (cut) add_int(g.t, PRO_E_STD_MATRLSIDE, PRO_REV_MATERIAL_SIDE_ONE);
    // 旋转轴：草绘内部中心线。TODO(实机核对): PRO_E_REV_AXIS / PRO_E_REV_AXIS_OPT / PRO_REV_AXIS_INT_CLINE 的元素与枚举名
    ProElement ax = add(g.t, elem(PRO_E_REV_AXIS));
    add_int(ax, PRO_E_REV_AXIS_OPT, PRO_REV_AXIS_INT_CLINE);
    ProElement ang = add(g.t, elem(PRO_E_STD_REV_ANGLE));
    ProElement afrom = add(ang, elem(PRO_E_REV_ANG_FROM));
    add_int(afrom, PRO_E_REV_ANG_FROM_TYPE, PRO_REV_ANG_FROM_NONE);
    ProElement ato = add(ang, elem(PRO_E_REV_ANG_TO));
    add_int(ato, PRO_E_REV_ANG_TO_TYPE, PRO_REV_ANG_TO_TYPE_VARIABLE);
    add_double(ato, PRO_E_REV_ANG_TO_VAL, angle_deg);
    ProFeature f = create_feature(s, g.t, true);
    complete_sketch(&f, ents, fr, k, &axis);
    return f;
}

PlaneFrame plane_arg(const json& p) {  // p = args["plane"]
    if (!p.is_object()) throw CmdError("BAD_ARGS", "plane 应为 {ref, offset}");
    return frame_of(arg_str(p, "ref"), has(p, "offset") ? arg_num(p, "offset") : 0.0);
}

// ---------------- 边查找（圆角/倒角/沉头）----------------
struct EdgeHit {
    ProEdge edge = NULL;
    double d = 1e30;
    V3 q{};
};

ProError edge_cb(ProEdge e, ProError, ProAppData data) {
    auto* h = static_cast<EdgeHit*>(data);
    ProVector* pts = NULL;
    ProSurface adj[2];
    ProUvParam* uvs = NULL;
    int n = 0;
    // TODO(实机核对): ProEdgeTessellationGet(ProEdge, ProVector**, ProSurface[2], ProUvParam**, int*)
    if (ProEdgeTessellationGet(e, &pts, adj, &uvs, &n) != PRO_TK_NO_ERROR) return PRO_TK_NO_ERROR;
    for (int i = 0; i + 1 < n; ++i) {  // 点到折线段的距离
        V3 a{pts[i][0], pts[i][1], pts[i][2]}, b{pts[i + 1][0], pts[i + 1][1], pts[i + 1][2]};
        V3 ab{b[0] - a[0], b[1] - a[1], b[2] - a[2]}, aq{h->q[0] - a[0], h->q[1] - a[1], h->q[2] - a[2]};
        double L = dot(ab, ab), t = L > 0 ? std::max(0.0, std::min(1.0, dot(aq, ab) / L)) : 0;
        V3 c{a[0] + ab[0] * t - h->q[0], a[1] + ab[1] * t - h->q[1], a[2] + ab[2] * t - h->q[2]};
        double d = std::sqrt(dot(c, c));
        if (d < h->d) { h->d = d; h->edge = e; }
    }
    if (pts) ProArrayFree(reinterpret_cast<ProArray*>(&pts));
    if (uvs) ProArrayFree(reinterpret_cast<ProArray*>(&uvs));
    return PRO_TK_NO_ERROR;
}

// TODO(实机核对): ProContourEdgeVisit(ProSurface, ProContour, action, filter, data)
ProError surface_cb(ProSurface srf, ProError, ProAppData data) {
    auto* hit = static_cast<EdgeHit*>(data);
    struct C { ProSurface s; EdgeHit* h; } c{srf, hit};
    auto ccb = [](ProContour con, ProError, ProAppData d) -> ProError {
        auto* cc = static_cast<C*>(d);
        ProContourEdgeVisit(cc->s, con, edge_cb, NULL, cc->h);
        return PRO_TK_NO_ERROR;
    };
    ProSurfaceContourVisit(srf, static_cast<ProSurfaceContourVisitAction>(+ccb), NULL, &c);
    return PRO_TK_NO_ERROR;
}

// IR 点（mm）→ 最近的模型边，容差 tol_mm
ProEdge nearest_edge(ProSolid s, const V3& p_ir, double tol_mm) {
    double k = model_mm_factor(reinterpret_cast<ProMdl>(s));
    EdgeHit h;
    V3 c = ir_to_creo(p_ir);
    h.q = {c[0] * k, c[1] * k, c[2] * k};
    check(ProSolidSurfaceVisit(s, surface_cb, NULL, &h), "ProSolidSurfaceVisit");
    if (!h.edge || h.d > tol_mm * k)
        throw CmdError("PRO_TK_E_NOT_FOUND", "点附近找不到模型边（容差 " + std::to_string(tol_mm) + " mm）");
    return h.edge;
}

ProSelection edge_selection(ProSolid s, ProEdge e) {
    ProGeomitem gi;
    int id;
    check(ProEdgeIdGet(e, &id), "ProEdgeIdGet");
    check(ProModelitemInit(reinterpret_cast<ProMdl>(s), id, PRO_EDGE, &gi), "ProModelitemInit(edge)");
    return item_selection(&gi);
}

// 圆角/倒角共用：一组边一个集合。TODO(实机核对): RNDCH 元素树（ProRound.h / ProChamfer.h 中的元素与枚举名）
ProFeature round_or_chamfer(ProSolid s, const json& edges, double size_mm, bool chamfer, const std::string& name) {
    double k = model_mm_factor(reinterpret_cast<ProMdl>(s));
    ProReference* refs = NULL;
    check(ProArrayAlloc(0, sizeof(ProReference), 4, reinterpret_cast<ProArray*>(&refs)), "ProArrayAlloc");
    for (const auto& p : edges) {
        if (!p.is_array() || p.size() != 3) throw CmdError("BAD_ARGS", "edges 每项应为 [x,y,z]");
        V3 q{p[0].get<double>(), p[1].get<double>(), p[2].get<double>()};
        ProSelection sel = edge_selection(s, nearest_edge(s, q, 0.5));
        ProReference r;
        check(ProSelectionToReference(sel, &r), "ProSelectionToReference");
        ProArrayObjectAdd(reinterpret_cast<ProArray*>(&refs), PRO_VALUE_UNUSED, 1, &r);
        ProSelectionFree(&sel);
    }
    TreeGuard g{elem(PRO_E_FEATURE_TREE)};
    add_int(g.t, PRO_E_FEATURE_TYPE, chamfer ? PRO_FEAT_CHAMFER : PRO_FEAT_ROUND);
    add_wstr(g.t, PRO_E_STD_FEATURE_NAME, name);
    ProElement sets = add(g.t, elem(PRO_E_RNDCH_SETS));
    ProElement set = add(sets, elem(PRO_E_RNDCH_SET));
    ProElement refe = elem(PRO_E_RNDCH_REFERENCES);
    check(ProElementReferencesSet(refe, refs), "ProElementReferencesSet");
    add(set, refe);
    if (chamfer) {
        add_int(set, PRO_E_RNDCH_DIMENSIONAL_SCHEME, PRO_CHM_D_X_D);
        add_double(set, PRO_E_RNDCH_D1, size_mm * k);
    } else {
        ProElement rad = add(set, elem(PRO_E_RNDCH_RADIUS));
        ProElement leg = add(rad, elem(PRO_E_RNDCH_LEG1));
        add_int(leg, PRO_E_RNDCH_LEG_TYPE, PRO_ROUND_TYPE_VALUE);
        add_double(leg, PRO_E_RNDCH_LEG_VALUE, size_mm * k);
    }
    ProArrayFree(reinterpret_cast<ProArray*>(&refs));
    ProFeature f = create_feature(s, g.t, false);
    set_feature_name(&f, name);
    return f;
}

// ---------------- 坐标系 / 基准点 ----------------
ProSelection default_csys(ProSolid s) {
    struct C { ProGeomitem gi; bool ok = false; } c;
    ProFeature f;
    for (const char* n : {"PRT_CSYS_DEF", "ASM_DEF_CSYS", "CS0"}) {
        if (find_feature(s, n, &f)) {
            auto gcb = [](ProGeomitem* gi, ProError, ProAppData d) -> ProError {
                auto* cc = static_cast<C*>(d);
                cc->gi = *gi;
                cc->ok = true;
                return PRO_TK_E_FOUND;
            };
            ProFeatureGeomitemVisit(&f, PRO_CSYS, static_cast<ProGeomitemAction>(+gcb), NULL, &c);
            if (c.ok) return item_selection(&c.gi);
        }
    }
    throw CmdError("PRO_TK_E_NOT_FOUND", "找不到默认坐标系（PRT_CSYS_DEF）");
}

ProFeature datum_point(ProSolid s, const V3& at_ir, const std::string& name) {
    double k = model_mm_factor(reinterpret_cast<ProMdl>(s));
    V3 c = ir_to_creo(at_ir);
    ProSelection cs = default_csys(s);
    TreeGuard g{elem(PRO_E_FEATURE_TREE)};
    add_int(g.t, PRO_E_FEATURE_TYPE, PRO_FEAT_DATUM_POINT);
    add_int(g.t, PRO_E_DPOINT_TYPE, PRO_DPOINT_TYPE_OFFSET_CSYS);
    add_wstr(g.t, PRO_E_STD_FEATURE_NAME, name);
    // TODO(实机核对): 偏移坐标系基准点的元素名（PRO_E_DPOINT_OFST_CSYS_*）
    add_int(g.t, PRO_E_DPOINT_OFST_CSYS_TYPE, PRO_DTMPNT_OFFCSYS_CARTESIAN);
    add_ref(g.t, PRO_E_DPOINT_OFST_CSYS_REF, cs);
    ProSelectionFree(&cs);
    ProElement arr = add(g.t, elem(PRO_E_DPOINT_OFST_CSYS_PNTS_ARRAY));
    ProElement pt = add(arr, elem(PRO_E_DPOINT_OFST_CSYS_PNT));
    add_wstr(pt, PRO_E_DPOINT_OFST_CSYS_PNT_NAME, name);
    add_double(pt, PRO_E_DPOINT_OFST_CSYS_DIR1_VAL, c[0] * k);
    add_double(pt, PRO_E_DPOINT_OFST_CSYS_DIR2_VAL, c[1] * k);
    add_double(pt, PRO_E_DPOINT_OFST_CSYS_DIR3_VAL, c[2] * k);
    ProFeature f = create_feature(s, g.t, false);
    set_feature_name(&f, name);
    return f;
}

ProSelection first_geom(ProFeature* f, ProType t) {
    struct C { ProGeomitem gi; bool ok = false; } c;
    auto cb = [](ProGeomitem* gi, ProError, ProAppData d) -> ProError {
        auto* cc = static_cast<C*>(d);
        cc->gi = *gi;
        cc->ok = true;
        return PRO_TK_E_FOUND;
    };
    ProFeatureGeomitemVisit(f, t, static_cast<ProGeomitemAction>(+cb), NULL, &c);
    if (!c.ok) throw CmdError("PRO_TK_E_NOT_FOUND", "特征中没有所需几何");
    return item_selection(&c.gi);
}

// ---------------- 命令 ----------------
json cmd_extrude(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    std::string dir = has(a, "direction") ? arg_str(a, "direction") : "normal";
    int sign = dir == "reverse" ? -1 : dir == "both" ? 0 : 1;
    bool cut = (has(a, "op") ? arg_str(a, "op") : "add") == "cut";
    bool all = has(a, "through_all") && a["through_all"].get<bool>();
    double depth = all ? 0.0 : arg_num(a, "depth");
    ProFeature f = extrude(s, plane_arg(a.at("plane")), a.at("entities"), depth, sign, cut, all, arg_str(a, "feature"));
    return created(f, arg_str(a, "feature"));
}

json cmd_revolve(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    bool cut = (has(a, "op") ? arg_str(a, "op") : "add") == "cut";
    double ang = has(a, "angle") ? arg_num(a, "angle") : 360.0;
    ProFeature f = revolve(s, plane_arg(a.at("plane")), a.at("entities"), a.at("axis"), ang, cut, arg_str(a, "feature"));
    return created(f, arg_str(a, "feature"));
}

// 孔：每一级（沉孔、钻孔）一个拉伸切除，同组全部孔口圆放在同一草图；沉头用 90° 倒角（D×D，D=(csink−d)/2）
json cmd_hole(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    std::string face = arg_str(a, "face"), name = arg_str(a, "feature");
    V3 fn = face_normal(face);
    std::string base = plane_for_face(face);
    const json& pts = a.at("points");
    if (!pts.is_array() || pts.empty()) throw CmdError("BAD_ARGS", "points 应为非空的 [x,y,z] 列表");
    std::vector<V3> centers;
    for (const auto& p : pts) centers.push_back({p[0].get<double>(), p[1].get<double>(), p[2].get<double>()});
    PlaneFrame probe = frame_of(base, 0);
    double off = dot(centers[0], probe.n);  // 孔口所在平面沿 IR 法向的偏移
    PlaneFrame fr = frame_of(base, off);
    int sign = dot(V3{-fn[0], -fn[1], -fn[2]}, fr.n) > 0 ? 1 : -1;  // 切向材料内部
    double d = arg_num(a, "diameter");
    bool through = has(a, "through") && a["through"].get<bool>();
    double depth = through ? 0.0 : arg_num(a, "depth");
    auto circles = [&](double r) {
        json ents = json::array();
        for (const auto& c : centers) {
            auto uv = ir_to_uv(fr, c);
            ents.push_back({{"type", "circle"}, {"center", {uv[0], uv[1]}}, {"r", r}});
        }
        return ents;
    };
    json result{{"features", json::array()}};
    if (has(a, "cbore")) {
        double cd = a["cbore"][0].get<double>(), ch = a["cbore"][1].get<double>();
        ProFeature fc = extrude(s, fr, circles(cd / 2), ch, sign, true, false, name + "_CB");
        set_feature_name(&fc, name + "_CB");
        result["features"].push_back(fc.id);
    }
    ProFeature fd = extrude(s, fr, circles(d / 2), depth, sign, true, through, name);
    set_feature_name(&fd, name);
    result["id"] = fd.id;
    result["features"].push_back(fd.id);
    if (has(a, "csink")) {
        double cs = arg_num(a, "csink");
        json edges = json::array();
        for (const auto& c : centers) {  // 孔口圆边上的一点
            auto uv = ir_to_uv(fr, c);
            V3 q = uv_to_ir(fr, uv[0] + d / 2, uv[1]);
            edges.push_back({q[0], q[1], q[2]});
        }
        ProFeature fk = round_or_chamfer(s, edges, (cs - d) / 2, true, name + "_CSK");
        result["features"].push_back(fk.id);
    }
    if (has(a, "drill_point") && a["drill_point"].get<bool>())
        result["drill_point"] = "omitted";  // 首期不建 118° 钻尖（工程图剖视由规划层画出）
    return result;
}

// ---------------- 装饰螺纹（ProThread.h 元素树）----------------
// PRO_E_FEATURE_TREE
//   PRO_E_FEATURE_TYPE = PRO_FEAT_THREAD
//   PRO_E_STD_FEATURE_NAME
//   PRO_E_THRD_TYPE_OPT_NEW = FALSE（简单螺纹）
//   PRO_E_THRD_SURF_NEW（螺纹圆柱面）/ PRO_E_THRD_DIAM_NEW（大径）/ PRO_E_THRD_START_REF_NEW（起始面）
//   PRO_E_THRD_DEP_COMP_NEW { PRO_E_THRD_DEP_OPT_NEW = FALSE（盲）, PRO_E_THRD_FLIP_OPT_NEW, PRO_E_THRD_DEP_VAL_NEW }
// 我们的孔是"拉伸切除"（cmd_hole），同一特征含多个孔：每个孔（按圆柱轴线去重，Creo 把圆柱面分成两半）建一个螺纹特征。
// 起始面 = 该拉伸特征的草绘平面（默认基准面或 <孔>_PLN 偏移基准面，均在孔口处）。

ProElement add_bool(ProElement parent, ProElemId id, bool v) {
    ProElement e = elem(id);
    // TODO(实机核对): ProElementBooleanSet（Creo 4.0+）；老版本用 ProElementIntegerSet(0/1)
    check(ProElementBooleanSet(e, v ? PRO_B_TRUE : PRO_B_FALSE), "ProElementBooleanSet");
    return add(parent, e);
}

struct HoleCyl {
    ProGeomitem surf;
    V3 axis_pt;  // 轴线上的点（模型单位），用于去重
    V3 dir;
};

// 拉伸切除特征中的圆柱面，按轴线去重（每个孔一个）
std::vector<HoleCyl> hole_cylinders(ProFeature* f) {
    struct Ctx { std::vector<HoleCyl> out; } ctx;
    auto cb = [](ProGeomitem* gi, ProError, ProAppData d) -> ProError {
        auto* c = static_cast<Ctx*>(d);
        ProSurface srf;
        if (ProGeomitemToSurface(gi, &srf) != PRO_TK_NO_ERROR) return PRO_TK_NO_ERROR;
        ProSrftype st;
        if (ProSurfaceTypeGet(srf, &st) != PRO_TK_NO_ERROR || st != PRO_SRF_CYL) return PRO_TK_NO_ERROR;
        ProGeomitemdata* gd = NULL;
        // TODO(实机核对): ProSurfaceDataGet(ProSurface, ProGeomitemdata**) + ProCylinderdataGet(ProSurfacedata*, e1, e2, e3, origin, &r)
        if (ProSurfaceDataGet(srf, &gd) != PRO_TK_NO_ERROR) return PRO_TK_NO_ERROR;
        ProVector e1, e2, e3;
        Pro3dPnt org;
        double r = 0;
        ProError e = ProCylinderdataGet(gd->data.p_surface_data, e1, e2, e3, org, &r);
        ProGeomitemdataFree(&gd);
        if (e != PRO_TK_NO_ERROR) return PRO_TK_NO_ERROR;
        V3 dir{e3[0], e3[1], e3[2]}, o{org[0], org[1], org[2]};
        double L = std::sqrt(dot(dir, dir));
        if (L <= 0) return PRO_TK_NO_ERROR;
        for (auto& x : dir) x /= L;
        // 投影到垂直轴线的平面：去掉沿轴分量，同一孔的两半圆柱面得到同一点
        double s = dot(o, dir);
        V3 q{o[0] - dir[0] * s, o[1] - dir[1] * s, o[2] - dir[2] * s};
        for (const auto& h : c->out) {
            V3 dq{h.axis_pt[0] - q[0], h.axis_pt[1] - q[1], h.axis_pt[2] - q[2]};
            if (std::fabs(std::fabs(dot(h.dir, dir)) - 1) < 1e-6 && dot(dq, dq) < 1e-8) return PRO_TK_NO_ERROR;
        }
        c->out.push_back({*gi, q, dir});
        return PRO_TK_NO_ERROR;
    };
    check(ProFeatureGeomitemVisit(f, PRO_SURFACE, static_cast<ProGeomitemAction>(+cb), NULL, &ctx),
          "ProFeatureGeomitemVisit(surface)");
    return ctx.out;
}

// 拉伸特征的草绘平面参照（PRO_E_STD_SECTION/PRO_E_STD_SEC_SETUP_PLANE/PRO_E_STD_SEC_PLANE）
ProSelection sketch_plane_selection(ProFeature* f) {
    ProElement tree;
    check(ProFeatureElemtreeExtract(f, NULL, PRO_FEAT_EXTRACT_NO_OPTS, &tree), "ProFeatureElemtreeExtract");
    TreeGuard g{tree};
    ProElempath path;
    check(ProElempathAlloc(&path), "ProElempathAlloc");
    ProElempathItem items[3];
    items[0].type = items[1].type = items[2].type = PRO_ELEM_PATH_ITEM_TYPE_ID;
    items[0].path_item.elem_id = PRO_E_STD_SECTION;
    items[1].path_item.elem_id = PRO_E_STD_SEC_SETUP_PLANE;
    items[2].path_item.elem_id = PRO_E_STD_SEC_PLANE;
    check(ProElempathDataSet(path, items, 3), "ProElempathDataSet");
    ProElement pe;
    ProError e = ProElemtreeElementGet(tree, path, &pe);
    ProElempathFree(&path);
    check(e, "ProElemtreeElementGet(STD_SEC_PLANE)");
    ProReference ref;
    check(ProElementReferenceGet(pe, NULL, &ref), "ProElementReferenceGet");  // TODO(实机核对): 第二参数 options 传 NULL
    ProSelection sel;
    check(ProReferenceToSelection(ref, &sel), "ProReferenceToSelection");
    ProReferenceFree(ref);
    return sel;
}

ProFeature make_thread(ProSolid s, ProGeomitem* cyl, ProSelection start, double dia_mm, double len_mm,
                       const std::string& name, int flip) {
    double k = model_mm_factor(reinterpret_cast<ProMdl>(s));
    ProSelection ssel = item_selection(cyl);
    TreeGuard g{elem(PRO_E_FEATURE_TREE)};
    add_int(g.t, PRO_E_FEATURE_TYPE, PRO_FEAT_THREAD);
    add_wstr(g.t, PRO_E_STD_FEATURE_NAME, name);
    add_bool(g.t, PRO_E_THRD_TYPE_OPT_NEW, false);
    add_ref(g.t, PRO_E_THRD_SURF_NEW, ssel);
    ProSelectionFree(&ssel);
    add_double(g.t, PRO_E_THRD_DIAM_NEW, dia_mm * k);
    add_ref(g.t, PRO_E_THRD_START_REF_NEW, start);
    ProElement dep = add(g.t, elem(PRO_E_THRD_DEP_COMP_NEW));
    add_bool(dep, PRO_E_THRD_DEP_OPT_NEW, false);  // 盲螺纹
    add_int(dep, PRO_E_THRD_FLIP_OPT_NEW, flip);
    add_double(dep, PRO_E_THRD_DEP_VAL_NEW, len_mm * k);
    return create_feature(s, g.t, false);
}

json cmd_cosmetic_thread(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    std::string name = arg_str(a, "feature"), hole = arg_str(a, "hole_feature");
    double dia = arg_num(a, "major_diameter"), len = arg_num(a, "length");
    if (dia <= 0 || len <= 0) throw CmdError("BAD_ARGS", "major_diameter / length 必须为正");
    ProFeature hf;
    if (!find_feature(s, hole, &hf)) throw CmdError("PRO_TK_E_NOT_FOUND", "找不到孔特征 " + hole);
    std::vector<HoleCyl> cyls = hole_cylinders(&hf);
    if (cyls.empty()) throw CmdError("PRO_TK_E_NOT_FOUND", "孔特征 " + hole + " 中没有圆柱面");
    ProSelection start = sketch_plane_selection(&hf);
    json ids = json::array();
    try {
        for (size_t i = 0; i < cyls.size(); ++i) {
            std::string nm = i == 0 ? name : name + "_" + std::to_string(i + 1);
            ProFeature f;
            // 螺纹深度方向：先按起始面正法向，失败再反向（起始面是孔口处的草绘平面，深度应指向材料内部）
            // TODO(实机核对): 哪个方向先成功；若总是 FLIP 成功，可直接按孔切除方向决定
            try {
                f = make_thread(s, &cyls[i].surf, start, dia, len, nm, PRO_COSTHREAD_THD_FLIP_OPT_NORM);
            } catch (const CmdError&) {
                f = make_thread(s, &cyls[i].surf, start, dia, len, nm, PRO_COSTHREAD_THD_FLIP_OPT_FLIP);
            }
            set_feature_name(&f, nm);
            ids.push_back(f.id);
        }
    } catch (...) {
        ProSelectionFree(&start);
        throw;
    }
    ProSelectionFree(&start);
    return json{{"id", ids[0]}, {"features", ids}};
}

json cmd_round(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    ProFeature f = round_or_chamfer(s, a.at("edges"), arg_num(a, "radius"), false, arg_str(a, "feature"));
    return json{{"id", f.id}};
}

json cmd_chamfer(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    ProFeature f = round_or_chamfer(s, a.at("edges"), arg_num(a, "distance"), true, arg_str(a, "feature"));
    return json{{"id", f.id}};
}

json cmd_datum_plane(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    ProFeature f = make_offset_plane(s, arg_str(a, "ref"), has(a, "offset") ? arg_num(a, "offset") : 0.0,
                                     arg_str(a, "feature"));
    return json{{"id", f.id}};
}

json cmd_datum_point(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    ProFeature f = datum_point(s, arg_pt3(a, "at"), arg_str(a, "feature"));
    return json{{"id", f.id}};
}

// 轴：先建两个辅助基准点 <名>_P1/_P2，再建穿过两点的轴
json cmd_datum_axis(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    std::string name = arg_str(a, "feature");
    ProFeature p1 = datum_point(s, arg_pt3(a, "p1"), name + "_P1");
    ProFeature p2 = datum_point(s, arg_pt3(a, "p2"), name + "_P2");
    TreeGuard g{elem(PRO_E_FEATURE_TREE)};
    add_int(g.t, PRO_E_FEATURE_TYPE, PRO_FEAT_DATUM_AXIS);
    add_wstr(g.t, PRO_E_STD_FEATURE_NAME, name);
    ProElement cons = add(g.t, elem(PRO_E_DTMAXIS_CONSTRAINTS));
    for (ProFeature* pf : {&p1, &p2}) {
        ProSelection ps = first_geom(pf, PRO_POINT);
        ProElement c = add(cons, elem(PRO_E_DTMAXIS_CONSTRAINT));
        // TODO(实机核对): 穿过点约束的枚举名 PRO_DTMAXIS_CONSTR_TYPE_THRU
        add_int(c, PRO_E_DTMAXIS_CONSTR_TYPE, PRO_DTMAXIS_CONSTR_TYPE_THRU);
        add_ref(c, PRO_E_DTMAXIS_CONSTR_REF, ps);
        ProSelectionFree(&ps);
    }
    ProFeature f = create_feature(s, g.t, false);
    set_feature_name(&f, name);
    return json{{"id", f.id}};
}

// 坐标系：相对默认坐标系平移 + 依次绕 X、Y、Z 旋转（Creo 坐标），由 x_dir / y_dir 求欧拉角
json cmd_datum_csys(const json& a) {
    ProSolid s = reinterpret_cast<ProSolid>(find_part(arg_str(a, "model")));
    double k = model_mm_factor(reinterpret_cast<ProMdl>(s));
    std::string name = arg_str(a, "feature");
    V3 o = ir_to_creo(arg_pt3(a, "origin"));
    V3 x = ir_to_creo(arg_pt3(a, "x_dir")), y = ir_to_creo(arg_pt3(a, "y_dir"));
    auto norm = [](V3 v) { double L = std::sqrt(dot(v, v)); return V3{v[0] / L, v[1] / L, v[2] / L}; };
    x = norm(x);
    y = norm(y);
    V3 z{x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]};
    // R = [x y z]（列）= Rx(a)·Ry(b)·Rz(c)（绕动轴依次 X、Y、Z）→ b = asin(R02)，a = atan2(−R12, R22)，c = atan2(−R01, R00)
    double R00 = x[0], R01 = y[0], R02 = z[0], R12 = z[1], R22 = z[2];
    double rb = std::asin(std::max(-1.0, std::min(1.0, R02)));
    double ra = std::atan2(-R12, R22), rc = std::atan2(-R01, R00);
    ProSelection cs = default_csys(s);
    TreeGuard g{elem(PRO_E_FEATURE_TREE)};
    add_int(g.t, PRO_E_FEATURE_TYPE, PRO_FEAT_CSYS);
    add_wstr(g.t, PRO_E_STD_FEATURE_NAME, name);
    // TODO(实机核对): 坐标系元素树（PRO_E_CSYS_ORIGIN_CONSTRS / PRO_E_CSYS_OFFSET_TYPE / PRO_E_CSYS_ORIENTMOVES）
    ProElement ocs = add(g.t, elem(PRO_E_CSYS_ORIGIN_CONSTRS));
    ProElement oc = add(ocs, elem(PRO_E_CSYS_ORIGIN_CONSTR));
    add_ref(oc, PRO_E_CSYS_ORIGIN_CONSTR_REF, cs);
    ProSelectionFree(&cs);
    add_int(g.t, PRO_E_CSYS_OFFSET_TYPE, PRO_CSYS_OFFSET_CARTESIAN);
    ProElement moves = add(g.t, elem(PRO_E_CSYS_ORIENTMOVES));
    struct Mv { int type; double val; };
    const Mv mv[] = {{PRO_CSYS_ORIENTMOVE_MOVE_OPT_TRAN_X, o[0] * k}, {PRO_CSYS_ORIENTMOVE_MOVE_OPT_TRAN_Y, o[1] * k},
                     {PRO_CSYS_ORIENTMOVE_MOVE_OPT_TRAN_Z, o[2] * k}, {PRO_CSYS_ORIENTMOVE_MOVE_OPT_ROT_X, ra * 180 / M_PI},
                     {PRO_CSYS_ORIENTMOVE_MOVE_OPT_ROT_Y, rb * 180 / M_PI}, {PRO_CSYS_ORIENTMOVE_MOVE_OPT_ROT_Z, rc * 180 / M_PI}};
    for (const auto& m : mv) {
        if (std::fabs(m.val) < 1e-12) continue;
        ProElement e = add(moves, elem(PRO_E_CSYS_ORIENTMOVE));
        add_int(e, PRO_E_CSYS_ORIENTMOVE_MOVE_TYPE, m.type);
        add_double(e, PRO_E_CSYS_ORIENTMOVE_MOVE_VAL, m.val);
    }
    ProFeature f = create_feature(s, g.t, false);
    set_feature_name(&f, name);
    return json{{"id", f.id}};
}

}  // namespace

void register_model_cmds() {
    register_cmd("feat.extrude", cmd_extrude);
    register_cmd("feat.revolve", cmd_revolve);
    register_cmd("feat.hole", cmd_hole);
    register_cmd("feat.cosmetic_thread", cmd_cosmetic_thread);
    register_cmd("feat.round", cmd_round);
    register_cmd("feat.chamfer", cmd_chamfer);
    register_cmd("datum.plane", cmd_datum_plane);
    register_cmd("datum.axis", cmd_datum_axis);
    register_cmd("datum.point", cmd_datum_point);
    register_cmd("datum.csys", cmd_datum_csys);
}

}  // namespace cadmcp
