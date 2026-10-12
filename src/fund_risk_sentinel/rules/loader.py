"""规则库加载与保存。

规则库是纯文本 YAML，这是刻意的设计：财务处可以自己改规则、走 Git 评审、
留版本痕迹，不需要开发介入，也不需要重新打包 exe。

目录约定（``config/rules/``）::

    config/rules/
    ├── builtin/              # 内置通用规则（随项目发布）
    │   ├── 01-额度.yaml
    │   ├── 02-用途.yaml
    │   └── ...
    └── institution/          # 本单位细则（用户自行维护，不进版本库）
        └── 校内报销细则.yaml

加载时按目录顺序合并，同 ID 规则后者覆盖前者，便于"校内口径覆盖通用口径"。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import yaml

from ..core.exceptions import RuleError
from ..core.models import Rule
from .engine import rule_from_dict, validate_condition

logger = logging.getLogger(__name__)

RULE_FILE_SUFFIXES = (".yaml", ".yml", ".json")
RULE_DIRS = ("builtin", "institution")


def load_rules_from_file(path: Path | str) -> list[Rule]:
    """从单个 YAML/JSON 文件加载规则。"""
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            if p.suffix.lower() == ".json":
                import json

                data = json.load(fh)
            else:
                data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise RuleError(f"规则文件 {p} 读取失败：{exc}") from exc

    if data is None:
        return []
    if isinstance(data, list):
        raw_rules = data
        file_meta: dict[str, Any] = {}
    elif isinstance(data, dict):
        raw_rules = data.get("rules") or []
        file_meta = data
    else:
        raise RuleError(f"规则文件 {p} 顶层结构应为映射或列表，实际为 {type(data).__name__}")

    default_source = str(file_meta.get("source") or "")
    default_version = str(file_meta.get("version") or "1.0")
    defaults = {
        k: file_meta[k]
        for k in ("dimension", "risk_category", "level_hint", "disposition_hint", "event_types", "basis")
        if k in file_meta
    }

    rules: list[Rule] = []
    for index, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            raise RuleError(f"规则文件 {p} 第 {index + 1} 条规则应为映射，实际为 {type(item).__name__}")
        merged = {**defaults, **item}
        if default_source and not merged.get("source"):
            merged["source"] = default_source
        merged.setdefault("version", default_version)
        # 优先使用规则作者显式声明的 id（如 ATH-002）；
        # 只有在既没有 id 也没有 rule_id 时，才用「文件名-序号」兜底，
        # 否则会把作者写的可读 ID 悄悄顶掉，导致规则引用与排查都对不上号。
        if not merged.get("rule_id") and not merged.get("id"):
            merged["rule_id"] = f"{p.stem}-{index + 1:03d}"
        try:
            rules.append(rule_from_dict(merged))
        except Exception as exc:  # noqa: BLE001 - pydantic 校验错误
            raise RuleError(f"规则文件 {p} 第 {index + 1} 条规则定义有误：{exc}") from exc
    return rules


def load_rules_from_dir(
    directory: Path | str,
    *,
    recursive: bool = True,
    strict: bool = False,
) -> list[Rule]:
    """加载目录下所有规则文件。

    ``strict=True`` 时任何一条规则的条件表达式非法都会抛错；默认只记录警告，
    避免一条写错的规则导致整个规则库加载失败。
    """
    root = Path(directory)
    if not root.is_dir():
        return []

    files: list[Path] = []
    for suffix in RULE_FILE_SUFFIXES:
        iterator = root.rglob(f"*{suffix}") if recursive else root.glob(f"*{suffix}")
        files.extend(iterator)
    files = sorted({f for f in files if not f.name.startswith(".") and not f.name.startswith("_")})

    rules: list[Rule] = []
    seen: dict[str, Rule] = {}
    for path in files:
        try:
            loaded = load_rules_from_file(path)
        except RuleError as exc:
            if strict:
                raise
            logger.warning("跳过规则文件 %s：%s", path, exc)
            continue
        for rule in loaded:
            problems = validate_condition(rule.condition)
            if problems:
                message = f"规则 [{rule.rule_id} {rule.name}] 条件非法：{problems[0]}"
                if strict:
                    raise RuleError(message)
                logger.warning("%s（已跳过该规则）", message)
                continue
            if rule.rule_id in seen:
                logger.info("规则 %s 被 %s 覆盖（后加载者优先）", rule.rule_id, path.name)
            seen[rule.rule_id] = rule
        rules.extend(loaded)

    # 去重：同 ID 保留最后加载的（便于校内细则覆盖内置规则）
    unique: dict[str, Rule] = {}
    for rule in rules:
        unique[rule.rule_id] = rule
    return list(unique.values())


def load_rules_from_dirs(directories: Iterable[Path | str], **kwargs: Any) -> list[Rule]:
    merged: dict[str, Rule] = {}
    for directory in directories:
        for rule in load_rules_from_dir(directory, **kwargs):
            merged[rule.rule_id] = rule
    return list(merged.values())


def save_rules_to_file(rules: Iterable[Rule], path: Path | str) -> Path:
    """把规则写回 YAML（界面"规则库"页的保存动作）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "source": "user",
        "rules": [
            rule.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
            for rule in rules
        ],
    }
    with p.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False, width=1000)
    return p


def resolve_rule_dirs(base: Path | str) -> list[Path]:
    """返回实际存在的规则目录列表（builtin 在前，institution 在后）。"""
    root = Path(base)
    dirs: list[Path] = []
    for name in RULE_DIRS:
        candidate = root / name
        if candidate.is_dir():
            dirs.append(candidate)
    if not dirs and root.is_dir():
        dirs.append(root)
    return dirs


def load_all_rules(base: Path | str, *, strict: bool = False) -> list[Rule]:
    """按约定目录结构加载全部规则。"""
    return load_rules_from_dirs(resolve_rule_dirs(base), strict=strict)


__all__ = [
    "RULE_FILE_SUFFIXES",
    "RULE_DIRS",
    "load_rules_from_file",
    "load_rules_from_dir",
    "load_rules_from_dirs",
    "save_rules_to_file",
    "resolve_rule_dirs",
    "load_all_rules",
]
