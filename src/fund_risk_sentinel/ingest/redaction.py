"""脱敏钩子。

数据约束要求"最小必要"：材料进入模型之前，把与风险识别无关的个人敏感信息
替换掉。这里刻意采用**保形替换**（保留长度与首尾特征），因为审核人仍需从
脱敏后的文本判断"这是不是一个正常格式的账号"，同时又不泄露真实号码。

脱敏是可配置的（``settings.security.*``），并会在材料上留下
``redaction_applied`` / ``redaction_log``，供审计追溯"这份材料是否被改动过"。
"""

from __future__ import annotations

import re

from ..config.settings import SecuritySettings
from ..core.models import Material

#: 身份证号（18 位，末位可为 X；兼容 15 位旧号）
_ID_CARD = re.compile(r"(?<![0-9])(\d{6})(\d{8}|\d{6})(\d{3}[\dXx])(?![0-9])")
#: 手机号
_PHONE = re.compile(r"(?<![0-9])(1[3-9]\d)(\d{4})(\d{4})(?![0-9])")
#: 银行卡 / 银行账号（16~19 位连续数字，排除已处理的身份证）
_BANK = re.compile(r"(?<![0-9])(\d{6})(\d{6,11})(\d{4})(?![0-9])")
#: 电子邮箱
_EMAIL = re.compile(r"([A-Za-z0-9._%+\-]{1,64})@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
#: 固定电话
_LANDLINE = re.compile(r"(?<![0-9])(0\d{2,3})[- ]?(\d{7,8})(?![0-9])")


def _mask_keep_edges(prefix: str, middle: str, suffix: str) -> str:
    return prefix + "*" * len(middle) + suffix


class Redactor:
    """按配置对文本执行脱敏，并返回动作日志。"""

    def __init__(self, settings: SecuritySettings | None = None) -> None:
        self.settings = settings or SecuritySettings()

    def redact(self, text: str) -> tuple[str, list[str]]:
        """返回 ``(脱敏后文本, 动作日志)``。"""
        if not text:
            return text, []
        log: list[str] = []
        result = text
        cfg = self.settings

        if cfg.redact_id_card:
            result, count = _sub_count(_ID_CARD, result, _id_repl)
            if count:
                log.append(f"身份证号 {count} 处")

        if cfg.redact_bank_account:
            result, count = _sub_count(_BANK, result, _bank_repl)
            if count:
                log.append(f"银行账号 {count} 处")

        if cfg.redact_phone:
            result, count = _sub_count(_PHONE, result, lambda m: _mask_keep_edges(m.group(1), m.group(2), m.group(3)))
            if count:
                log.append(f"手机号 {count} 处")

        if cfg.redact_email:
            result, count = _sub_count(_EMAIL, result, _email_repl)
            if count:
                log.append(f"邮箱 {count} 处")

        result, count = _sub_count(_LANDLINE, result, lambda m: m.group(1) + "-" + "*" * len(m.group(2)))
        if count:
            log.append(f"固定电话 {count} 处")

        return result, log

    def redact_material(self, material: Material) -> Material:
        """就地脱敏一份材料，并记录动作。"""
        redacted, log = self.redact(material.text)
        if log:
            material.text = redacted
            material.redaction_applied = True
            material.redaction_log = log
        # 表格里的敏感信息同样处理
        if material.tables:
            new_tables: list[list[list[str]]] = []
            table_log: list[str] = []
            for table in material.tables:
                new_table: list[list[str]] = []
                for row in table:
                    new_row: list[str] = []
                    for cell in row:
                        cell_text, cell_log = self.redact(cell)
                        if cell_log:
                            table_log.extend(cell_log)
                        new_row.append(cell_text)
                    new_table.append(new_row)
                new_tables.append(new_table)
            if table_log:
                material.tables = new_tables
                material.redaction_applied = True
                material.redaction_log = list(dict.fromkeys(material.redaction_log + table_log))
        return material

    def describe(self) -> list[str]:
        """返回当前启用的脱敏项，供设置页展示。"""
        cfg = self.settings
        items: list[str] = []
        if cfg.redact_id_card:
            items.append("身份证号")
        if cfg.redact_phone:
            items.append("手机号")
        if cfg.redact_bank_account:
            items.append("银行账号")
        if cfg.redact_email:
            items.append("邮箱")
        items.append("固定电话")
        if cfg.redact_person_name:
            items.append("人名")
        return items


def _sub_count(pattern: re.Pattern[str], text: str, repl) -> tuple[str, int]:
    count = 0

    def _wrapper(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return repl(match)

    return pattern.sub(_wrapper, text), count


def _id_repl(match: re.Match[str]) -> str:
    return match.group(1) + "*" * len(match.group(2)) + match.group(3)


def _bank_repl(match: re.Match[str]) -> str:
    return match.group(1) + "*" * len(match.group(2)) + match.group(3)


def _email_repl(match: re.Match[str]) -> str:
    local, domain = match.group(1), match.group(2)
    head = local[:1]
    return f"{head}{'*' * max(1, len(local) - 1)}@{domain}"


__all__ = ["Redactor"]
