"""CAD 版本识别与版本保护（防止新版本覆盖旧版本文件）。"""
from __future__ import annotations

from dataclasses import dataclass

from .errors import CAD_MISMATCH, VERSION_DOWNGRADE_RISK, VERSION_UPGRADE_BLOCKED, CadError

# SolidWorks 大版本 ↔ RevisionNumber 主号 ↔ 带版本 ProgID
SW_REVISION = {"2020": 28, "2021": 29, "2022": 30, "2023": 31, "2024": 32, "2025": 33, "2026": 34}
SW_PROGID = {v: f"SldWorks.Application.{n}" for v, n in SW_REVISION.items()}
CREO_ORDER = ["4.0", "5.0", "6.0", "7.0", "8.0", "9.0", "10.0", "11.0", "12.0"]


def _rank(cad: str, version: str) -> float:
    if cad == "solidworks":
        return float(SW_REVISION.get(version, 0))
    if cad == "creo":
        return float(CREO_ORDER.index(version)) if version in CREO_ORDER else float(version.split(".")[0])
    return float(version.split(".")[0]) if version[:1].isdigit() else 0.0


def sw_version_from_revision(revision: str) -> str | None:
    """'32.3.0' → '2024'"""
    major = int(revision.split(".")[0])
    return next((v for v, n in SW_REVISION.items() if n == major), None)


@dataclass
class OpenDecision:
    readonly: bool
    upgraded_from: str | None = None
    note: str | None = None


def decide_open(file_cad: str, file_ver: str, target_cad: str, target_ver: str,
                want_write: bool, allow_upgrade_save: bool) -> OpenDecision:
    if file_cad != target_cad:
        raise CadError(CAD_MISMATCH, f"file is {file_cad}, target is {target_cad}",
                       hint="用 cad_list_targets 选择对应 CAD 的目标")
    f, t = _rank(file_cad, file_ver), _rank(target_cad, target_ver)
    if f > t:
        raise CadError(VERSION_DOWNGRADE_RISK, f"file saved by {file_cad} {file_ver}, target is {target_ver} (older)",
                       hint=f"选择 {file_cad} {file_ver} 或更新版本的目标")
    if f < t:
        if want_write and not allow_upgrade_save:
            raise CadError(VERSION_UPGRADE_BLOCKED,
                           f"editing a {file_ver} file in {target_ver} would upgrade it irreversibly",
                           hint=f"选择 {file_cad} {file_ver} 目标；或由管理员在 policy 中开启 allow_upgrade_save")
        return OpenDecision(readonly=not want_write, upgraded_from=file_ver if want_write else None,
                            note=f"文件版本 {file_ver} 低于目标 {target_ver}" + ("，保存后将升级" if want_write else "，只读打开"))
    return OpenDecision(readonly=not want_write)
