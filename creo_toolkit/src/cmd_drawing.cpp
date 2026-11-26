// 出图细节命令（协议见 docs/CREO_TOOLKIT.md §3.3）。
// 视图由 CREOSON 创建，本文件只做：注释、尺寸、基准、形位公差、粗糙度、表格、剖视、球标、回读。
// 约定：入参图纸坐标为 mm、左下角原点；用 sheet_mm_to_drw 换算成图纸单位坐标。
// 注意：本机无 TOOLKIT 头文件，API 名称凭文档记忆编写；不确定处统一标 TODO(实机核对)。
// 命令处理函数的参数名统一为 a，且只在处理函数内读取参数键（tests/test_toolkit_drawing_cmds.py 依赖此约定）。
extern "C" {
#include <ProToolkit.h>
#include <ProMdl.h>
#include <ProSolid.h>
#include <ProDrawing.h>
#include <ProDtlnote.h>
#include <ProDtlattach.h>
#include <ProDtlsyminst.h>
#include <ProDwgtable.h>
#include <ProDimension.h>
#include <ProGtol.h>
#include <ProSelection.h>
#include <ProModelitem.h>
#include <ProXsec.h>
#include <ProAxis.h>
#include <ProAnnotation.h>
#include <ProArray.h>
#include <ProUtil.h>
#include <ProDrawingView.h>
#include <ProEdge.h>
#include <ProPoint.h>
#include <ProCurvedata.h>
#include <Pro2dEntdef.h>
}

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <cwchar>
#include <string>
#include <vector>

#include "registry.h"
#include "util.h"

namespace cadmcp {
namespace {

using Pt = std::array<double, 2>;

// 回读视图名回调用的上下文（服务单线程，安全）
ProDrawing g_readback_drawing = nullptr;

// ---------- 通用小工具 ----------

// 图纸 mm 长度 → 图纸单位长度（图纸单位与模型单位换算同 model_mm_factor）
double mm_to_drw_len(ProDrawing d, double mm) { return mm * model_mm_factor((ProMdl)d); }

// UTF-8 按 '\n' 切行
std::vector<std::string> split_lines(const std::string& s) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : s) {
        if (c == '\n') { out.push_back(cur); cur.clear(); }
        else if (c != '\r') cur.push_back(c);
    }
    out.push_back(cur);
    return out;
}

// 构造 ProDtlattach：free（无引线，位置即文字位置）
ProDtlattach make_free_attach(ProDrawing d, const Pt& pos_mm) {
    ProPoint3d p;
    sheet_mm_to_drw(d, pos_mm, p);
    ProDtlattach att = nullptr;
    // TODO(实机核对): ProDtlattachAlloc(type, view, position, attach_pt, &att) 参数顺序与 PRO_DTLATTACHTYPE_FREE 枚举名
    check(ProDtlattachAlloc(PRO_DTLATTACHTYPE_FREE, nullptr, p, nullptr, &att), "ProDtlattachAlloc(FREE)");
    return att;
}

// 构造带引线的附着：引线指向模型边（sel），view 为所属视图
ProDtlattach make_leader_attach(ProDrawing d, ProView view, const Pt& tip_mm, ProSelection sel) {
    ProPoint3d p;
    sheet_mm_to_drw(d, tip_mm, p);
    ProDtlattach att = nullptr;
    // TODO(实机核对): 引线附着为 PRO_DTLATTACHTYPE_PARAMETRIC + ProSelection；
    //   并核对该附着应通过 ProDtlnotedataLeadersSet 还是 ProDtlnotedataAttachmentSet 的 leader 类型挂到注释上
    check(ProDtlattachAlloc(PRO_DTLATTACHTYPE_PARAMETRIC, view, p, sel, &att), "ProDtlattachAlloc(PARAMETRIC)");
    return att;
}

// 创建并显示一个注释，返回注释 id；leader_sel 非空则带引线
int create_note(ProDrawing d, const std::vector<std::string>& lines, const Pt& at_mm, double height_mm,
                ProView leader_view, ProSelection leader_sel, const Pt* leader_tip_mm) {
    ProDtlnotedata data = nullptr;
    check(ProDtlnotedataAlloc((ProMdl)d, &data), "ProDtlnotedataAlloc");

    // TODO(实机核对): 文字样式 ProDtlnotetextstyleAlloc / ...HeightSet / ProDtlnotetextStyleSet 命名
    ProDtlnotetextstyle style = nullptr;
    check(ProDtlnotetextstyleAlloc(&style), "ProDtlnotetextstyleAlloc");
    check(ProDtlnotetextstyleHeightSet(style, mm_to_drw_len(d, height_mm)), "ProDtlnotetextstyleHeightSet");

    for (const auto& ln : lines) {
        ProDtlnoteline line = nullptr;
        check(ProDtlnotelineAlloc(&line), "ProDtlnotelineAlloc");
        ProDtlnotetext text = nullptr;
        check(ProDtlnotetextAlloc(&text), "ProDtlnotetextAlloc");
        std::wstring w = widen(ln);
        check(ProDtlnotetextStringSet(text, (wchar_t*)w.c_str()), "ProDtlnotetextStringSet");
        check(ProDtlnotetextStyleSet(text, style), "ProDtlnotetextStyleSet");
        // TODO(实机核对): 也可能是 ProDtlnotelineTextsSet(line, ProArray)；此处用 Add 形式
        check(ProDtlnotelineTextAdd(line, text), "ProDtlnotelineTextAdd");
        check(ProDtlnotedataLineAdd(data, line), "ProDtlnotedataLineAdd");
    }

    // 文字位置：free 附着
    ProDtlattach att = make_free_attach(d, at_mm);
    check(ProDtlnotedataAttachmentSet(data, att), "ProDtlnotedataAttachmentSet");

    // 引线：指向模型边
    if (leader_sel && leader_tip_mm) {
        ProDtlattach lead = make_leader_attach(d, leader_view, *leader_tip_mm, leader_sel);
        // TODO(实机核对): 引线设置 API（ProDtlnotedataLeadersSet? 需 ProDtlleaders 结构）与箭头样式
        check(ProDtlnotedataLeaderAttachAdd(data, lead), "ProDtlnotedataLeaderAttachAdd");
    }

    ProDtlnote note;
    // TODO(实机核对): ProDtlnoteCreate 第一参是 ProMdl 还是 ProDrawing
    check(ProDtlnoteCreate((ProMdl)d, data, &note), "ProDtlnoteCreate");
    check(ProDtlnoteShow(&note), "ProDtlnoteShow");
    ProDtlnotedataFree(data);
    return note.id;
}

// ---------- 系统符号（基准/粗糙度）通用放置 ----------
struct SymVar { std::string name; std::string value; };

