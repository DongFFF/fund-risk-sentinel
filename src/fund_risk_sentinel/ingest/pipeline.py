"""上传与解析流水线——系统的统一材料入口。

对外提供四种上传方式，覆盖桌面端与后续 API 化的全部需要：

======================  ==========================================  ==============================
方法                    用途                                        典型场景
======================  ==========================================  ==============================
``load_paths``          一批具体文件                                 界面多选 / 拖拽
``load_folder``         整个目录（可递归、可按扩展名过滤）           批量导入一个项目的材料
``load_bytes``          内存字节流                                   浏览器上传 / 接口调用
``load_text``           直接粘贴文本                                 临时核对一段制度或说明
======================  ==========================================  ==============================

统一行为：格式判定 → 角色推断 → 解析 → 脱敏 → 指纹去重 → 数量与体积限额。
任何单个文件失败都不会中断整批，失败原因逐条记录在 ``IngestResult.items`` 中。
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ..config.settings import ReviewSettings, SecuritySettings
from ..core.enums import DOCUMENT_ROLE_LABELS, DocumentFormat, DocumentRole
from ..core.exceptions import IngestError
from ..core.models import Material
from .base import LoaderContext, LoaderRegistry, detect_format, file_sha256
from .loaders import register_all
from .redaction import Redactor
from .roles import infer_role

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]

#: 角色在拼装上下文时的排序：制度文本放前面，业务材料随后
_ROLE_ORDER = {
    DocumentRole.POLICY: 0,
    DocumentRole.BUDGET: 1,
    DocumentRole.APPLICATION: 2,
    DocumentRole.CONTRACT: 3,
    DocumentRole.INVOICE: 4,
    DocumentRole.APPROVAL: 5,
    DocumentRole.ACCEPTANCE: 6,
    DocumentRole.OTHER: 7,
}


@dataclass
class IngestItem:
    """单个文件的上传结果。"""

    filename: str
    path: str
    ok: bool = False
    role: DocumentRole = DocumentRole.OTHER
    material_id: str = ""
    error: str = ""
    skipped_reason: str = ""

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "filename": self.filename,
            "path": self.path,
            "ok": self.ok,
            "role": self.role.value,
            "role_label": DOCUMENT_ROLE_LABELS.get(self.role, self.role.value),
            "material_id": self.material_id,
            "error": self.error,
            "skipped_reason": self.skipped_reason,
        }


@dataclass
class IngestResult:
    """一批上传的整体结果。"""

    materials: list[Material] = field(default_factory=list)
    items: list[IngestItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> list[IngestItem]:
        return [i for i in self.items if i.ok]

    @property
    def failed(self) -> list[IngestItem]:
        return [i for i in self.items if not i.ok]

    @property
    def duplicates(self) -> list[IngestItem]:
        return [i for i in self.items if i.skipped_reason]

    @property
    def total_chars(self) -> int:
        return sum(m.char_count for m in self.materials)

    def summary(self) -> str:
        parts = [f"成功 {len(self.succeeded)} 份"]
        if self.duplicates:
            parts.append(f"跳过重复 {len(self.duplicates)} 份")
        if self.failed:
            parts.append(f"失败 {len(self.failed)} 份")
        parts.append(f"合计 {self.total_chars:,} 字符")
        return "，".join(parts)

    def by_role(self) -> dict[DocumentRole, list[Material]]:
        out: dict[DocumentRole, list[Material]] = {}
        for m in self.materials:
            out.setdefault(m.role, []).append(m)
        return out

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary(),
            "warnings": self.warnings,
            "items": [i.to_dict() for i in self.items],
            "materials": [
                {
                    "material_id": m.material_id,
                    "filename": m.filename,
                    "role": m.role.value,
                    "format": m.fmt.value,
                    "chars": m.char_count,
                    "pages": m.page_count,
                    "size_bytes": m.size_bytes,
                    "redacted": m.redaction_applied,
                }
                for m in self.materials
            ],
        }


class IngestPipeline:
    """材料上传与解析的统一入口。"""

    def __init__(
        self,
        *,
        security: SecuritySettings | None = None,
        review: ReviewSettings | None = None,
        context: LoaderContext | None = None,
        work_dir: Path | str | None = None,
    ) -> None:
        register_all()
        self.security = security or SecuritySettings()
        self.review = review or ReviewSettings()
        self.context = context or LoaderContext()
        self.redactor = Redactor(self.security)
        self.work_dir = Path(work_dir) if work_dir else None
        if self.work_dir:
            self.work_dir.mkdir(parents=True, exist_ok=True)

    # ============================================================ 上传接口

    def load_paths(
        self,
        paths: Iterable[Path | str],
        *,
        role_overrides: dict[str, DocumentRole] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> IngestResult:
        """上传一批具体文件。``role_overrides`` 以文件名为键覆盖自动角色判定。"""
        file_list = [Path(p) for p in paths]
        result = IngestResult()
        seen_hashes: dict[str, str] = {}

        if len(file_list) > self.review.max_materials_per_task:
            result.warnings.append(
                f"本次选择 {len(file_list)} 份材料，超过单任务上限 "
                f"{self.review.max_materials_per_task} 份，仅处理前 "
                f"{self.review.max_materials_per_task} 份。"
            )
            file_list = file_list[: self.review.max_materials_per_task]

        total = len(file_list)
        for index, path in enumerate(file_list, start=1):
            if on_progress:
                on_progress(index, total, path.name)
            item, material = self._ingest_one(path, role_overrides, seen_hashes)
            result.items.append(item)
            if material is not None:
                result.materials.append(material)

        self._post_check(result)
        return result

    def load_folder(
        self,
        folder: Path | str,
        *,
        recursive: bool = True,
        extensions: Iterable[str] | None = None,
        role_overrides: dict[str, DocumentRole] | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> IngestResult:
        """上传整个目录。默认递归，可按扩展名白名单过滤。"""
        root = Path(folder)
        if not root.is_dir():
            raise IngestError(f"目录不存在：{root}")
        allowed = {e.lower() for e in extensions} if extensions else set(LoaderRegistry.supported_extensions())
        iterator = root.rglob("*") if recursive else root.glob("*")
        files = sorted(
            p for p in iterator
            if p.is_file()
            and p.suffix.lower() in allowed
            and not p.name.startswith("~$")          # Office 临时文件
            and not p.name.startswith(".")           # 隐藏文件
        )
        return self.load_paths(files, role_overrides=role_overrides, on_progress=on_progress)

    def load_bytes(
        self,
        filename: str,
        data: bytes,
        *,
        role: DocumentRole | None = None,
        on_progress: ProgressCallback | None = None,
    ) -> IngestResult:
        """上传内存中的字节流（接口调用 / 浏览器上传场景）。"""
        work_dir = self.work_dir or Path(tempfile.mkdtemp(prefix="fund-risk-upload-"))
        work_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(filename).name or "upload.bin"
        target = _unique_path(work_dir / safe_name)
        target.write_bytes(data)
        overrides = {safe_name: role} if role else None
        result = self.load_paths([target], role_overrides=overrides, on_progress=on_progress)
        for item in result.items:
            item.filename = safe_name
        return result

    def load_text(
        self,
        text: str,
        *,
        name: str = "手工录入材料.txt",
        role: DocumentRole | None = None,
    ) -> IngestResult:
        """直接录入文本（临时核对一段制度或说明）。"""
        work_dir = self.work_dir or Path(tempfile.mkdtemp(prefix="fund-risk-manual-"))
        work_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(name).name
        if not safe_name.lower().endswith(".txt"):
            safe_name += ".txt"
        target = _unique_path(work_dir / safe_name)
        target.write_text(text, encoding="utf-8")
        overrides = {safe_name: role} if role else None
        result = self.load_paths([target], role_overrides=overrides)
        for item in result.items:
            item.filename = Path(name).name
            if item.ok and result.materials:
                result.materials[-1].filename = Path(name).name
        return result

    # ============================================================ 内部

    def _ingest_one(
        self,
        path: Path,
        role_overrides: dict[str, DocumentRole] | None,
        seen_hashes: dict[str, str],
    ) -> tuple[IngestItem, Material | None]:
        item = IngestItem(filename=path.name, path=str(path))

        if not path.is_file():
            item.error = "文件不存在或不可读"
            return item, None

        try:
            digest = file_sha256(path)
        except OSError as exc:
            item.error = f"无法读取文件：{exc}"
            return item, None

        if digest in seen_hashes:
            item.ok = False
            item.skipped_reason = f"内容与「{seen_hashes[digest]}」重复，已跳过"
            return item, None

        try:
            loader = LoaderRegistry.resolve(path)
        except IngestError as exc:
            item.error = str(exc)
            return item, None

        forced_role = (role_overrides or {}).get(path.name)
        try:
            material = loader.load(path, role=forced_role or DocumentRole.OTHER)
        except IngestError as exc:
            item.error = str(exc)
            return item, None
        except Exception as exc:  # noqa: BLE001 - 未知解析异常也不应中断整批
            logger.exception("解析 %s 时发生未预期异常", path)
            item.error = f"解析失败：{type(exc).__name__}: {exc}"
            return item, None

        if forced_role is None:
            material.role = infer_role(path.name, material.text)
        material.metadata["detected_format"] = material.fmt.value
        material.metadata["role_inferred"] = forced_role is None

        self.redactor.redact_material(material)

        if self.review.max_chars_per_material and material.char_count > self.review.max_chars_per_material:
            material.metadata["trimmed_for_model"] = True
            material.metadata["original_chars"] = material.char_count

        seen_hashes[digest] = path.name
        item.ok = True
        item.role = material.role
        item.material_id = material.material_id
        return item, material

    def _post_check(self, result: IngestResult) -> None:
        """整批级别的提示：扫描件、角色缺失、体量过大等。"""
        if not result.materials:
            return
        needs_ocr = [m.filename for m in result.materials if m.metadata.get("needs_ocr")]
        if needs_ocr:
            result.warnings.append(
                "以下材料未识别出文字，其中的内容尚未进入分析范围："
                + "、".join(needs_ocr[:5])
                + ("…" if len(needs_ocr) > 5 else "")
                + "。建议开启 OCR 或改用支持视觉输入的模型。"
            )

        truncated = [m.filename for m in result.materials if m.metadata.get("truncated")]
        if truncated:
            result.warnings.append(
                "以下材料超出长度上限已被截断，可能影响完整性判断："
                + "、".join(truncated[:5])
                + ("…" if len(truncated) > 5 else "")
            )

        if not any(m.role == DocumentRole.INVOICE for m in result.materials):
            result.warnings.append("本批材料中未识别到发票/票据类材料，请确认是否遗漏关键凭证。")
        if not any(m.role == DocumentRole.POLICY for m in result.materials):
            result.warnings.append(
                "本批材料中未包含制度文本，制度语义匹配将仅依据内置规则库，"
                "建议补充相关管理办法或校内实施细则。"
            )

        total = sum(m.char_count for m in result.materials)
        if total > self.review.total_prompt_char_budget:
            result.warnings.append(
                f"本批材料合计 {total:,} 字符，超过单次提示预算 "
                f"{self.review.total_prompt_char_budget:,} 字符，"
                "送模型时将按材料优先级裁剪，建议拆分为多个审查任务。"
            )

    # ============================================================ 上下文拼装

    def assemble_context(
        self,
        materials: Iterable[Material],
        *,
        total_budget: int | None = None,
        per_material_limit: int | None = None,
        include_manifest: bool = True,
    ) -> tuple[str, list[str]]:
        """把材料拼装成送模型的上下文，返回 ``(文本, 警告列表)``。

        策略：制度文本优先完整保留（它决定"规则是什么"），业务材料按角色顺序
        填充剩余预算；超出部分截断并明确标注，绝不让模型在不知情的情况下
        基于残缺材料下判断。
        """
        material_list = sorted(materials, key=lambda m: _ROLE_ORDER.get(m.role, 99))
        budget = total_budget or self.review.total_prompt_char_budget
        per_limit = per_material_limit or self.review.max_chars_per_material
        warnings: list[str] = []
        blocks: list[str] = []
        used = 0

        if include_manifest:
            manifest_lines = ["【材料清单】"]
            for idx, m in enumerate(material_list, start=1):
                manifest_lines.append(
                    f"{idx}. {m.filename}｜角色：{DOCUMENT_ROLE_LABELS.get(m.role, m.role.value)}"
                    f"｜格式：{m.fmt.value}｜页数：{m.page_count or '-'}"
                    f"｜字符：{m.char_count:,}"
                    + ("｜已脱敏" if m.redaction_applied else "")
                )
            manifest = "\n".join(manifest_lines)
            blocks.append(manifest)
            used += len(manifest)

        for idx, material in enumerate(material_list, start=1):
            header = (
                f"\n【材料 {idx}】{material.filename}"
                f"（角色：{DOCUMENT_ROLE_LABELS.get(material.role, material.role.value)}）\n"
            )
            body = material.text
            if len(body) > per_limit:
                body = body[:per_limit] + f"\n……（本材料共 {material.char_count:,} 字符，已按上限截断）"
                warnings.append(f"{material.filename} 超出单材料上限，已截断至 {per_limit:,} 字符。")

            remaining = budget - used - len(header)
            if remaining <= 0:
                warnings.append(f"提示预算已用尽，{material.filename} 及后续材料未送入模型。")
                break
            if len(body) > remaining:
                body = body[:remaining] + "\n……（受总预算限制截断）"
                warnings.append(f"{material.filename} 因总预算限制被截断。")

            block = header + body
            blocks.append(block)
            used += len(block)

        return "\n".join(blocks), warnings


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for i in range(1, 1000):
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
    raise IngestError(f"无法为 {path.name} 生成唯一文件名")


__all__ = [
    "IngestItem",
    "IngestResult",
    "IngestPipeline",
    "ProgressCallback",
    "LoaderContext",
    "detect_format",
    "DocumentFormat",
]
