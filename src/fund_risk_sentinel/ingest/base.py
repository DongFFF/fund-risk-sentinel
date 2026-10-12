"""材料解析基类与格式注册表。

新增一种材料格式只需三步：
1. 继承 ``BaseLoader``，声明 ``formats`` 与 ``extensions``，实现 ``load``；
2. 在 ``ingest/loaders/`` 下建模块；
3. 在 ``ingest/loaders/__init__.py`` 里 ``register`` 一行。

``IngestPipeline`` 会自动把新格式纳入上传流程，无需改动任何调用方。
"""

from __future__ import annotations

import abc
import hashlib
from pathlib import Path

from ..core.enums import DocumentFormat, DocumentRole
from ..core.exceptions import CorruptedFileError, EmptyContentError, IngestError, UnsupportedFormatError
from ..core.models import Material

#: 扩展名 → 格式
EXTENSION_MAP: dict[str, DocumentFormat] = {
    ".pdf": DocumentFormat.PDF,
    ".docx": DocumentFormat.DOCX,
    ".doc": DocumentFormat.DOCX,
    ".xlsx": DocumentFormat.XLSX,
    ".xlsm": DocumentFormat.XLSX,
    ".xls": DocumentFormat.XLSX,
    ".csv": DocumentFormat.CSV,
    ".tsv": DocumentFormat.CSV,
    ".json": DocumentFormat.JSON,
    ".txt": DocumentFormat.TXT,
    ".log": DocumentFormat.TXT,
    ".md": DocumentFormat.MARKDOWN,
    ".markdown": DocumentFormat.MARKDOWN,
    ".png": DocumentFormat.IMAGE,
    ".jpg": DocumentFormat.IMAGE,
    ".jpeg": DocumentFormat.IMAGE,
    ".bmp": DocumentFormat.IMAGE,
    ".tif": DocumentFormat.IMAGE,
    ".tiff": DocumentFormat.IMAGE,
    ".webp": DocumentFormat.IMAGE,
}


def detect_format(path: Path | str) -> DocumentFormat:
    """按扩展名判定格式。"""
    return EXTENSION_MAP.get(Path(path).suffix.lower(), DocumentFormat.UNKNOWN)


def file_sha256(path: Path | str, chunk_size: int = 1 << 20) -> str:
    """流式计算文件指纹，用于去重与完整性校验。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


class LoaderContext:
    """解析上下文：把运行时参数与 loader 解耦。"""

    def __init__(
        self,
        *,
        max_chars: int = 200_000,
        max_rows: int = 5_000,
        max_pages: int = 200,
        ocr_enabled: bool = False,
        ocr_language: str = "chi_sim+eng",
        keep_tables: bool = True,
    ) -> None:
        self.max_chars = max_chars
        self.max_rows = max_rows
        self.max_pages = max_pages
        self.ocr_enabled = ocr_enabled
        self.ocr_language = ocr_language
        self.keep_tables = keep_tables


class BaseLoader(abc.ABC):
    """材料解析器基类。"""

    #: 支持的格式（用于注册表匹配）
    formats: tuple[DocumentFormat, ...] = ()
    #: 支持的扩展名（小写，含点）
    extensions: tuple[str, ...] = ()
    #: 依赖缺失时给出的安装提示
    requires: tuple[str, ...] = ()

    def __init__(self, context: LoaderContext | None = None) -> None:
        self.context = context or LoaderContext()

    # ---------------------------------------------------------- 匹配

    @classmethod
    def supports(cls, fmt: DocumentFormat, suffix: str = "") -> bool:
        if fmt in cls.formats:
            return True
        return bool(suffix) and suffix.lower() in cls.extensions

    # ---------------------------------------------------------- 契约

    @abc.abstractmethod
    def load(self, path: Path | str, *, role: DocumentRole = DocumentRole.OTHER) -> Material:
        """读取文件并返回统一的 ``Material``。"""
        raise NotImplementedError

    # ---------------------------------------------------------- 工具

    def _base_material(
        self,
        path: Path,
        role: DocumentRole,
        fmt: DocumentFormat,
    ) -> Material:
        """构造带通用元信息的 Material 骨架。"""
        stat = path.stat()
        return Material(
            filename=path.name,
            path=str(path.resolve()),
            fmt=fmt,
            role=role,
            size_bytes=stat.st_size,
            sha256=file_sha256(path),
            metadata={
                "suffix": path.suffix.lower(),
                "mtime": int(stat.st_mtime),
                "loader": type(self).__name__,
            },
        )

    def _finalize(self, material: Material) -> Material:
        """统一收尾：空内容校验 + 长度截断。"""
        if not material.text.strip() and not material.tables:
            raise EmptyContentError(
                f"{material.filename}：未提取到任何文本或表格内容。"
                "若为扫描件/图片，请在设置中开启 OCR 后重试。"
            )
        limit = self.context.max_chars
        if len(material.text) > limit:
            material.metadata["truncated"] = True
            material.metadata["original_chars"] = len(material.text)
            material.text = material.text[:limit] + f"\n\n……（原文共 {len(material.text)} 字符，已按上限截断）"
        material.page_count = material.page_count or 0
        return material

    def _missing_dep(self, package: str, purpose: str) -> IngestError:
        return IngestError(
            f"解析{purpose}需要依赖 {package}，当前环境未安装。"
            f"请执行：pip install {package}"
        )

    def _wrap_error(self, path: Path, exc: Exception) -> IngestError:
        return CorruptedFileError(f"{path.name} 解析失败：{type(exc).__name__}: {exc}")


# ================================================================ 注册表

class LoaderRegistry:
    """格式 → loader 实例 的注册表。"""

    _loaders: list[BaseLoader] = []

    @classmethod
    def register(cls, loader: type[BaseLoader] | BaseLoader) -> None:
        instance = loader() if isinstance(loader, type) else loader
        cls._loaders.append(instance)

    @classmethod
    def clear(cls) -> None:
        cls._loaders.clear()

    @classmethod
    def all(cls) -> list[BaseLoader]:
        return list(cls._loaders)

    @classmethod
    def resolve(cls, path: Path | str) -> BaseLoader:
        """按扩展名/格式找到合适的 loader。"""
        p = Path(path)
        fmt = detect_format(p)
        suffix = p.suffix.lower()
        for loader in cls._loaders:
            if loader.supports(fmt, suffix):
                return loader
        raise UnsupportedFormatError(
            f"不支持的文件格式：{p.name}（{suffix or '无扩展名'}）。"
            f"当前支持：{', '.join(sorted({e for l in cls._loaders for e in l.extensions}))}"
        )

    @classmethod
    def supported_extensions(cls) -> list[str]:
        return sorted({e for loader in cls._loaders for e in loader.extensions})

    @classmethod
    def with_context(cls, context: LoaderContext) -> list[BaseLoader]:
        """按给定上下文重建 loader 实例（用于每次上传应用不同参数）。"""
        return [type(loader)(context) for loader in cls._loaders]


__all__ = [
    "EXTENSION_MAP",
    "detect_format",
    "file_sha256",
    "LoaderContext",
    "BaseLoader",
    "LoaderRegistry",
]