// 在视图内放置库符号 symname；tip_sel/tip_mm 非空则带引线指向该边；vars 为符号可变文字
int place_symbol(ProDrawing d, ProView view, const char* symname, const Pt& pos_mm, ProSelection tip_sel,
                 const Pt* tip_mm, const std::vector<SymVar>& vars) {
    ProDtlsyminstdata data = nullptr;
    check(ProDtlsyminstdataAlloc((ProMdl)d, &data), "ProDtlsyminstdataAlloc");

    // TODO(实机核对): 系统库符号需先载入到图纸（ProDtlsymdefRetrieve），再 ProDtlsyminstdataSymdefSet；
    //   这里按“先按名查找，找不到则从系统库载入”的思路写
    ProDtlsymdef def;
    wchar_t wname[PRO_NAME_SIZE];
    {
        std::wstring w = widen(symname);
        std::wcsncpy(wname, w.c_str(), PRO_NAME_SIZE - 1);
        wname[PRO_NAME_SIZE - 1] = 0;
    }
    if (ProDtlsymdefFind((ProMdl)d, wname, &def) != PRO_TK_NO_ERROR) {
        // TODO(实机核对): 系统符号库路径（<creo>/symbols/library_syms/），传空让 Creo 按搜索路径找
        check(ProDtlsymdefRetrieve((ProMdl)d, (ProPath)L"", wname, &def), "ProDtlsymdefRetrieve");
    }
    check(ProDtlsyminstdataSymdefSet(data, &def), "ProDtlsyminstdataSymdefSet");

    // 位置 / 引线
    ProDtlattach att = (tip_sel && tip_mm) ? make_leader_attach(d, view, *tip_mm, tip_sel)
                                           : make_free_attach(d, pos_mm);
    check(ProDtlsyminstdataAttachmentSet(data, att), "ProDtlsyminstdataAttachmentSet");
    // TODO(实机核对): 带引线时符号本体位置(pos)与引线终点(tip)应分别设置（ProDtlsyminstdataLeaderSet?）

    // TODO(实机核对): ProDtlvartext 构造：ProDtlvartextAlloc / ProDtlvartextAttrSet / ProDtlsyminstdataVartextAdd 命名
    for (const auto& v : vars) {
        ProDtlvartext vt = nullptr;
        check(ProDtlvartextAlloc(&vt), "ProDtlvartextAlloc");
        std::wstring wn = widen(v.name), wv = widen(v.value);
        check(ProDtlvartextAttrSet(vt, (wchar_t*)wn.c_str(), (wchar_t*)wv.c_str()), "ProDtlvartextAttrSet");
        check(ProDtlsyminstdataVartextAdd(data, vt), "ProDtlsyminstdataVartextAdd");
    }

    ProDtlsyminst inst;
    check(ProDtlsyminstCreate((ProMdl)d, data, &inst), "ProDtlsyminstCreate");
    check(ProDtlsyminstShow(&inst), "ProDtlsyminstShow");
    ProDtlsyminstdataFree(data);
    return inst.id;
}

// 形位公差类型映射（IR 名 → ProGtoltype）
ProGtoltype gtol_type_of(const std::string& t) {
    // TODO(实机核对): 枚举名按 ProGtol.h；"roundness" 在 Creo 中为 CIRCULARITY；
    //   "coaxiality" 无同名枚举，Creo 用 CONCENTRICITY（同轴度）或 POSITION，此处取 CONCENTRICITY
    if (t == "flatness") return PRO_GTOL_FLATNESS;
    if (t == "straightness") return PRO_GTOL_STRAIGHTNESS;
    if (t == "roundness") return PRO_GTOL_CIRCULARITY;
    if (t == "cylindricity") return PRO_GTOL_CYLINDRICITY;
    if (t == "parallelism") return PRO_GTOL_PARALLELISM;
    if (t == "perpendicularity") return PRO_GTOL_PERPENDICULARITY;
    if (t == "angularity") return PRO_GTOL_ANGULARITY;
    if (t == "position") return PRO_GTOL_POSITION;
    if (t == "coaxiality") return PRO_GTOL_CONCENTRICITY;
    if (t == "symmetry") return PRO_GTOL_SYMMETRY;
    if (t == "circular_runout") return PRO_GTOL_CIRCULAR_RUNOUT;
    if (t == "total_runout") return PRO_GTOL_TOTAL_RUNOUT;
    throw CmdError("BAD_ARGS", "未知形位公差类型: " + t);
}

// ---------- 回读辅助 ----------

struct CountCtx { int count = 0; };

ProError count_visit(void* /*item*/, ProError /*status*/, ProAppData data) {
    ((CountCtx*)data)->count++;
    return PRO_TK_NO_ERROR;
}

ProError view_name_visit(ProView view, ProError /*status*/, ProAppData data) {
    auto* names = (std::vector<std::string>*)data;
    ProName nm;
    // TODO(实机核对): ProDrawingViewNameGet(drawing, view, name)
    if (ProDrawingViewNameGet(g_readback_drawing, view, nm) == PRO_TK_NO_ERROR) names->push_back(narrow(nm));
    return PRO_TK_NO_ERROR;
}

// 表格单元格文字（多行以 \n 连接）
std::string table_cell_text(ProDwgtable* t, int col, int row) {
    ProDtlnote note;
    // TODO(实机核对): ProDwgtableCellNoteGet(table*, col, row, &note)
    if (ProDwgtableCellNoteGet(t, col, row, &note) != PRO_TK_NO_ERROR) return "";
    ProDtlnotedata nd = nullptr;
    if (ProDtlnoteDataGet(&note, nullptr, PRODISPMODE_SYMBOLIC, &nd) != PRO_TK_NO_ERROR) return "";
    // TODO(实机核对): 取行/文本——ProDtlnotedataLinesCollect / ProDtlnotelineTextsCollect / ProDtlnotetextStringGet
    std::string out;
    ProDtlnoteline* lines = nullptr;
    if (ProDtlnotedataLinesCollect(nd, &lines) == PRO_TK_NO_ERROR && lines) {
        int nl = 0;
        ProArraySizeGet((ProArray)lines, &nl);
        for (int i = 0; i < nl; ++i) {
            ProDtlnotetext* texts = nullptr;
            if (ProDtlnotelineTextsCollect(lines[i], &texts) == PRO_TK_NO_ERROR && texts) {
                int nt = 0;
                ProArraySizeGet((ProArray)texts, &nt);
                if (i > 0) out += "\n";
                for (int j = 0; j < nt; ++j) {
                    wchar_t* ws = nullptr;
                    if (ProDtlnotetextStringGet(texts[j], &ws) == PRO_TK_NO_ERROR && ws) out += narrow(ws);
                }
                ProArrayFree((ProArray*)&texts);
            }
        }
        ProArrayFree((ProArray*)&lines);
    }
    ProDtlnotedataFree(nd);
    return out;
}

// ---------- 命令实现（参数键只在这些函数内读取） ----------

// drw.note
json cmd_drw_note(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    std::string text = arg_str(a, "text");
    Pt at = arg_pt2(a, "at");
    double height = has(a, "height") ? arg_num(a, "height") : 3.5;
    if (height <= 0) throw CmdError("BAD_ARGS", "height 必须为正");

    ProSelection sel = nullptr;
    ProView view = nullptr;
    Pt tip{};
    if (has(a, "leader_to")) {
        tip = arg_pt2(a, "leader_to");
        if (!has(a, "view")) throw CmdError("BAD_ARGS", "leader_to 需要同时给 view");
        view = find_view(d, arg_str(a, "view"));
        sel = pick_edge_at(d, view, tip);  // 找不到抛 PRO_TK_E_NOT_FOUND
    }
    int id;
    try {
        id = create_note(d, split_lines(text), at, height, view, sel, sel ? &tip : nullptr);
    } catch (...) {
        if (sel) ProSelectionFree(&sel);
        throw;
    }
    if (sel) ProSelectionFree(&sel);
    return json{{"id", id}};
}

