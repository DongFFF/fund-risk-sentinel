"""路径解析——统一处理「源码运行」与「打包成 exe 后运行」两种形态。

打包后（``sys.frozen``）项目根目录是 exe 所在目录，而可写的配置与数据目录
必须放到用户目录（``%APPDATA%`` / ``~/.config``），否则单文件 exe 解压出的
临时目录是只读的。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "FundRiskSentinel"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包后的可执行文件中。"""
    return bool(getattr(sys, "frozen", False))


def _frozen_exe_dir() -> Path:
    return Path(sys.executable).resolve().parent


def project_root() -> Path:
    """项目根目录。

    - 源码运行：``<repo>/``（本文件位于 ``<repo>/src/fund_risk_sentinel/config/``）
    - 打包运行：exe 所在目录
    """
    if is_frozen():
        return _frozen_exe_dir()
    return Path(__file__).resolve().parents[3]


def bundled_dir() -> Path:
    """只读资源目录。

    PyInstaller 单文件模式下 ``sys._MEIPASS`` 指向解压出的临时目录，
    模板、内置规则等随包资源都在那里。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return project_root()


def user_config_dir() -> Path:
    """用户级可写配置目录。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_DIR_NAME / "config"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "fund-risk-sentinel"


def user_data_dir() -> Path:
    """用户级数据目录（SQLite、材料副本、日志、导出）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "fund-risk-sentinel"


def user_log_dir() -> Path:
    return user_data_dir() / "logs"


def ensure_dir(path: Path) -> Path:
    """确保目录存在并返回它。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_search_paths() -> list[Path]:
    """配置文件搜索顺序（先命中先用）：

    1. 环境变量 ``FUND_RISK_CONFIG_DIR`` 指定目录
    2. 用户配置目录（打包后的主位置）
    3. 项目 ``config/``（源码运行时的主位置）
    4. 打包资源目录内的 ``config/``（内置默认值）
    """
    paths: list[Path] = []
    env_dir = os.environ.get("FUND_RISK_CONFIG_DIR")
    if env_dir:
        paths.append(Path(env_dir))
    paths.append(user_config_dir())
    paths.append(project_root() / "config")
    paths.append(bundled_dir() / "config")
    seen: set[Path] = set()
    unique: list[Path] = []
    for p in paths:
        rp = p.resolve() if p.exists() else p
        if rp not in seen:
            seen.add(rp)
            unique.append(p)
    return unique


def resolve_config_file(name: str) -> Path | None:
    """按搜索顺序找到第一个存在的配置文件。"""
    for d in config_search_paths():
        candidate = d / name
        if candidate.is_file():
            return candidate
    return None


__all__ = [
    "APP_DIR_NAME",
    "is_frozen",
    "project_root",
    "bundled_dir",
    "user_config_dir",
    "user_data_dir",
    "user_log_dir",
    "ensure_dir",
    "config_search_paths",
    "resolve_config_file",
]
