// 会话命令（docs/CREO_TOOLKIT.md §3.1）：ping / model.open / model.new_part / model.regenerate /
// model.save / model.feature_status / model.param_set
// 本文件凭 TOOLKIT API 记忆编写，不确定处标 TODO(实机核对)，汇总在 creo_toolkit/README.md。
#include <algorithm>
#include <cwchar>

extern "C" {
#include <ProArray.h>
#include <ProFeature.h>
#include <ProParamval.h>
#include <ProParameter.h>
#include <ProUnit.h>
}

#include "util.h"

namespace cadmcp {

namespace {

ProSolid need_solid(ProMdl m, const std::string& name) {
    std::string t = model_type_name(m);
    if (t != "part" && t != "assembly") throw CmdError("BAD_ARGS", "模型不是零件/装配: " + name);
    return reinterpret_cast<ProSolid>(m);
}

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

// 取文件名（去目录）与扩展名（小写，不含点）；忽略 .prt.3 这样的版本后缀
void split_path(const std::string& path, std::string* base, std::string* ext) {
    size_t sl = path.find_last_of("/\\");
    std::string fn = (sl == std::string::npos) ? path : path.substr(sl + 1);
    size_t dot = fn.rfind('.');
    if (dot != std::string::npos && dot + 1 < fn.size() &&
        std::all_of(fn.begin() + dot + 1, fn.end(), [](unsigned char c) { return std::isdigit(c); }))
        fn = fn.substr(0, dot);
    dot = fn.rfind('.');
    if (dot == std::string::npos) {
        *base = fn;
        ext->clear();
    } else {
        *base = fn.substr(0, dot);
        *ext = lower(fn.substr(dot + 1));
    }
}

ProMdlfileType file_type_of_ext(const std::string& ext) {
    if (ext == "prt") return PRO_MDLFILE_PART;
    if (ext == "asm") return PRO_MDLFILE_ASSEMBLY;
    if (ext == "drw") return PRO_MDLFILE_DRAWING;
    throw CmdError("BAD_ARGS", "不支持的文件扩展名（应为 .prt/.asm/.drw）: ." + ext);
}

const char* ext_of_type(const std::string& t) {
    if (t == "part") return "prt";
    if (t == "assembly") return "asm";
    if (t == "drawing") return "drw";
    return "";
}

// 把新建零件的主单位制设为 mmNs
void set_unitsystem_mmns(ProMdl m) {
    ProUnitsystem* systems = nullptr;
    // TODO(实机核对): ProMdlUnitsystemsCollect(ProMdl, ProUnitsystem** ProArray) 的签名
    check(ProMdlUnitsystemsCollect(m, &systems), "ProMdlUnitsystemsCollect");
    int n = 0;
    ProArraySizeGet(systems, &n);
    int idx = -1;
    for (int i = 0; i < n; ++i)
        if (narrow(systems[i].name) == "mmNs") idx = i;  // 标准单位制名 "mmNs"（毫米 牛顿 秒）
    ProError e = PRO_TK_E_NOT_FOUND;
    if (idx >= 0) {
        // TODO(实机核对): ProMdlPrincipalunitsystemSet 的参数（此处：mdl, 单位制, 转换方式, 选项）与枚举名
        e = ProMdlPrincipalunitsystemSet(m, &systems[idx], PRO_UNITCONVERT_SAME_SIZE, nullptr);
    }
    ProArrayFree(reinterpret_cast<ProArray*>(&systems));
    check(e, "设置单位制 mmNs 失败");
}

// 模型所在目录（UTF-8）；取不到返回空
std::string model_dir(ProMdl m) {
    ProMdldata data;
    // TODO(实机核对): ProMdlDataGet(ProMdl, ProMdldata*) 及其 path 字段
    if (ProMdlDataGet(m, &data) != PRO_TK_NO_ERROR) return std::string();
    return narrow(data.path);
}

std::string feat_type_name(ProFeature* f) {
    ProFeattype t;
    if (ProFeatureTypeGet(f, &t) != PRO_TK_NO_ERROR) return "unknown";
    // TODO(实机核对): 常量名；未列出的回落为 FEAT_<数字>
    switch (t) {
        case PRO_FEAT_FIRST_FEAT: return "first_feat";
        case PRO_FEAT_PROTRUSION: return "protrusion";
        case PRO_FEAT_CUT: return "cut";
        case PRO_FEAT_HOLE: return "hole";
        case PRO_FEAT_ROUND: return "round";
        case PRO_FEAT_CHAMFER: return "chamfer";
        case PRO_FEAT_DATUM: return "datum_plane";
        case PRO_FEAT_DATUM_AXIS: return "datum_axis";
        case PRO_FEAT_DATUM_POINT: return "datum_point";
        case PRO_FEAT_CSYS: return "csys";
        case PRO_FEAT_CURVE: return "curve";
        default: return "FEAT_" + std::to_string(static_cast<int>(t));
    }
}

// 返回 "active|inactive|suppressed|unregenerated|unknown"
std::string feat_status_name(ProFeature* f) {
    ProFeatStatus s;
    if (ProFeatureStatusGet(f, &s) != PRO_TK_NO_ERROR) return "unknown";
    switch (s) {
        case PRO_FEAT_ACTIVE: return "active";
        case PRO_FEAT_INACTIVE: return "inactive";
        case PRO_FEAT_UNREGENERATED: return "unregenerated";
        case PRO_FEAT_SUPPRESSED:
        case PRO_FEAT_FAMTAB_SUPPRESSED:
        case PRO_FEAT_SIMP_REP_SUPPRESSED:
        case PRO_FEAT_PROG_SUPPRESSED: return "suppressed";
        default: return "unknown";
    }
}

std::string feat_label(ProFeature* f) {
    std::string n = feature_name(f);
    return n.empty() ? "#" + std::to_string(f->id) : n;
}

// ---------------- 命令实现 ----------------

json cmd_ping(const json&) {
    return json{{"creo", creo_release()}, {"toolkit", "cadmcp_tk 0.1 (async)"}};
}

json cmd_open(const json& a) {
    std::string file = arg_str(a, "file");
    std::string base, ext;
    split_path(file, &base, &ext);
    ProMdlfileType ft = file_type_of_ext(ext);
    std::wstring wpath = widen(file);
    if (wpath.size() > PRO_PATH_SIZE - 1) throw CmdError("BAD_ARGS", "路径过长");
    ProPath path;
    std::wmemcpy(path, wpath.c_str(), wpath.size() + 1);

    ProMdl m = nullptr;
    // TODO(实机核对): ProMdlLoad(ProPath, ProMdlfileType, ProBoolean unused, ProMdl*)
    ProError e = ProMdlLoad(path, ft, PRO_B_FALSE, &m);
    if (e == PRO_TK_E_FOUND) m = find_model(base);  // 已在会话中
    else check(e, "ProMdlLoad");
    // 显示到窗口（出图/再生通常需要）。TODO(实机核对): ProMdlDisplay 需当前窗口；失败不致命，仅记录
    ProMdlDisplay(m);
    return json{{"name", model_name(m)}, {"type", model_type_name(m)}};
}

json cmd_new_part(const json& a) {
    std::string name = arg_str(a, "name");
    std::string start = has(a, "start_part") ? arg_str(a, "start_part") : std::string();
    ProName pn;
    to_proname(name, pn);
    ProMdl m = nullptr;

    if (start.empty()) {
        // 空白零件。TODO(实机核对): ProSolidCreate(ProName, ProMdlType, ProSolid*)；
        //   新版可能为 ProSolidMdlnameCreate(ProMdlName, ProMdlfileType, ProSolid*)
        //   注意：空白零件没有默认基准面（TOP/FRONT/RIGHT），建模命令依赖基准面时推荐传 start_part（带默认基准的模板）
        ProSolid s = nullptr;
        check(ProSolidCreate(pn, PRO_MDL_PART, &s), "ProSolidCreate");
        m = reinterpret_cast<ProMdl>(s);
        set_unitsystem_mmns(m);
    } else {
        // start_part：会话中已有的模型名，或完整路径（会先加载）
        ProMdl src = nullptr;
        if (start.find_first_of("/\\") != std::string::npos) {
            json oa = {{"file", start}};
            cmd_open(oa);
            std::string b, e;
            split_path(start, &b, &e);
            src = find_model(b);
        } else {
            src = find_model(start);
        }
        // TODO(实机核对): ProMdlCopy(ProMdl, ProName new_name) 参数表；
        //   备选：ProMdlfileCopy(type, 源名, 新名) 作用于磁盘文件
        check(ProMdlCopy(src, pn), "ProMdlCopy");
        m = find_model(name);
    }
    ProMdlDisplay(m);
    return json{{"name", model_name(m)}};
}

json cmd_regenerate(const json& a) {
    std::string name = arg_str(a, "name");
    ProSolid s = need_solid(find_model(name), name);
    // TODO(实机核对): ProSolidRegenerate(ProSolid, int mode)；PRO_REGEN_NO_RESOLVE_MODE 避免失败时弹出“解决特征”交互
    ProError e = ProSolidRegenerate(s, PRO_REGEN_NO_RESOLVE_MODE);
    json failed = json::array();
    if (e != PRO_TK_NO_ERROR) {
        // TODO(实机核对): ProFeatureStatusGet 本身没有“失败”枚举；这里把 UNREGENERATED 视为失败。
        //   若 TOOLKIT 提供失败特征列表函数（如 ProSolidFailedFeaturesList），应改用它。
        for (auto& f : list_features(s))
            if (feat_status_name(&f) == "unregenerated") failed.push_back(feat_label(&f));
    }
    return json{{"status", e == PRO_TK_NO_ERROR ? "ok" : "errors"}, {"failed", failed},
                {"code", pro_err_name(e)}};
}

json cmd_save(const json& a) {
    std::string name = arg_str(a, "name");
    std::string dir = has(a, "dir") ? arg_str(a, "dir") : std::string();
    ProMdl m = find_model(name);
    std::string type = model_type_name(m);
    std::string fn = model_name(m) + "." + ext_of_type(type);
    if (dir.empty()) {
        check(ProMdlSave(m), "ProMdlSave");
        std::string d = model_dir(m);
        return json{{"path", d.empty() ? fn : d + "/" + fn}};
    }
    // 另存副本到指定目录。TODO(实机核对): ProMdlBackup(ProMdl, ProPath target_dir)；目录需已存在
    std::wstring wd = widen(dir);
    if (wd.size() > PRO_PATH_SIZE - 1) throw CmdError("BAD_ARGS", "路径过长");
    ProPath pd;
    std::wmemcpy(pd, wd.c_str(), wd.size() + 1);
    check(ProMdlBackup(m, pd), "ProMdlBackup");
    std::string sep = (dir.back() == '/' || dir.back() == '\\') ? "" : "/";
    return json{{"path", dir + sep + fn}};
}

json cmd_feature_status(const json& a) {
    std::string name = arg_str(a, "name");
    ProSolid s = need_solid(find_model(name), name);
    json arr = json::array();
    for (auto& f : list_features(s))
        arr.push_back({{"name", feature_name(&f)}, {"id", f.id}, {"type", feat_type_name(&f)},
                       {"status", feat_status_name(&f)}});
    return json{{"features", arr}};
}

json cmd_param_set(const json& a) {
    std::string name = arg_str(a, "name");
    std::string pname = arg_str(a, "param");
    std::string type = arg_str(a, "type");
    if (!a.contains("value") || a["value"].is_null()) throw CmdError("BAD_ARGS", "缺少参数: value");
    const json& v = a["value"];
    ProMdl m = find_model(name);

    ProParamvalue pv;
    // TODO(实机核对): ProParamvalue 的字段名（type / value.s_val / d_val / i_val / l_val）与 PRO_PARAM_* 枚举
    if (type == "string") {
        if (!v.is_string()) throw CmdError("BAD_ARGS", "value 应为字符串");
        pv.type = PRO_PARAM_STRING;
        to_line(v.get<std::string>(), pv.value.s_val);
    } else if (type == "double") {
        if (!v.is_number()) throw CmdError("BAD_ARGS", "value 应为数字");
        pv.type = PRO_PARAM_DOUBLE;
        pv.value.d_val = v.get<double>();
    } else if (type == "int") {
        if (!v.is_number()) throw CmdError("BAD_ARGS", "value 应为整数");
        pv.type = PRO_PARAM_INTEGER;
        pv.value.i_val = v.get<int>();
    } else if (type == "bool") {
        if (!v.is_boolean()) throw CmdError("BAD_ARGS", "value 应为布尔");
        pv.type = PRO_PARAM_BOOLEAN;
        pv.value.l_val = v.get<bool>() ? 1 : 0;
    } else {
        throw CmdError("BAD_ARGS", "type 应为 string/double/int/bool");
    }

    ProModelitem owner;
    check(ProMdlToModelitem(m, &owner), "ProMdlToModelitem");
    ProName pn;
    to_proname(pname, pn);
    ProParameter param;
    // 已存在则改值，否则新建。TODO(实机核对): ProParameterInit(ProModelitem*, ProName, ProParameter*) 不存在时返回 PRO_TK_E_NOT_FOUND
    ProError e = ProParameterInit(&owner, pn, &param);
    if (e == PRO_TK_NO_ERROR) {
        check(ProParameterValueSet(&param, &pv), "ProParameterValueSet");
    } else if (e == PRO_TK_E_NOT_FOUND) {
        check(ProParameterCreate(&owner, pn, &pv, &param), "ProParameterCreate");
    } else {
        check(e, "ProParameterInit");
    }
    return json::object();
}

}  // namespace

void register_session_cmds() {
    register_cmd("ping", cmd_ping);
    register_cmd("model.open", cmd_open);
    register_cmd("model.new_part", cmd_new_part);
    register_cmd("model.regenerate", cmd_regenerate);
    register_cmd("model.save", cmd_save);
    register_cmd("model.feature_status", cmd_feature_status);
    register_cmd("model.param_set", cmd_param_set);
}

}  // namespace cadmcp