// ---------- drw.dim ----------
// 签名依据 Creo TOOLKIT API 参考（ProDrawing.h / ProDimension.h）：
//   ProDrawingDimensionCreate(ProDrawing, ProDimAttachment* attachment_arr /*ProArray*/, ProDimSense* dsense_arr /*ProArray*/,
//                             ProDimOrient orient_hint, ProVector location, ProBoolean ref_dim, ProDimension*)
//   typedef ProSelection ProDimAttachment[2];  ProDimSense{type, sense, angle_sense, orient_hint}
//   ProDrawingOrdbaselineCreate(drawing, ProDimension* linear, ProVector location, ProDimension* base_out)
//     —— 线性尺寸转为坐标尺寸，同时生成基线（0）尺寸；location 为线性尺寸某条尺寸界线上的点
//   ProDrawingDimToOrdinate(drawing, ProDimension* linear, ProDimension* base)
//   ProDrawingDimIsOrdinate(drawing, ProDimension*, ProBoolean*, ProDimension* base_out)

// 选中边的尺寸意义：直线边 → 整条边；圆/圆弧 → 圆心（孔位坐标、孔距都量到圆心）
ProDimSense sense_for(ProSelection sel, bool center_for_arcs) {
    ProDimSense s;
    std::memset(&s, 0, sizeof(s));
    s.type = PRO_DIM_SNS_TYP_NONE;
    s.orient_hint = PRO_DIM_ORNT_NONE;
    if (!center_for_arcs) return s;
    ProModelitem mi;
    if (ProSelectionModelitemGet(sel, &mi) != PRO_TK_NO_ERROR || mi.type != PRO_EDGE) return s;
    ProEdge edge;
    ProEnttype et;
    // TODO(实机核对): ProGeomitemToEdge / ProEdgeTypeGet(ProEdge, ProEnttype*)，PRO_ENT_CIRCLE / PRO_ENT_ARC
    if (ProGeomitemToEdge(&mi, &edge) == PRO_TK_NO_ERROR && ProEdgeTypeGet(edge, &et) == PRO_TK_NO_ERROR &&
        (et == PRO_ENT_CIRCLE || et == PRO_ENT_ARC)) {
        s.type = PRO_DIM_SNS_TYP_PNT;
        s.sense = PRO_POINT_TYP_CENTER;
    }
    return s;
}

bool same_item(ProSelection a, ProSelection b) {
    ProModelitem ma, mb;
    if (ProSelectionModelitemGet(a, &ma) != PRO_TK_NO_ERROR || ProSelectionModelitemGet(b, &mb) != PRO_TK_NO_ERROR)
        return false;
    return ma.id == mb.id && ma.type == mb.type && ma.owner == mb.owner;
}

ProDimension create_dim(ProDrawing d, const std::vector<ProSelection>& sels, bool center_for_arcs, ProDimOrient ori,
                        const Pt& at) {
    ProDimAttachment* atts = nullptr;
    ProDimSense* senses = nullptr;
    check(ProArrayAlloc(0, sizeof(ProDimAttachment), 2, (ProArray*)&atts), "ProArrayAlloc(attachments)");
    check(ProArrayAlloc(0, sizeof(ProDimSense), 2, (ProArray*)&senses), "ProArrayAlloc(senses)");
    for (ProSelection s : sels) {
        ProDimAttachment att;
        att[0] = s;
        att[1] = nullptr;
        ProDimSense sn = sense_for(s, center_for_arcs);
        ProArrayObjectAdd((ProArray*)&atts, PRO_VALUE_UNUSED, 1, &att);
        ProArrayObjectAdd((ProArray*)&senses, PRO_VALUE_UNUSED, 1, &sn);
    }
    ProPoint3d loc;
    sheet_mm_to_drw(d, at, loc);
    ProDimension dim;
    // ref_dim：文档写"PRO_B_TRUE 建 PRO_DIMENSION"。TODO(实机核对): 若建出参考尺寸（带 REF/括号）则改 PRO_B_FALSE
    ProError e = ProDrawingDimensionCreate(d, atts, senses, ori, loc, PRO_B_TRUE, &dim);
    ProArrayFree((ProArray*)&atts);
    ProArrayFree((ProArray*)&senses);
    check(e, "ProDrawingDimensionCreate");
    return dim;
}

// 坐标尺寸基线缓存：同一图纸、同一视图、同一方向、同一基准边点 → 共用一条基线（0 尺寸）
struct OrdBase {
    std::string key;
    ProDimension base;
};
std::vector<OrdBase> g_ord_bases;

std::string ord_key(const std::string& drawing, const std::string& view, const std::string& orient, const Pt& p1) {
    char buf[96];
    std::snprintf(buf, sizeof(buf), "|%s|%.2f|%.2f", orient.c_str(), p1[0], p1[1]);
    return drawing + "|" + view + buf;
}

bool find_base(ProDrawing d, const std::string& key, ProDimension* out) {
    for (auto it = g_ord_bases.begin(); it != g_ord_bases.end(); ++it) {
        if (it->key != key) continue;
        ProBoolean is_ord = PRO_B_FALSE;
        ProDimension bb;
        // 基线可能已被删除或图纸已重开：校验仍是坐标尺寸
        if (ProDrawingDimIsOrdinate(d, &it->base, &is_ord, &bb) == PRO_TK_NO_ERROR && is_ord) {
            *out = it->base;
            return true;
        }
        g_ord_bases.erase(it);
        return false;
    }
    return false;
}

void set_dim_text(ProDimension* dim, const std::string& text) {
    std::wstring w = widen(text);
    ProWstring* lines = nullptr;
    check(ProArrayAlloc(1, sizeof(ProWstring), 1, (ProArray*)&lines), "ProArrayAlloc");
    lines[0] = (ProWstring)w.c_str();
    // TODO(实机核对): ProDimensionTextWstringsSet(ProDimension*, ProWstring* /*ProArray*/)；@D 保留实测值时文字写 "@D" 前后缀
    ProError e = ProDimensionTextWstringsSet(dim, lines);
    ProArrayFree((ProArray*)&lines);
    check(e, "ProDimensionTextWstringsSet");
}

// 极限偏差：Creo 原生"正负"公差显示（上下叠写）。失败只记数，不中断。
// TODO(实机核对): ProDimensionToltypeSet(dim, PRO_TOL_PLUS_MINUS) 与 ProDimensionToleranceSet(dim, upper, lower)
//   在 Creo 7/9/10 的签名；ProDimensionToleranceSet 的下偏差是否传绝对值（Creo 界面输入下偏差时自带负号）。
//   公差显示需 config 选项 tol_display=yes：首次调用时在会话里打开（ProConfigoptSet，ProUtil.h）。
bool set_dim_tol(ProDimension* dim, double upper, double lower) {
    static bool cfg_done = false;
    if (!cfg_done) {
        ProConfigoptSet(const_cast<wchar_t*>(L"tol_display"), const_cast<wchar_t*>(L"yes"));
        cfg_done = true;
    }
    if (ProDimensionToltypeSet(dim, PRO_TOL_PLUS_MINUS) != PRO_TK_NO_ERROR) return false;
    return ProDimensionToleranceSet(dim, upper, -lower) == PRO_TK_NO_ERROR;
}

