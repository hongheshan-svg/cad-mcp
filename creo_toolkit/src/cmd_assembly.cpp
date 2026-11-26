// 装配命令（docs/CREO_TOOLKIT.md §3.4）：asm.new / asm.add_component / asm.interference。
//
// 坐标约定同 cmd_model.cpp：协议给 IR 坐标（Z 向上），Creo 坐标 = Q·p，Q: (x, y, z) → (x, z, −y)。
// 元件位姿 T（IR，列向量 p_asm = T·p_comp）→ Creo：T_creo = Q·T·Qᵀ，再转成 ProMatrix（行向量约定：
// 前三行为元件 X/Y/Z 轴在装配中的方向，第 4 行为原点）。
//
// 放置：给了 csys 时建"坐标系重合"约束（PRO_ASM_CSYS），失败或未给时按 transform 固定放置（PRO_ASM_FIX）。
// 服务端在进程内记住"装配名 / 实例 id → 元件特征 id"，供后续元件以 target 引用（单线程，无需加锁）。
// 未能在本机编译核对的 TOOLKIT 签名、枚举名标 TODO(实机核对)。
#include <algorithm>
#include <array>
#include <cwchar>
#include <map>
#include <string>
#include <vector>

extern "C" {
#include <ProArray.h>
#include <ProAsmcomp.h>
#include <ProAsmcomppath.h>
#include <ProCsys.h>
#include <ProFeature.h>
#include <ProGeomitem.h>
#include <ProInterference.h>
#include <ProModelitem.h>
#include <ProSelection.h>
#include <ProSolid.h>
#include <ProUtil.h>
}

#include "util.h"

