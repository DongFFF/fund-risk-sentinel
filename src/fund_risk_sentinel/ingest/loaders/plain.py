"""纯文本类材料解析：txt / md / csv / json。

这几种格式不依赖第三方库，是"任何环境下都能用"的底线能力——即使目标机器
没有安装 PDF / Office 解析依赖，报销说明、制度条款、台账导出这些材料依然可读。
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from ...core.enums import DocumentFormat, DocumentRole
from ...core.exceptions import CorruptedFileError
from ...core.models import Material
from ..base import BaseLoader

#: 常见中文编码，按命中概率排序
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "utf-16", "latin-1")


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """按候选编码依次尝试解码，返回 ``(文本, 命中编码)``。"""
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    # 最后兜底：忽略错误字符，保证流程不中断
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


class TextLoader(BaseLoader):
    """``.txt`` / ``.log`` —— 纯文本。"""

    formats = (DocumentFormat.TXT,)
    extensions = (".txt", ".log")

    def load(self, path: Path | str, *, role: DocumentRole = DocumentRole.OTHER) -> Material:
        p = Path(path)
        try:
            raw = p.read_bytes()
        except OSError as exc:
            raise CorruptedFileError(f"{p.name} 无法读取：{exc}") from exc
        text, encoding = decode_bytes(raw)
        material = self._base_material(p, role, DocumentFormat.TXT)
        material.text = text
        material.metadata["encoding"] = encoding
        material.page_count = 1
        return self._finalize(material)


class MarkdownLoader(BaseLoader):
    """``.md`` —— 保留原文，同时抽出标题结构，便于制度文本定位到条。"""

    formats = (DocumentFormat.MARKDOWN,)
    extensions = (".md", ".markdown")

    def load(self, path: Path | str, *, role: DocumentRole = DocumentRole.OTHER) -> Material:
        p = Path(path)
        try:
            raw = p.read_bytes()
        except OSError as exc:
            raise CorruptedFileError(f"{p.name} 无法读取：{exc}") from exc
        text, encoding = decode_bytes(raw)
        material = self._base_material(p, role, DocumentFormat.MARKDOWN)
        material.text = text
        material.metadata["encoding"] = encoding
        headings: list[dict[str, Any]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                headings.append(
                    {"level": level, "title": stripped.lstrip("#").strip(), "line": lineno}
                )
        material.metadata["headings"] = headings
        material.page_count = max(1, len(text.split("\n\n")) // 20 or 1)
        return self._finalize(material)


class CsvLoader(BaseLoader):
    """``.csv`` / ``.tsv`` —— 表格材料，如经费台账、报销明细。"""

    formats = (DocumentFormat.CSV,)
    extensions = (".csv", ".tsv")

    def load(self, path: Path | str, *, role: DocumentRole = DocumentRole.OTHER) -> Material:
        p = Path(path)
        try:
            raw = p.read_bytes()
        except OSError as exc:
            raise CorruptedFileError(f"{p.name} 无法读取：{exc}") from exc
        text, encoding = decode_bytes(raw)
        delimiter = "\t" if p.suffix.lower() == ".tsv" else None

        rows: list[list[str]] = []
        try:
            if delimiter is None:
                try:
                    dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
                    delimiter = dialect.delimiter
                except csv.Error:
                    delimiter = ","
            reader = csv.reader(io.StringIO(text), delimiter=delimiter)
            for index, row in enumerate(reader):
                if index >= self.context.max_rows:
                    rows.append(["……", f"（超过 {self.context.max_rows} 行，已截断）"])
                    break
                rows.append([str(cell).strip() for cell in row])
        except csv.Error as exc:
            raise CorruptedFileError(f"{p.name} CSV 解析失败：{exc}") from exc

        material = self._base_material(p, role, DocumentFormat.CSV)
        material.tables = [rows] if rows else []
        material.metadata["encoding"] = encoding
        material.metadata["delimiter"] = delimiter
        material.metadata["row_count"] = len(rows)
        material.metadata["columns"] = rows[0] if rows else []
        material.text = _render_table(rows, p.name)
        material.page_count = 1
        return self._finalize(material)


class JsonLoader(BaseLoader):
    """``.json`` —— 结构化材料，如系统导出的报销单、接口返回的台账。"""

    formats = (DocumentFormat.JSON,)
    extensions = (".json",)

    def load(self, path: Path | str, *, role: DocumentRole = DocumentRole.OTHER) -> Material:
        p = Path(path)
        try:
            raw = p.read_bytes()
        except OSError as exc:
            raise CorruptedFileError(f"{p.name} 无法读取：{exc}") from exc
        text, encoding = decode_bytes(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CorruptedFileError(f"{p.name} 不是合法 JSON：{exc}") from exc

        material = self._base_material(p, role, DocumentFormat.JSON)
        material.metadata["encoding"] = encoding
        material.metadata["top_level_type"] = type(data).__name__

        if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
            headers = list({k for item in data[:50] for k in item})
            rows = [headers] + [[_stringify(item.get(h, "")) for h in headers] for item in data]
            material.tables = [rows[: self.context.max_rows + 1]]
            material.metadata["record_count"] = len(data)
            material.text = _render_table(material.tables[0], p.name)
        else:
            material.text = json.dumps(data, ensure_ascii=False, indent=2)
            if isinstance(data, dict):
                material.metadata["keys"] = list(data.keys())[:80]
        material.page_count = 1
        return self._finalize(material)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _render_table(rows: list[list[str]], title: str) -> str:
    """把二维表渲染成"键：值"逐行文本，便于模型阅读与定位。"""
    if not rows:
        return ""
    lines: list[str] = [f"【表格：{title}】"]
    header = rows[0]
    body = rows[1:]
    if body:
        for idx, row in enumerate(body, start=1):
            pairs = [
                f"{header[i] if i < len(header) else f'列{i + 1}'}：{cell}"
                for i, cell in enumerate(row)
            ]
            lines.append(f"第{idx}行 | " + "；".join(pairs))
    else:
        lines.append(" | ".join(header))
    lines.append(f"（共 {len(body)} 条记录）")
    return "\n".join(lines)


__all__ = [
    "decode_bytes",
    "TextLoader",
    "MarkdownLoader",
    "CsvLoader",
    "JsonLoader",
]