// drw.dim
//  linear：p1/p2 两条被标注边上的点；diameter/radius：p1 为圆/圆弧边上的点
//  ordinate：p1 = 基准轮廓边上的点，p2 = 被标注要素边上的点（孔取圆周点 → 量到圆心）。
//    同一视图同一方向同一基准边的第一个坐标尺寸：先建 p1→p2 线性尺寸，再 ProDrawingOrdbaselineCreate 转为坐标尺寸并生成基线；
//    之后的：先建线性尺寸，再 ProDrawingDimToOrdinate 挂到已有基线。p2 选到与基准同一条边（值为 0）时不重复建，只确保基线存在。
json cmd_drw_dim(const json& a) {
    std::string dname = arg_str(a, "drawing");
    ProDrawing d = find_drawing(dname);
    std::string vname = arg_str(a, "view");
    ProView view = find_view(d, vname);
    std::string kind = arg_str(a, "kind");
    Pt p1 = arg_pt2(a, "p1");
    Pt at = arg_pt2(a, "at");
    std::string orient = has(a, "orient") ? arg_str(a, "orient") : "aligned";
    std::string text = has(a, "text") ? arg_str(a, "text") : "";
    bool has_tol = has(a, "tol");
    double tol_up = 0, tol_lo = 0;
    if (has_tol) {
        if (!a["tol"].is_array() || a["tol"].size() != 2) throw CmdError("BAD_ARGS", "tol 须为 [上偏差, 下偏差]（mm）");
        tol_up = a["tol"][0].get<double>();
        tol_lo = a["tol"][1].get<double>();
    }
    int tol_failed = 0;

    if (kind != "linear" && kind != "diameter" && kind != "radius" && kind != "ordinate")
        throw CmdError("BAD_ARGS", "kind 须为 linear|diameter|radius|ordinate");
    if (orient != "h" && orient != "v" && orient != "aligned")
        throw CmdError("BAD_ARGS", "orient 须为 h|v|aligned");
    if (kind == "ordinate" && orient == "aligned") throw CmdError("BAD_ARGS", "ordinate 须给 orient=h|v");

    ProDimOrient ori = orient == "h" ? PRO_DIM_ORNT_HORIZ : orient == "v" ? PRO_DIM_ORNT_VERT : PRO_DIM_ORNT_SLANTED;
    if (kind == "diameter" || kind == "radius") ori = PRO_DIM_ORNT_NONE;  // 由附着圆边决定

    std::vector<ProSelection> sels;
    auto free_sels = [&]() { for (auto& s : sels) if (s) ProSelectionFree(&s); sels.clear(); };
    json out;
    try {
        sels.push_back(pick_edge_at(d, view, p1));
        if (kind == "linear" || kind == "ordinate") sels.push_back(pick_edge_at(d, view, arg_pt2(a, "p2")));

        if (kind == "ordinate") {
            std::string key = ord_key(dname, vname, orient, p1);
            ProDimension base;
            bool have_base = find_base(d, key, &base);
            if (same_item(sels[0], sels[1])) {  // 值为 0：就是基线本身
                free_sels();
                return json{{"id", have_base ? json(base.id) : json(nullptr)}, {"baseline", true}};
            }
            ProDimension dim = create_dim(d, sels, true, ori, at);
            if (have_base) {
                check(ProDrawingDimToOrdinate(d, &dim, &base), "ProDrawingDimToOrdinate");
            } else {
                ProPoint3d loc;  // 基准边一侧尺寸界线上的点
                sheet_mm_to_drw(d, p1, loc);
                check(ProDrawingOrdbaselineCreate(d, &dim, loc, &base), "ProDrawingOrdbaselineCreate");
                g_ord_bases.push_back({key, base});
            }
            if (!text.empty()) set_dim_text(&dim, text);
            if (has_tol && !set_dim_tol(&dim, tol_up, tol_lo)) ++tol_failed;
            out = json{{"id", dim.id}, {"baseline_id", base.id}};
        } else {
            // diameter/radius：Creo 按圆边自动加 Φ/R；TODO(实机核对): 圆边上直接建出的是直径还是半径——
            //   若与 kind 不符，改用 ProDimSense.type=PRO_DIM_SNS_TYP_PNT + orient_hint 指定，或建后 ProDimensionTypeSet
            ProDimension dim = create_dim(d, sels, kind == "linear", ori, at);
            if (!text.empty()) set_dim_text(&dim, text);
            if (has_tol && !set_dim_tol(&dim, tol_up, tol_lo)) ++tol_failed;
            out = json{{"id", dim.id}};
        }
    } catch (...) {
        free_sels();
        throw;
    }
    free_sels();
    if (has_tol) out["tol_set"] = tol_failed == 0;
    return out;
}

// drw.datum：基准符号（方框内字母，贴边引线）
json cmd_drw_datum(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    ProView view = find_view(d, arg_str(a, "view"));
    Pt at = arg_pt2(a, "at");
    Pt pos = arg_pt2(a, "pos");
    std::string label = arg_str(a, "label");
    ProSelection sel = pick_edge_at(d, view, at);
    int id;
    try {
        // TODO(实机核对): 系统库中的基准符号名（疑似 "datum_feat_sym"/"ref_datum"）与其可变文字名（如 "name"）；
        //   另一条路是模型侧 ProSetDatumTag + 图中 show annotation，但图纸里直接放符号更可控
        id = place_symbol(d, view, "datum_feat_sym", pos, sel, &at, {{"name", label}});
    } catch (...) {
        ProSelectionFree(&sel);
        throw;
    }
    ProSelectionFree(&sel);
    return json{{"id", id}};
}

// drw.gtol：形位公差框格
json cmd_drw_gtol(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    ProView view = find_view(d, arg_str(a, "view"));
    Pt pos = arg_pt2(a, "pos");
    ProGtoltype type = gtol_type_of(arg_str(a, "type"));
    double value = arg_num(a, "value");
    bool dia = has(a, "diameter") && a["diameter"].is_boolean() && a["diameter"].get<bool>();
    std::vector<std::string> datums;
    if (has(a, "datums")) {
        for (const auto& x : a["datums"]) datums.push_back(x.get<std::string>());
        if (datums.size() > 3) throw CmdError("BAD_ARGS", "datums 最多 3 个");
    }

    ProSelection sel = nullptr;
    if (has(a, "at")) sel = pick_edge_at(d, view, arg_pt2(a, "at"));  // 不带引线时 at 为 null

    int id = 0;
    try {
        // TODO(实机核对): Creo 10 的 gtol 创建 API。旧版：ProGtoldataAlloc → ProGtoldataTypeSet / ...ValueSet /
        //   ...DatumRefsSet / ...PlacementSet → ProGtolCreate；新版 ProMdlGtolCreate 等。以下按旧版写。
        ProGtoldata gd = nullptr;
        check(ProGtoldataAlloc((ProMdl)d, &gd), "ProGtoldataAlloc");
        check(ProGtoldataTypeSet(gd, type), "ProGtoldataTypeSet");
        check(ProGtoldataValueSet(gd, mm_to_drw_len(d, value)), "ProGtoldataValueSet");
        // TODO(实机核对): 直径公差带——ProGtoldataDiameterSet? 或 ProGtoldataTolzoneSet(PRO_GTOL_ZONE_DIAMETER)
        if (dia) check(ProGtoldataDiameterSet(gd, PRO_B_TRUE), "ProGtoldataDiameterSet");
        // TODO(实机核对): 基准参照为 ProGtoldatumref 数组，每项含基准名 + 修饰符；字母对应 drw.datum 的 label
        for (size_t i = 0; i < datums.size(); ++i) {
            std::wstring w = widen(datums[i]);
            check(ProGtoldataDatumRefAdd(gd, (int)i, (wchar_t*)w.c_str()), "ProGtoldataDatumRefAdd");
        }
        ProPoint3d p;
        sheet_mm_to_drw(d, pos, p);
        // TODO(实机核对): ProGtolplacetype 枚举（LEADER/FREE）与 ProGtoldataPlacementSet 参数
        check(ProGtoldataPlacementSet(gd, sel ? PRO_GTOL_PLACE_LEADER : PRO_GTOL_PLACE_FREE, sel, view, p),
              "ProGtoldataPlacementSet");
        ProGtol gt;
        check(ProGtolCreate((ProMdl)d, gd, &gt), "ProGtolCreate");
        check(ProGtolShow(&gt), "ProGtolShow");
        id = gt.id;
        ProGtoldataFree(gd);
    } catch (...) {
        if (sel) ProSelectionFree(&sel);
        throw;
    }
    if (sel) ProSelectionFree(&sel);
    return json{{"id", id}};
}

