"""材料解析器注册入口。

导入本模块即完成所有 loader 的注册。``ingest`` 包内部统一从这里取注册表。
"""

from __future__ import annotations

from ..base import LoaderRegistry
from .docx import DocxLoader
from .image import ImageLoader
from .pdf import PdfLoader, ocr_available
from .plain import CsvLoader, JsonLoader, MarkdownLoader, TextLoader
from .xlsx import XlsxLoader

_LOADER_TYPES = (
    TextLoader,
    MarkdownLoader,
    CsvLoader,
    JsonLoader,
    DocxLoader,
    XlsxLoader,
    PdfLoader,
    ImageLoader,
)


def register_all() -> None:
    """把内置 loader 注册进全局注册表（幂等）。"""
    if LoaderRegistry.all():
        return
    for loader_type in _LOADER_TYPES:
        LoaderRegistry.register(loader_type)


register_all()

__all__ = [
    "register_all",
    "TextLoader",
    "MarkdownLoader",
    "CsvLoader",
    "JsonLoader",
    "DocxLoader",
    "XlsxLoader",
    "PdfLoader",
    "ImageLoader",
    "ocr_available",
]
