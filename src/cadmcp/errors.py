"""Unified error model. Every error carries a `hint` written for the AI client."""
from __future__ import annotations

from typing import Any


class CadError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None, native_code: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hint = hint
        self.native_code = native_code

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ok": False, "error": {"code": self.code, "message": self.message}}
        if self.hint:
            d["error"]["hint"] = self.hint
        if self.native_code is not None:
            d["error"]["native_code"] = self.native_code
        return d


# Error codes (see docs/DESIGN.md §5.5)
TARGET_UNAVAILABLE = "TARGET_UNAVAILABLE"
TARGET_BUSY = "TARGET_BUSY"
CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
NO_CHANGE_SESSION = "NO_CHANGE_SESSION"
POLICY_DENIED = "POLICY_DENIED"
VERSION_DOWNGRADE_RISK = "VERSION_DOWNGRADE_RISK"
VERSION_UPGRADE_BLOCKED = "VERSION_UPGRADE_BLOCKED"
CAD_MISMATCH = "CAD_MISMATCH"
DOC_NOT_FOUND = "DOC_NOT_FOUND"
PARAM_NOT_FOUND = "PARAM_NOT_FOUND"
REF_NOT_FOUND = "REF_NOT_FOUND"
SPEC_INVALID = "SPEC_INVALID"
REGEN_FAILED = "REGEN_FAILED"
CHECKS_NOT_PASSED = "CHECKS_NOT_PASSED"
CAD_TIMEOUT = "CAD_TIMEOUT"
CAD_CRASHED = "CAD_CRASHED"
NATIVE_ERROR = "NATIVE_ERROR"