// drw.surface_finish：表面粗糙度（Ra，去除材料/不去除）
json cmd_drw_surface_finish(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    ProView view = find_view(d, arg_str(a, "view"));
    Pt at = arg_pt2(a, "at");
    Pt pos = arg_pt2(a, "pos");
    double ra = arg_num(a, "ra");
    std::string removal = has(a, "removal") ? arg_str(a, "removal") : "required";
    if (removal != "required" && removal != "prohibited")
        throw CmdError("BAD_ARGS", "removal 须为 required|prohibited");

    // Ra 值文字：整数不带小数点，否则按 %g（3.2、1.6、0.8、12.5）
    char buf[32];
    if (ra == std::floor(ra)) std::snprintf(buf, sizeof buf, "%d", (int)ra);
    else std::snprintf(buf, sizeof buf, "%g", ra);

    ProSelection sel = pick_edge_at(d, view, at);
    int id;
    try {
        // TODO(实机核对): 系统符号库粗糙度符号名——常见 "machined"（去除材料）/"unmachined"（不去除）/"standard1"；
        //   可变文字名通常为 "roughness_height"。
        const char* sym = removal == "required" ? "machined" : "unmachined";
        id = place_symbol(d, view, sym, pos, sel, &at, {{"roughness_height", buf}});
    } catch (...) {
        ProSelectionFree(&sel);
        throw;
    }
    ProSelectionFree(&sel);
    return json{{"id", id}};
}

// drw.table：origin 为左下角；header_bottom 时表头在最下行（数据行倒序，第一条数据紧挨表头之上）
json cmd_drw_table(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    Pt origin = arg_pt2(a, "origin");
    double row_h = has(a, "row_height") ? arg_num(a, "row_height") : 6.0;
    bool header_bottom = has(a, "header_bottom") && a["header_bottom"].is_boolean() && a["header_bottom"].get<bool>();
    if (!has(a, "columns") || !a["columns"].is_array() || a["columns"].empty())
        throw CmdError("BAD_ARGS", "columns 必须为非空数组");
    if (!has(a, "rows") || !a["rows"].is_array()) throw CmdError("BAD_ARGS", "rows 必须为二维数组");
    if (row_h <= 0) throw CmdError("BAD_ARGS", "row_height 必须为正");

    std::vector<std::string> titles;
    std::vector<double> widths;
    for (const auto& col : a["columns"]) {
        titles.push_back(col.at("title").get<std::string>());
        widths.push_back(col.at("width").get<double>());
    }
    const int ncols = (int)titles.size();

    std::vector<std::vector<std::string>> body, grid;
    for (const auto& r : a["rows"]) {
        std::vector<std::string> row;
        for (const auto& c : r) row.push_back(c.is_string() ? c.get<std::string>() : c.dump());
        row.resize(ncols);  // 缺的补空，多的截断
        body.push_back(row);
    }
    if (header_bottom) {
        for (auto it = body.rbegin(); it != body.rend(); ++it) grid.push_back(*it);
        grid.push_back(titles);
    } else {
        grid.push_back(titles);
        for (auto& r : body) grid.push_back(r);
    }
    const int nrows = (int)grid.size();

    // Creo 表格以左上角为原点向下、向右生长；origin 是左下角，故上移 nrows*row_h
    Pt top_left{origin[0], origin[1] + nrows * row_h};
    ProPoint3d p;
    sheet_mm_to_drw(d, top_left, p);

    ProDwgtabledata td = nullptr;
    // TODO(实机核对): ProDwgtabledataAlloc 是否带 drawing 参数；以下 Set 系列名称与单位类型（PRO_TABLESIZE_*）
    check(ProDwgtabledataAlloc(&td), "ProDwgtabledataAlloc");
    check(ProDwgtabledataOriginSet(td, p), "ProDwgtabledataOriginSet");
    check(ProDwgtabledataSizetypeSet(td, PRO_TABLESIZE_DRAWING_UNITS), "ProDwgtabledataSizetypeSet");
    std::vector<double> wd;
    for (double w : widths) wd.push_back(mm_to_drw_len(d, w));
    std::vector<double> ht(nrows, mm_to_drw_len(d, row_h));
    check(ProDwgtabledataColumnsSet(td, ncols, wd.data()), "ProDwgtabledataColumnsSet");
    check(ProDwgtabledataRowsSet(td, nrows, ht.data()), "ProDwgtabledataRowsSet");

    ProDwgtable table;
    check(ProDwgtableCreate(d, td, &table), "ProDwgtableCreate");
    ProDwgtabledataFree(td);

    // 逐格写文字；Creo 行列号从 1 开始
    for (int r = 0; r < nrows; ++r) {
        for (int c = 0; c < ncols; ++c) {
            if (grid[r][c].empty()) continue;
            std::wstring w = widen(grid[r][c]);
            // TODO(实机核对): ProDwgtableTextEnter(table*, col, row, lines) 参数顺序；lines 为 ProArray
            ProArray arr;
            check(ProArrayAlloc(1, sizeof(ProWstring), 1, &arr), "ProArrayAlloc");
            ((ProWstring*)arr)[0] = (ProWstring)w.c_str();
            ProError e = ProDwgtableTextEnter(&table, c + 1, r + 1, (ProWstring*)arr);
            ProArrayFree(&arr);
            check(e, "ProDwgtableTextEnter");
        }
    }
    check(ProDwgtableDisplay(&table), "ProDwgtableDisplay");
    return json{{"id", table.id}, {"rows", nrows}, {"cols", ncols}};
}

