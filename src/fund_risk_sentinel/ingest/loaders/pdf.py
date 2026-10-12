"""``.pdf`` 材料解析（双引擎：pypdf → PyMuPDF）。

策略：先用 pypdf 走文本层（快、依赖轻）；若文本过少，说明大概率是扫描件，
改用 PyMuPDF 再试一次（对版面还原更好）。两次都拿不到文本时：

- 若开启 OCR 且环境有 pytesseract + Tesseract，则渲染页面做 OCR；
- 否则抛出 ``EmptyContentError`` 并明确提示"这是扫描件，请开启 OCR 或上传电子版"，
  而不是静默返回空材料——静默失败会让审查结论失真。
"""

from __future__ import annotations

from pathlib import Path

from ...core.enums import DocumentFormat, DocumentRole
from ...core.exceptions import CorruptedFileError, EmptyContentError, IngestError
from ...core.models import Material
from ..base import BaseLoader

#: 每页平均字符数低于此值，视为疑似扫描件
_SPARSE_THRESHOLD = 40


class PdfLoader(BaseLoader):
    """PDF 解析器。"""

    formats = (DocumentFormat.PDF,)
    extensions = (".pdf",)
    requires = ("pypdf",)

    def load(self, path: Path | str, *, role: DocumentRole = DocumentRole.OTHER) -> Material:
        p = Path(path)
        material = self._base_material(p, role, DocumentFormat.PDF)
        engines_tried: list[str] = []

        text, pages, engine = self._try_pypdf(p, engines_tried)
        if len(text.strip()) < _SPARSE_THRESHOLD * max(1, pages):
            better_text, better_pages, better_engine = self._try_pymupdf(p, engines_tried)
            if len(better_text.strip()) > len(text.strip()):
                text, pages, engine = better_text, better_pages, better_engine

        if len(text.strip()) < _SPARSE_THRESHOLD * max(1, pages):
            ocr_text, ocr_pages = self._try_ocr(p)
            if ocr_text.strip():
                text, pages, engine = ocr_text, ocr_pages, "pytesseract(OCR)"
            else:
                raise EmptyContentError(
                    f"{p.name}：未提取到有效文本（共 {pages} 页），大概率是扫描件或纯图片 PDF。"
                    "请开启 OCR 后重试，或上传可复制的电子版/原始 Word、Excel 材料。"
                )

        material.text = text
        material.page_count = pages
        material.metadata["engine"] = engine
        material.metadata["engines_tried"] = engines_tried
        material.metadata["text_chars"] = len(text)
        return self._finalize(material)

    # ---------------------------------------------------------- 引擎一：pypdf

    def _try_pypdf(self, path: Path, tried: list[str]) -> tuple[str, int, str]:
        tried.append("pypdf")
        try:
            from pypdf import PdfReader
        except ImportError:
            return "", 0, "pypdf(未安装)"
        try:
            reader = PdfReader(str(path))
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")  # 尝试空密码（部分文件仅设了权限密码）
                except Exception as exc:  # noqa: BLE001
                    raise CorruptedFileError(
                        f"{path.name} 已加密，无法解析。请提供解密后的版本。"
                    ) from exc
            limit = min(len(reader.pages), self.context.max_pages)
            chunks: list[str] = []
            for index in range(limit):
                try:
                    chunks.append(reader.pages[index].extract_text() or "")
                except Exception as exc:  # noqa: BLE001 - 单页失败不应中断整篇
                    chunks.append(f"（第 {index + 1} 页解析失败：{exc}）")
            text = "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip())
            return text, len(reader.pages), "pypdf"
        except CorruptedFileError:
            raise
        except Exception as exc:  # noqa: BLE001
            return f"（pypdf 解析异常：{exc}）", 0, "pypdf(失败)"

    # ---------------------------------------------------------- 引擎二：PyMuPDF

    def _try_pymupdf(self, path: Path, tried: list[str]) -> tuple[str, int, str]:
        tried.append("pymupdf")
        try:
            import fitz  # PyMuPDF
        except ImportError:
            return "", 0, "pymupdf(未安装)"
        try:
            with fitz.open(str(path)) as doc:
                if doc.is_encrypted and not doc.authenticate(""):
                    raise CorruptedFileError(
                        f"{path.name} 已加密，无法解析。请提供解密后的版本。"
                    )
                limit = min(doc.page_count, self.context.max_pages)
                chunks = [doc.load_page(i).get_text("text").strip() for i in range(limit)]
                text = "\n\n".join(chunk for chunk in chunks if chunk)
                return text, doc.page_count, "pymupdf"
        except CorruptedFileError:
            raise
        except Exception as exc:  # noqa: BLE001
            return f"（pymupdf 解析异常：{exc}）", 0, "pymupdf(失败)"

    # ---------------------------------------------------------- 引擎三：OCR

    def _try_ocr(self, path: Path) -> tuple[str, int]:
        if not self.context.ocr_enabled:
            return "", 0
        try:
            import fitz
            from PIL import Image
            import pytesseract
        except ImportError:
            return "", 0
        try:
            with fitz.open(str(path)) as doc:
                limit = min(doc.page_count, self.context.max_pages)
                chunks: list[str] = []
                for index in range(limit):
                    pixmap = doc.load_page(index).get_pixmap(dpi=200)
                    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                    chunks.append(
                        pytesseract.image_to_string(image, lang=self.context.ocr_language)
                    )
                return "\n\n".join(chunk.strip() for chunk in chunks if chunk.strip()), doc.page_count
        except Exception as exc:  # noqa: BLE001 - OCR 失败退回"未提取到文本"的提示
            return f"（OCR 失败：{exc}）", 0


def ocr_available() -> tuple[bool, str]:
    """检测 OCR 环境是否就绪，供设置页展示。"""
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False, "未安装 pytesseract（pip install pytesseract），OCR 不可用"
    try:
        import pytesseract

        version = pytesseract.get_tesseract_version()
        return True, f"Tesseract {version} 可用"
    except Exception as exc:  # noqa: BLE001
        return False, f"未找到 Tesseract 可执行文件：{exc}"


__all__ = ["PdfLoader", "ocr_available"]
