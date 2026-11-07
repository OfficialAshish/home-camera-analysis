"""Request helpers shared by every endpoint module of the app (one definition each)."""
from __future__ import annotations

import os


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def abs_dir(p: str, field: str, must_exist: bool = True) -> str:
    if not isinstance(p, str) or not p.strip():
        raise ApiError(400, f"{field} is required")
    p = os.path.abspath(os.path.expanduser(p.strip()))
    if must_exist and not os.path.isdir(p):
        raise ApiError(400, f"{field}: folder not found: {p}")
    return p


def inside(base: str, rel_path: str) -> str:
    """Resolve rel_path under base; refuse anything that escapes it (.., absolute, symlinks)."""
    if not rel_path or "\x00" in rel_path:
        raise ApiError(400, "path required")
    base_r = os.path.realpath(base)
    full = os.path.realpath(os.path.join(base_r, rel_path))
    if os.path.commonpath([base_r, full]) != base_r:
        raise ApiError(403, "path outside the output folder")
    if not os.path.isfile(full):
        raise ApiError(404, "file not found")
    return full