// ---------- drw.section ----------
// 剖截面一律用 ProXSectionOffsetCreate（ProXsec.h，Creo 7+ 取代 ProXsecOffsetCreate）：
//   ProXSectionOffsetCreate(ProSolid, ProMdlName, double trf[4][3], Pro2dLinedef* ent_arr /*ProArray*/,
//                           ProXsecOffsetSide side, int flip, ProXsecExcludeModels, ProSelection* exclude_items,
//                           ProXSectionCreateData data, ProXsecMdlname* r_xsec)
//   折线所在平面 = trf 给出的局部坐标系（x 轴、y 轴、z 轴、原点）；只有一段的折线即平面剖（full/half），多段即阶梯剖（step）。
// 视图设剖视：ProDrawingView2DSectionSet(drawing, view, ProName sec_name, ProDrawingViewSectionAreaType,
//                                       ProSelection ref_sel, ProCurvedata* curve_data, ProView arrow_display_view)
//   FULL：ref_sel = NULL；HALF：ref_sel = 分界基准面；LOCAL：ref_sel = 局部区域内模型边上的点，curve_data = 样条边界。
//
// cut_points 是 IR 坐标（Z 向上）：剖切折线画在零件顶面（z = cut_points[0].z）上，剖切面沿 IR −Z 方向穿过零件。
// 折线平面局部系：x = IR +X → Creo (1,0,0)；y = IR +Y → Creo (0,0,−1)；z = x×y = Creo (0,1,0)（= IR +Z）；
// 原点 = IR (0,0,z) → Creo (0,z,0)。于是折线二维坐标就是 IR 的 (x, y)。

// 被替代视图的观察方向（IR）：剖切后保留"远离观察者"一侧的材料。GB 第一角：主视图从 −Y 看向 +Y，左视图从 −X 看向 +X
std::array<double, 2> view_keep_dir(const std::string& view) {
    if (view == "V_FRONT") return {0, 1};
    if (view == "V_BACK") return {0, -1};
    if (view == "V_LEFT") return {1, 0};
    if (view == "V_RIGHT") return {-1, 0};
    throw CmdError("BAD_ARGS", "剖视只能替代主/后/左/右视图（视线水平），收到 " + view);
}

// trf_in 为空：折线在顶面（见上方说明）；否则用给定局部系（行 = x 轴、y 轴、z 轴、原点[mm]，Creo 坐标）
void create_offset_xsec(ProSolid solid, const std::string& name, const std::vector<std::array<double, 3>>& cp,
                        int flip, const double (*trf_in)[3] = nullptr) {
    double f = model_mm_factor((ProMdl)solid);
    double trf[4][3] = {{1, 0, 0}, {0, 0, -1}, {0, 1, 0}, {0, cp[0][2] * f, 0}};
    if (trf_in) {
        for (int i = 0; i < 3; ++i)
            for (int j = 0; j < 3; ++j) trf[i][j] = trf_in[i][j];
        for (int j = 0; j < 3; ++j) trf[3][j] = trf_in[3][j] * f;
    }
    Pro2dLinedef* ents = nullptr;
    check(ProArrayAlloc(0, sizeof(Pro2dLinedef), (int)cp.size(), (ProArray*)&ents), "ProArrayAlloc(lines)");
    for (size_t i = 0; i + 1 < cp.size(); ++i) {
        Pro2dLinedef l;
        std::memset(&l, 0, sizeof(l));
        l.type = PRO_2D_LINE;
        l.end1[0] = cp[i][0] * f;
        l.end1[1] = cp[i][1] * f;
        l.end2[0] = cp[i + 1][0] * f;
        l.end2[1] = cp[i + 1][1] * f;
        ProArrayObjectAdd((ProArray*)&ents, PRO_VALUE_UNUSED, 1, &l);
    }
    ProMdlName wname;
    std::wstring wl = widen(name);
    if (wl.size() >= sizeof(wname) / sizeof(wname[0])) {
        ProArrayFree((ProArray*)&ents);
        throw CmdError("BAD_ARGS", "剖截面名过长: " + name);
    }
    std::wcscpy(wname, wl.c_str());
    ProXsecMdlname xs = nullptr;
    // side = SIDE_2：折线在顶面，材料都在局部 z 的负侧（IR 下方）。TODO(实机核对): 若剖面为空改 SIDE_1
    ProError e = ProXSectionOffsetCreate(solid, wname, trf, ents, PRO_XSEC_OFFSET_SIDE_2, flip,
                                         PRO_XSEC_MODEL_EXCLUDE, nullptr, nullptr, &xs);
    ProArrayFree((ProArray*)&ents);
    check(e, "ProXSectionOffsetCreate");
    if (xs) ProXsecMdlnameFree(&xs);
}

// 局部剖边界：图纸 mm 闭合折线 → 过各点的闭合样条（图纸坐标）
// TODO(实机核对): curve_data 的坐标系（图纸坐标 / 视图坐标）与 ProSplinedataInit(par, pnts, tans, n, ProCurvedata*) 签名
void make_boundary_spline(ProDrawing d, const std::vector<Pt>& pts_mm, ProCurvedata* cd) {
    size_t n = pts_mm.size();
    std::vector<ProPoint3d> pnts(n + 1), tans(n + 1);
    std::vector<double> par(n + 1);
    for (size_t i = 0; i <= n; ++i) sheet_mm_to_drw(d, pts_mm[i % n], pnts[i]);
    double acc = 0;
    for (size_t i = 0; i <= n; ++i) {
        if (i > 0) {
            double dx = pnts[i][0] - pnts[i - 1][0], dy = pnts[i][1] - pnts[i - 1][1];
            acc += std::sqrt(dx * dx + dy * dy);
        }
        par[i] = acc;
        const ProPoint3d& a = pnts[(i + n - 1) % n];  // 闭合：首尾切线一致
        const ProPoint3d& b = pnts[(i + 1) % n];
        tans[i][0] = (b[0] - a[0]) / 2;
        tans[i][1] = (b[1] - a[1]) / 2;
        tans[i][2] = 0;
    }
    for (auto& v : par) v /= acc > 0 ? acc : 1;
    check(ProSplinedataInit(par.data(), pnts.data(), tans.data(), (int)(n + 1), cd), "ProSplinedataInit");
}

