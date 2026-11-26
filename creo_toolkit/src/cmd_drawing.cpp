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
#include <ProArray.h>
#include <ProUtil.h>
}

#include <array>
#include <cmath>
#include <cstdio>
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

// drw.dim
json cmd_drw_dim(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    ProView view = find_view(d, arg_str(a, "view"));
    std::string kind = arg_str(a, "kind");
    Pt p1 = arg_pt2(a, "p1");
    Pt at = arg_pt2(a, "at");
    std::string orient = has(a, "orient") ? arg_str(a, "orient") : "aligned";
    std::string text = has(a, "text") ? arg_str(a, "text") : "";

    if (kind != "linear" && kind != "diameter" && kind != "radius" && kind != "ordinate")
        throw CmdError("BAD_ARGS", "kind 须为 linear|diameter|radius|ordinate");
    if (orient != "h" && orient != "v" && orient != "aligned")
        throw CmdError("BAD_ARGS", "orient 须为 h|v|aligned");

    ProSelection sels[2] = {nullptr, nullptr};
    int nsel = 0;
    auto free_sels = [&]() { for (int i = 0; i < nsel; ++i) if (sels[i]) ProSelectionFree(&sels[i]); };
    sels[nsel++] = pick_edge_at(d, view, p1);
    ProDimension dim;
    try {
        if (kind == "linear" || kind == "ordinate") {
            Pt p2 = arg_pt2(a, "p2");
            sels[nsel++] = pick_edge_at(d, view, p2);
        }
        // TODO(实机核对): ProDrawingDimensionCreate 真实签名（疑为 drawing, sels[], attachments[], senses[], orient, location, &dim）；
        //   ProDimAttachment / ProDimSense 取值。diameter/radius 仅 1 个选择（圆弧/圆边）；
        //   linear 的端点型附着（PRO_DIM_ATTACH_END1/END2/MIDDLE）需按边方向实机调试。
        ProDimAttachment atts[2] = {PRO_DIM_ATTACH_MIDDLE, PRO_DIM_ATTACH_MIDDLE};
        ProDimSense senses[2];
        senses[0].type = senses[1].type = PRO_DIM_SNS_NONE;  // TODO(实机核对): ProDimSense 结构字段
        ProDimOrient ori = PRO_DIM_ORNT_HORIZ;
        if (orient == "v") ori = PRO_DIM_ORNT_VERT;
        else if (orient == "aligned") ori = PRO_DIM_ORNT_SLANTED;  // TODO(实机核对): 枚举名
        ProPoint3d loc;
        sheet_mm_to_drw(d, at, loc);
        check(ProDrawingDimensionCreate(d, sels, atts, senses, ori, loc, &dim), "ProDrawingDimensionCreate");
    } catch (...) {
        free_sels();
        throw;
    }
    free_sels();

    // 文字覆盖（如 Φ20k6）；diameter/radius 的 Φ/R 符号由 Creo 按附着圆边自动加
    if (!text.empty()) {
        std::wstring w = widen(text);
        // TODO(实机核对): ProDimensionTextWstringsSet(ProDimension*, ProWstring* lines)——lines 为 ProArray；
        //   也可改用 ProDimensionTextSet
        ProArray arr;
        check(ProArrayAlloc(1, sizeof(ProWstring), 1, &arr), "ProArrayAlloc");
        ((ProWstring*)arr)[0] = (ProWstring)w.c_str();
        ProError e = ProDimensionTextWstringsSet(&dim, (ProWstring*)arr);
        ProArrayFree(&arr);
        check(e, "ProDimensionTextWstringsSet");
    }
    return json{{"id", dim.id}};
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

// drw.section：为 parent 视图所属模型创建平面剖截面，并把 replaces 视图设为 2D 剖视（首期仅 full/half）
json cmd_drw_section(const json& a) {
    ProDrawing d = find_drawing(arg_str(a, "drawing"));
    std::string parent = arg_str(a, "parent");
    std::string label = arg_str(a, "label");
    std::string kind = arg_str(a, "kind");
    if (kind == "step")
        throw CmdError("PRO_TK_NOT_IMPLEMENTED",
                       "阶梯剖需要偏移剖截面（草绘折线 + 偏移 ProXsec），TOOLKIT 无稳定公开的创建接口，首期未实现");
    if (kind == "local")
        throw CmdError("PRO_TK_NOT_IMPLEMENTED", "局部剖需要样条边界 + 视图局部剖设置，首期未实现");
    if (kind != "full" && kind != "half") throw CmdError("BAD_ARGS", "kind 须为 full|half|step|local");

    ProView pv = find_view(d, parent);
    std::string replaces = has(a, "replaces") ? arg_str(a, "replaces") : "";
    if (replaces.empty())
        throw CmdError("BAD_ARGS", "首期要求 replaces 指向已存在的视图（由 CREOSON 先建投影视图），再由本命令设为剖视");
    if (has(a, "center")) (void)arg_pt2(a, "center");  // 仅校验格式；位置由已存在的视图决定

    // cut_points：模型坐标折线（mm）。full/half 取前两点，剖切平面为过该直线、平行于视图法向的平面。
    if (!has(a, "cut_points") || !a["cut_points"].is_array() || a["cut_points"].size() < 2)
        throw CmdError("BAD_ARGS", "cut_points 至少 2 个模型坐标点");
    std::vector<std::array<double, 3>> cp;
    for (const auto& pt : a["cut_points"]) {
        if (!pt.is_array() || pt.size() < 2) throw CmdError("BAD_ARGS", "cut_points 元素须为 [x,y(,z)]");
        cp.push_back({pt[0].get<double>(), pt[1].get<double>(), pt.size() > 2 ? pt[2].get<double>() : 0.0});
    }

    // 取视图所属模型
    ProSolid solid = nullptr;
    // TODO(实机核对): ProDrawingViewSolidGet? 或 ProViewModelGet(drawing, view, &mdl)
    check(ProDrawingViewSolidGet(d, pv, &solid), "ProDrawingViewSolidGet");

    // 1) 创建平面剖截面，名为 label
    // TODO(实机核对): TOOLKIT 创建截面的函数——ProXsec.h 以 ProXsecdata 为主，创建入口名与平面数据设置
    //   （以下 ProXsecdataAlloc/NameSet/PlanarSet/ProXsecCreate 均为推测）；若无，备选：建剖切基准面特征再引用。
    ProXsec xs;
    {
        ProXsecdata xd = nullptr;
        check(ProXsecdataAlloc(&xd), "ProXsecdataAlloc");
        std::wstring wl = widen(label);
        check(ProXsecdataNameSet(xd, (wchar_t*)wl.c_str()), "ProXsecdataNameSet");
        double f = model_mm_factor((ProMdl)solid);  // mm → 模型单位
        double p0[3], p1[3];
        for (int i = 0; i < 3; ++i) { p0[i] = cp[0][i] * f; p1[i] = cp[1][i] * f; }
        check(ProXsecdataPlanarSet(xd, p0, p1), "ProXsecdataPlanarSet");
        check(ProXsecCreate(solid, xd, &xs), "ProXsecCreate");
        ProXsecdataFree(xd);
    }

    // 2) 把 replaces 视图设为 2D 剖视
    ProView rv = find_view(d, replaces);
    // TODO(实机核对): ProDrawingView2DSectionSet 参数与剖面范围枚举 PRO_VIEW_SECTION_TOTAL
    check(ProDrawingView2DSectionSet(d, rv, &xs, PRO_VIEW_SECTION_TOTAL, nullptr), "ProDrawingView2DSectionSet");
    if (kind == "half") {
        // TODO(实机核对): 半剖需指定“半截面 + 参照平面 + 保留侧”，此处函数名为推测
        check(ProDrawingViewSectionAreaSet(d, rv, PRO_VIEW_SECTION_HALF), "ProDrawingViewSectionAreaSet(HALF)");
    }
    // TODO(实机核对): 剖切箭头/剖切线显示（ProDrawingViewSectionArrowsShow?）
    return json{{"xsec", label}, {"view", replaces}};
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

}  // namespace

void register_drawing_cmds() {
    register_cmd("drw.note", cmd_drw_note);
    register_cmd("drw.dim", cmd_drw_dim);
    register_cmd("drw.datum", cmd_drw_datum);
    register_cmd("drw.gtol", cmd_drw_gtol);
    register_cmd("drw.surface_finish", cmd_drw_surface_finish);
    register_cmd("drw.table", cmd_drw_table);
    register_cmd("drw.section", cmd_drw_section);
    register_cmd("drw.balloon", cmd_drw_balloon);
    register_cmd("drw.readback", cmd_drw_readback);
}

}  // namespace cadmcp