namespace cadmcp {
namespace {

using M4 = std::array<std::array<double, 4>, 4>;

// 装配名 → (实例 id → 元件特征 id)
std::map<std::string, std::map<std::string, int>> g_instances;

M4 arg_mat4(const json& a, const char* key) {
    if (!has(a, key) || !a[key].is_array() || a[key].size() != 4)
        throw CmdError("BAD_ARGS", std::string(key) + " 应为 4×4 数组");
    M4 m{};
    for (int i = 0; i < 4; ++i) {
        const json& r = a[key][i];
        if (!r.is_array() || r.size() != 4) throw CmdError("BAD_ARGS", std::string(key) + " 每行应有 4 个数");
        for (int j = 0; j < 4; ++j) m[i][j] = r[j].get<double>();
    }
    return m;
}

// T_creo = Q·T·Qᵀ；Q 的行：Creo x = IR x，Creo y = IR z，Creo z = −IR y
M4 ir_to_creo(const M4& t) {
    static const double Q[3][3] = {{1, 0, 0}, {0, 0, 1}, {0, -1, 0}};
    M4 out{};
    for (int i = 0; i < 3; ++i) {
        for (int j = 0; j < 3; ++j) {
            double s = 0;
            for (int k = 0; k < 3; ++k)
                for (int l = 0; l < 3; ++l) s += Q[i][k] * t[k][l] * Q[j][l];
            out[i][j] = s;
        }
        double s = 0;
        for (int k = 0; k < 3; ++k) s += Q[i][k] * t[k][3];
        out[i][3] = s;
    }
    out[3][3] = 1;
    return out;
}

// 列向量矩阵（mm）→ ProMatrix（行向量，模型单位）
void to_promatrix(const M4& c, double k, ProMatrix out) {
    for (int i = 0; i < 3; ++i) {
        for (int j = 0; j < 3; ++j) out[i][j] = c[j][i];  // 第 i 行 = 元件第 i 轴在装配中的方向
        out[i][3] = 0;
    }
    for (int j = 0; j < 3; ++j) out[3][j] = c[j][3] * k;
    out[3][3] = 1;
}

std::string base_name(const std::string& path) {
    size_t sl = path.find_last_of("/\\");
    std::string fn = sl == std::string::npos ? path : path.substr(sl + 1);
    size_t dot = fn.find('.');
    return dot == std::string::npos ? fn : fn.substr(0, dot);
}

ProMdl load_component(const std::string& file) {
    std::string base = base_name(file);
    std::string lower = file;
    std::transform(lower.begin(), lower.end(), lower.begin(), [](unsigned char c) { return (char)std::tolower(c); });
    ProMdlfileType ft = lower.find(".asm") != std::string::npos ? PRO_MDLFILE_ASSEMBLY : PRO_MDLFILE_PART;
    std::wstring w = widen(file);
    if (w.size() > PRO_PATH_SIZE - 1) throw CmdError("BAD_ARGS", "路径过长");
    ProPath path;
    std::wmemcpy(path, w.c_str(), w.size() + 1);
    ProMdl m = nullptr;
    // TODO(实机核对): ProMdlLoad(ProPath, ProMdlfileType, ProBoolean, ProMdl*)（与 cmd_session.cpp 一致）
    ProError e = ProMdlLoad(path, ft, PRO_B_FALSE, &m);
    if (e == PRO_TK_E_FOUND || e != PRO_TK_NO_ERROR) {
        try {
            return find_model(base);  // 已在会话中
        } catch (const CmdError&) {
            check(e, "ProMdlLoad");
        }
    }
    return m;
}

ProSolid find_asm(const std::string& name) {
    ProMdl m = find_model(name);
    if (model_type_name(m) != "assembly") throw CmdError("BAD_ARGS", "不是装配: " + name);
    return reinterpret_cast<ProSolid>(m);
}

// 在实体（零件或装配顶层）中按名字找坐标系，返回其 ProGeomitem
bool find_csys(ProSolid s, const std::string& name, ProModelitem* out) {
    ProName pn;
    to_proname(name, pn);
    // TODO(实机核对): ProModelitemByNameInit(ProMdl, ProType, ProName, ProModelitem*)
    return ProModelitemByNameInit(reinterpret_cast<ProMdl>(s), PRO_CSYS, pn, out) == PRO_TK_NO_ERROR;
}

// 坐标系重合约束：元件坐标系 ref ↔ 目标（装配本身或已放元件）的坐标系 target_ref
bool constrain_csys(ProSolid asm_s, ProAsmcomp* comp, ProMdl comp_mdl, const json& c) {
    std::string ref = arg_str(c, "ref"), target_ref = arg_str(c, "target_ref");
    ProModelitem comp_cs, tgt_cs;
    if (!find_csys(reinterpret_cast<ProSolid>(comp_mdl), ref, &comp_cs)) return false;

    // 目标：null = 装配顶层；否则为已放元件，经元件路径选取其坐标系
    ProAsmcomppath tgt_path;
    int ids[1];
    int depth = 0;
    ProSolid tgt_owner = asm_s;
    if (has(c, "target")) {
        auto& inst = g_instances[model_name(reinterpret_cast<ProMdl>(asm_s))];
        auto it = inst.find(arg_str(c, "target"));
        if (it == inst.end()) return false;
        ids[0] = it->second;
        depth = 1;
        ProAsmcomp tc;
        tc.owner = asm_s;
        tc.type = PRO_FEATURE;
        tc.id = it->second;
        ProMdl tm;
        // TODO(实机核对): ProAsmcompMdlGet(ProAsmcomp*, ProMdl*)
        if (ProAsmcompMdlGet(&tc, &tm) != PRO_TK_NO_ERROR) return false;
        tgt_owner = reinterpret_cast<ProSolid>(tm);
    }
    if (!find_csys(tgt_owner, target_ref, &tgt_cs)) return false;
    // TODO(实机核对): ProAsmcomppathInit(ProSolid root, ProIdTable, int depth, ProAsmcomppath*)
    check(ProAsmcomppathInit(asm_s, ids, depth, &tgt_path), "ProAsmcomppathInit");

    ProSelection comp_sel, tgt_sel;
    check(ProSelectionAlloc(NULL, &comp_cs, &comp_sel), "ProSelectionAlloc(comp csys)");
    check(ProSelectionAlloc(&tgt_path, &tgt_cs, &tgt_sel), "ProSelectionAlloc(target csys)");

    ProAsmcompconstraint cst;
    check(ProAsmcompconstraintAlloc(&cst), "ProAsmcompconstraintAlloc");
    // TODO(实机核对): 枚举名 PRO_ASM_CSYS；参照设置 ProAsmcompconstraintAsmreferenceSet / CompreferenceSet 的 attr 参数
    ProAsmcompconstraintTypeSet(cst, PRO_ASM_CSYS);
    ProAsmcompconstraintAsmreferenceSet(cst, tgt_sel, PRO_DATUM_SIDE_YELLOW);
    ProAsmcompconstraintCompreferenceSet(cst, comp_sel, PRO_DATUM_SIDE_YELLOW);
    ProAsmcompconstraint* arr = NULL;
    ProArrayAlloc(0, sizeof(ProAsmcompconstraint), 1, reinterpret_cast<ProArray*>(&arr));
    ProArrayObjectAdd(reinterpret_cast<ProArray*>(&arr), PRO_VALUE_UNUSED, 1, &cst);
    ProError e = ProAsmcompConstraintsSet(NULL, comp, arr);  // TODO(实机核对): 第一个参数为装配上下文路径
    ProArrayFree(reinterpret_cast<ProArray*>(&arr));
    ProSelectionFree(&comp_sel);
    ProSelectionFree(&tgt_sel);
    if (e != PRO_TK_NO_ERROR) return false;
    // TODO(实机核对): ProAsmcompRegenerate(ProAsmcomp*, ProBoolean)；约束未满足时返回错误 → 退回固定放置
    return ProAsmcompRegenerate(comp, PRO_B_FALSE) == PRO_TK_NO_ERROR;
}

void fix_in_place(ProAsmcomp* comp) {
    ProAsmcompconstraint cst;
    check(ProAsmcompconstraintAlloc(&cst), "ProAsmcompconstraintAlloc");
    // TODO(实机核对): 固定约束枚举名 PRO_ASM_FIX（Creo 中为"固定"，保持 ProAsmcompAssemble 给的初始位姿）
    ProAsmcompconstraintTypeSet(cst, PRO_ASM_FIX);
    ProAsmcompconstraint* arr = NULL;
    ProArrayAlloc(0, sizeof(ProAsmcompconstraint), 1, reinterpret_cast<ProArray*>(&arr));
    ProArrayObjectAdd(reinterpret_cast<ProArray*>(&arr), PRO_VALUE_UNUSED, 1, &cst);
    ProError e = ProAsmcompConstraintsSet(NULL, comp, arr);
    ProArrayFree(reinterpret_cast<ProArray*>(&arr));
    check(e, "ProAsmcompConstraintsSet(FIX)");
}

// ---------------- 命令 ----------------
json cmd_asm_new(const json& a) {
    std::string name = arg_str(a, "name");
    std::string start = has(a, "start_asm") ? arg_str(a, "start_asm") : std::string();
    ProName pn;
    to_proname(name, pn);
    if (start.empty()) {
        ProSolid s = nullptr;
        // TODO(实机核对): ProSolidCreate(ProName, PRO_MDL_ASSEMBLY, ProSolid*)；空白装配无默认基准与 ASM_DEF_CSYS，
        //   需要坐标系约束时请传 start_asm；单位制设置同 cmd_session.cpp 的 mmNs
        check(ProSolidCreate(pn, PRO_MDL_ASSEMBLY, &s), "ProSolidCreate(assembly)");
    } else {
        ProMdl src = start.find_first_of("/\\") != std::string::npos ? load_component(start) : find_model(start);
        check(ProMdlCopy(src, pn), "ProMdlCopy");  // TODO(实机核对): 同 cmd_session.cpp
    }
    ProMdl m = find_model(name);
    ProMdlDisplay(m);
    g_instances[model_name(m)].clear();
    return json{{"name", model_name(m)}};
}

json cmd_asm_add_component(const json& a) {
    ProSolid asm_s = find_asm(arg_str(a, "asm"));
    std::string inst = arg_str(a, "name");
    ProMdl cm = load_component(arg_str(a, "file"));
    double k = model_mm_factor(reinterpret_cast<ProMdl>(asm_s));
    ProMatrix init;
    to_promatrix(ir_to_creo(arg_mat4(a, "transform")), k, init);
    ProAsmcomp comp;
    // TODO(实机核对): ProAsmcompAssemble(ProAssembly, ProSolid, ProMatrix init_pos, ProAsmcomp*)
    check(ProAsmcompAssemble(reinterpret_cast<ProAssembly>(asm_s), reinterpret_cast<ProSolid>(cm), init, &comp),
          "ProAsmcompAssemble");
    std::string placement = "fixed";
    if (has(a, "csys") && a["csys"].is_object() && constrain_csys(asm_s, &comp, cm, a["csys"])) placement = "csys";
    else fix_in_place(&comp);
    g_instances[model_name(reinterpret_cast<ProMdl>(asm_s))][inst] = comp.id;
    return json{{"id", comp.id}, {"placement", placement}};
}

std::string instance_of(const std::string& asm_name, int comp_id) {
    for (const auto& kv : g_instances[asm_name])
        if (kv.second == comp_id) return kv.first;
    return "#" + std::to_string(comp_id);
}

json cmd_asm_interference(const json& a) {
    std::string name = arg_str(a, "asm");
    ProSolid asm_s = find_asm(name);
    ProInterferenceInfo* info = NULL;
    // TODO(实机核对): ProFitGlobalinterferenceCompute(ProAssembly, ProInterferenceComputeType, ProBoolean, ProBoolean,
    //   ProBoolean, ProInterferenceInfo**)：只算零件间实体干涉；返回 ProArray
    check(ProFitGlobalinterferenceCompute(reinterpret_cast<ProAssembly>(asm_s), PRO_FIT_PART, PRO_B_FALSE, PRO_B_FALSE,
                                          PRO_B_FALSE, &info),
          "ProFitGlobalinterferenceCompute");
    json pairs = json::array();
    int n = 0;
    if (info) ProArraySizeGet(info, &n);
    double k = model_mm_factor(reinterpret_cast<ProMdl>(asm_s));
    for (int i = 0; i < n; ++i) {
        double vol = 0;
        // TODO(实机核对): ProFitInterferencevolumeCompute(ProInterferenceData, double*)；part_1/part_2 为 ProSelection
        ProFitInterferencevolumeCompute(info[i].interf_data, &vol);
        ProAsmcomppath p1, p2;
        ProSelectionAsmcomppathGet(info[i].part_1, &p1);
        ProSelectionAsmcomppathGet(info[i].part_2, &p2);
        std::string a1 = p1.table_num > 0 ? instance_of(name, p1.comp_id_table[0]) : "?";
        std::string a2 = p2.table_num > 0 ? instance_of(name, p2.comp_id_table[0]) : "?";
        pairs.push_back({{"components", {a1, a2}}, {"volume_mm3", vol / (k * k * k)}});
    }
    if (info) ProInterferenceInfoProarrayFree(info);  // TODO(实机核对): 释放函数名
    return json{{"pairs", pairs}};
}

}  // namespace

void register_assembly_cmds() {
    register_cmd("asm.new", cmd_asm_new);
    register_cmd("asm.add_component", cmd_asm_add_component);
    register_cmd("asm.interference", cmd_asm_interference);
}

}  // namespace cadmcp