// drw.section：full / half / step 先建剖截面（label 为名）再把 replaces 视图设为 2D 剖视；
// local 在 replaces 视图上按 boundary（图纸 mm 闭合折线）做局部剖。
json cmd_drw_section(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    std::string parent = has(a, "parent") ? arg_str(a, "parent") : "";
    std::string label = arg_str(a, "label");
    std::string kind = arg_str(a, "kind");
    if (kind != "full" && kind != "half" && kind != "step" && kind != "local")
        throw CmdError("BAD_ARGS", "kind 须为 full|half|step|local");
    std::string replaces = has(a, "replaces") ? arg_str(a, "replaces") : "";
    if (replaces.empty())
        throw CmdError("BAD_ARGS", "replaces 须指向已存在的视图（由 CREOSON 先建好），本命令把它设为剖视");
    if (has(a, "center")) (void)arg_pt2(a, "center");  // 仅校验格式；位置由已存在的视图决定
    ProView rv = find_view(d, replaces);
    ProView pv = parent.empty() ? nullptr : find_view(d, parent);

    if (!has(a, "cut_points") || !a["cut_points"].is_array() || a["cut_points"].size() < 2)
        throw CmdError("BAD_ARGS", "cut_points 至少 2 个模型坐标点");
    std::vector<std::array<double, 3>> cp;  // 仍是 IR 坐标；create_offset_xsec 内换到 Creo
    for (const auto& pt : a["cut_points"]) {
        if (!pt.is_array() || pt.size() < 2) throw CmdError("BAD_ARGS", "cut_points 元素须为 [x,y(,z)]");
        cp.push_back({pt[0].get<double>(), pt[1].get<double>(), pt.size() > 2 ? pt[2].get<double>() : 0.0});
    }
    if (kind != "step") cp.resize(2);  // full/half/local：平面剖，取前两点

    ProSolid solid = nullptr;
    // TODO(实机核对): ProDrawingViewSolidGet(drawing, view, &solid)
    check(ProDrawingViewSolidGet(d, rv, &solid), "ProDrawingViewSolidGet");

    // 保留侧：折线方向 (dx,dy) 从 +Z 看的右侧为 (dy,−dx)；flip=FALSE 保留右侧（ProXsec.h 说明）
    auto keep = view_keep_dir(replaces);
    double dx = cp[1][0] - cp[0][0], dy = cp[1][1] - cp[0][1];
    int flip = (dy * keep[0] - dx * keep[1]) > 0 ? 0 : 1;
    create_offset_xsec(solid, label, cp, flip);

    ProName sname;
    to_proname(label, sname);
    ProDrawingViewSectionAreaType area = kind == "half" ? PRO_VIEW_SECTION_AREA_HALF
                                         : kind == "local" ? PRO_VIEW_SECTION_AREA_LOCAL
                                                           : PRO_VIEW_SECTION_AREA_FULL;
    ProSelection ref = nullptr;
    ProCurvedata curve;
    ProCurvedata* curve_p = nullptr;
    if (kind == "half") {
        // 分界面：过原点、法向沿剖切线方向的默认基准面（IR X 向 → RIGHT，IR Y 向 → FRONT）
        // TODO(实机核对): 保留哪一半由 ProDrawingViewHalfVisibleAreaSet 的 keep_side 决定；此处用默认侧
        std::string pln = std::fabs(dx) >= std::fabs(dy) ? "RIGHT" : "FRONT";
        ProModelitem surf;
        if (!find_datum_plane(solid, pln, &surf)) throw CmdError("PRO_TK_E_NOT_FOUND", "半剖分界面 " + pln + " 不存在");
        check(ProSelectionAlloc(nullptr, &surf, &ref), "ProSelectionAlloc(half plane)");
    } else if (kind == "local") {
        if (!has(a, "boundary") || !a["boundary"].is_array() || a["boundary"].size() < 3)
            throw CmdError("BAD_ARGS", "local 需要 boundary：图纸 mm 闭合折线（≥3 点）");
        std::vector<Pt> bd;
        Pt c{0, 0};
        double xmin = 1e30, xmax = -1e30;
        for (const auto& q : a["boundary"]) {
            if (!q.is_array() || q.size() < 2) throw CmdError("BAD_ARGS", "boundary 元素须为 [x,y]");
            bd.push_back({q[0].get<double>(), q[1].get<double>()});
            c[0] += bd.back()[0];
            c[1] += bd.back()[1];
            xmin = std::min(xmin, bd.back()[0]);
            xmax = std::max(xmax, bd.back()[0]);
        }
        c[0] /= bd.size();
        c[1] /= bd.size();
        // ref_sel：区域中心附近的模型边（局部剖以它所在的位置为"剖开处"）
        ref = pick_edge_at(d, rv, c, std::max(1.5, (xmax - xmin) / 2));
        make_boundary_spline(d, bd, &curve);
        curve_p = &curve;
    }
    ProError e = ProDrawingView2DSectionSet(d, rv, sname, area, ref, curve_p, kind == "local" ? nullptr : pv);
    if (ref) ProSelectionFree(&ref);
    check(e, "ProDrawingView2DSectionSet");
    return json{{"xsec", label}, {"view", replaces}, {"kind", kind}};
}

// drw.removed_section：移出断面（轴类）。剖切面垂直于 IR Z 轴（轴线），位于 z；view 为 CREOSON 已建好的、
// 沿轴线观察的一般视图（如 V_SEC_A）。剖截面同样用 ProXSectionOffsetCreate：
//   局部系 x = IR +X → Creo (1,0,0)；z（扫掠方向）= IR +Y → Creo (0,0,−1)；y = z×x = Creo (0,−1,0)（= IR −Z）；
//   原点 = IR (0, 1000, 0) → Creo (0,0,−1000)，使零件全部落在局部 z 负侧（与 create_offset_xsec 的 SIDE_2 假设一致）。
//   折线 = 局部 y = −z 处沿 x 的一段直线 → 扫掠后即平面 IR z = const。
// 视图：ProDrawingView2DSectionSet(FULL，箭头画在 parent) → ProDrawingView2DSectionAreaSet（只显示切面，GB 断面图）。
json cmd_drw_removed_section(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    std::string view = arg_str(a, "view"), label = arg_str(a, "label");
    std::string parent = has(a, "parent") ? arg_str(a, "parent") : "";
    double z = arg_num(a, "z");
    double half = has(a, "half_span") ? arg_num(a, "half_span") : 500.0;
    ProView rv = find_view(d, view);
    ProView pv = parent.empty() ? nullptr : find_view(d, parent);
    ProSolid solid = nullptr;
    check(ProDrawingViewSolidGet(d, rv, &solid), "ProDrawingViewSolidGet");
    const double trf[4][3] = {{1, 0, 0}, {0, -1, 0}, {0, 0, -1}, {0, 0, -1000}};
    std::vector<std::array<double, 3>> cp = {{-half, -z, 0}, {half, -z, 0}};  // 局部 (x, y)
    // 保留侧：线向 +x，从局部 +z 看右侧 = 局部 −y = IR +Z。视图从 IR +Z 往下看（TOP），保留 IR z < z0 的一侧 → flip
    // TODO(实机核对): 断面图只显示切面，保留侧仅影响剖面线方向；若剖面为空改 flip 或 SIDE_1
    create_offset_xsec(solid, label, cp, 1, trf);
    ProName sname;
    to_proname(label, sname);
    check(ProDrawingView2DSectionSet(d, rv, sname, PRO_VIEW_SECTION_AREA_FULL, nullptr, nullptr, pv),
          "ProDrawingView2DSectionSet");
    // TODO(实机核对): ProDrawingView2DSectionAreaSet(ProDrawing, ProView)；已是 Area 时返回错误码，可忽略
    ProError e = ProDrawingView2DSectionAreaSet(d, rv);
    return json{{"xsec", label}, {"view", view}, {"section_only", e == PRO_TK_NO_ERROR}};
}

// drw.detail_view：局部放大图（GB/T 4458.1）。父视图中 circle（图纸 mm：圆心 + 半径）内的区域按 scale 放大，
// 放到 center（图纸 mm）；view 为别名（后续 drw.dim 用它找视图）。参考点 = 圆内最近的模型边。
// TODO(实机核对): ProDrawingViewDetailCreate(drawing, parent, ref_sel, ProCurvedata* boundary, ProPoint3d location,
//   ProView*) 签名与 boundary 的坐标系（图纸 / 父视图）；ProDrawingViewScaleSet 对局部放大图是否生效
json cmd_drw_detail_view(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    ProView pv = find_view(d, arg_str(a, "parent"));
    std::string alias = arg_str(a, "view");
    if (!has(a, "circle") || !a["circle"].is_array() || a["circle"].size() < 3)
        throw CmdError("BAD_ARGS", "circle 须为 [cx, cy, r]（图纸 mm）");
    Pt c{a["circle"][0].get<double>(), a["circle"][1].get<double>()};
    double r = a["circle"][2].get<double>();
    Pt at = arg_pt2(a, "center");
    double scale = arg_num(a, "scale");
    std::vector<Pt> ring;
    const double pi = 3.14159265358979323846;
    for (int k = 0; k < 24; ++k) ring.push_back({c[0] + r * std::cos(2 * pi * k / 24), c[1] + r * std::sin(2 * pi * k / 24)});
    ProSelection ref = pick_edge_at(d, pv, c, r);
    ProCurvedata curve;
    make_boundary_spline(d, ring, &curve);
    ProPoint3d loc;
    sheet_mm_to_drw(d, at, loc);
    ProView dv = nullptr;
    ProError e = ProDrawingViewDetailCreate(d, pv, ref, &curve, loc, &dv);
    ProSelectionFree(&ref);
    check(e, "ProDrawingViewDetailCreate");
    ProError es = ProDrawingViewScaleSet(d, dv, scale);
    register_view_alias(alias, dv);
    return json{{"view", alias}, {"scale_set", es == PRO_TK_NO_ERROR}};
}

// drw.balloon：BOM 球标（首期降级为带引线的序号注释）
json cmd_drw_balloon(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    ProView view = find_view(d, arg_str(a, "view"));
    Pt at = arg_pt2(a, "at");
    Pt pos = arg_pt2(a, "pos");
    std::string item = arg_str(a, "item");
    if (!has(a, "component") || !a["component"].is_array())
        throw CmdError("BAD_ARGS", "component 须为组件路径名列表");

    // TODO(实机核对): 首选 TOOLKIT 的 BOM 球标 API（ProDrawingBomballoonCreate / ProBomballoonCreate），
    //   它们通常要求先有 BOM 表/重复区域且按整体生成，签名无法确认，当前直接走降级路线：
    //   带引线的注释写序号（无圆圈），引线指向 at 处的模型边；应答带 degraded 标记。
    ProSelection sel = pick_edge_at(d, view, at);
    int id;
    try {
        id = create_note(d, {item}, pos, 3.5, view, sel, &at);
    } catch (...) {
        ProSelectionFree(&sel);
        throw;
    }
    ProSelectionFree(&sel);
    return json{{"id", id}, {"degraded", "note_with_leader"}};
}

// drw.readback：尺寸数/注释数/表格文字/视图名/符号数
json cmd_drw_readback(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    json out;

    // TODO(实机核对): 图纸尺寸遍历 ProDrawingDimensionVisit(drawing, action, filter, data)（或 ProSolidDimensionVisit）
    CountCtx dims;
    check(ProDrawingDimensionVisit(d, (ProDimensionVisitAction)count_visit, nullptr, &dims),
          "ProDrawingDimensionVisit");
    out["dims"] = dims.count;

    // TODO(实机核对): ProDtlnoteVisit 的 owner 参数；注释计数是否含表格单元格注释需实机确认
    CountCtx notes;
    check(ProDtlnoteVisit((ProDtlnoteOwner*)nullptr, nullptr, (ProDtlnoteVisitAction)count_visit, nullptr, &notes),
          "ProDtlnoteVisit");
    out["notes"] = notes.count;

    // TODO(实机核对): ProDtlsyminstVisit 签名
    CountCtx syms;
    check(ProDtlsyminstVisit((ProDtlsyminstOwner*)nullptr, nullptr, (ProDtlsyminstVisitAction)count_visit, nullptr,
                             &syms),
          "ProDtlsyminstVisit");
    out["symbols"] = syms.count;

    std::vector<std::string> vnames;
    g_readback_drawing = d;
    // TODO(实机核对): ProDrawingViewVisit(drawing, action, filter, data)
    check(ProDrawingViewVisit(d, (ProDrawingViewVisitAction)view_name_visit, nullptr, &vnames),
          "ProDrawingViewVisit");
    out["views"] = vnames;

    json tables = json::array();
    ProDwgtable* tabs = nullptr;
    // TODO(实机核对): ProDrawingTablesCollect(drawing, &array)
    if (ProDrawingTablesCollect(d, &tabs) == PRO_TK_NO_ERROR && tabs) {
        int nt = 0;
        ProArraySizeGet((ProArray)tabs, &nt);
        for (int i = 0; i < nt; ++i) {
            int nr = 0, nc = 0;
            // TODO(实机核对): ProDwgtableRowsCount / ProDwgtableColumnsCount 名称
            check(ProDwgtableRowsCount(&tabs[i], &nr), "ProDwgtableRowsCount");
            check(ProDwgtableColumnsCount(&tabs[i], &nc), "ProDwgtableColumnsCount");
            json rows = json::array();
            for (int r = 1; r <= nr; ++r) {
                json row = json::array();
                for (int c = 1; c <= nc; ++c) row.push_back(table_cell_text(&tabs[i], c, r));
                rows.push_back(row);
            }
            tables.push_back(json{{"rows", rows}});
        }
        ProArrayFree((ProArray*)&tabs);
    }
    out["tables"] = tables;
    return out;
}

ProError axis_collect(ProAxis axis, ProError /*status*/, ProAppData data) {
    static_cast<std::vector<ProAxis>*>(data)->push_back(axis);
    return PRO_TK_NO_ERROR;
}

// drw.show_axes：在视图中显示零件的基准轴（孔、圆柱的中心线，GB 孔须画中心线）。
// TODO(实机核对): ProAnnotationShow 对 PRO_AXIS 模型项是否有效（Creo 7/9/10）；
//   不行时改用"显示模型注释"对应的 API，或在 drawing setup 打开 show_axes_for_extr_arcs。
json cmd_drw_show_axes(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    std::string vname = arg_str(a, "view");
    ProView view = find_view(d, vname);
    ProSolid solid = nullptr;
    check(ProDrawingViewSolidGet(d, view, &solid), "ProDrawingViewSolidGet");
    std::vector<ProAxis> axes;
    // TODO(实机核对): ProSolidAxisVisit(solid, visit, filter, data)
    ProSolidAxisVisit(solid, (ProAxisVisitAction)axis_collect, nullptr, &axes);
    int shown = 0, failed = 0;
    for (ProAxis ax : axes) {
        int id = -1;
        if (ProAxisIdGet(ax, &id) != PRO_TK_NO_ERROR) { ++failed; continue; }
        ProModelitem mi;
        if (ProModelitemInit((ProMdl)solid, id, PRO_AXIS, &mi) != PRO_TK_NO_ERROR) { ++failed; continue; }
        if (ProAnnotationShow((ProAnnotation*)&mi, nullptr, view) == PRO_TK_NO_ERROR) ++shown; else ++failed;
    }
    return json{{"view", vname}, {"axes", (int)axes.size()}, {"shown", shown}, {"failed", failed}};
}

}  // namespace

void register_drawing_cmds() {
    register_cmd("drw.note", cmd_drw_note);
    register_cmd("drw.dim", cmd_drw_dim);
    register_cmd("drw.datum", cmd_drw_datum);
    register_cmd("drw.gtol", cmd_drw_gtol);
    register_cmd("drw.surface_finish", cmd_drw_surface_finish);
    register_cmd("drw.table", cmd_drw_table);
    register_cmd("drw.section", cmd_drw_section);
    register_cmd("drw.removed_section", cmd_drw_removed_section);
    register_cmd("drw.detail_view", cmd_drw_detail_view);
    register_cmd("drw.balloon", cmd_drw_balloon);
    register_cmd("drw.readback", cmd_drw_readback);
    register_cmd("drw.show_axes", cmd_drw_show_axes);
}

}  // namespace cadmcp
