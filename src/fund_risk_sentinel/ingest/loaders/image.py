"""图片材料解析（Pillow + 可选 OCR）。

发票、票据、签批单常以照片形式提交。本 loader 有两条出路：

1. **OCR 路线**：环境有 pytesseract + Tesseract 时，直接把图片转成文字；
2. **多模态路线**：若所用模型支持视觉输入（``supports_vision: true``），
   材料本身作为图片证据留存，由 ``review`` 阶段连同图片一并送模型。

两条路都走不通时，材料仍会被登记进证据链（记录尺寸、格式、指纹），
并在 ``metadata["needs_ocr"]`` 上打标，结论中会明示"该材料未能识别文字"，
避免审核人误以为系统"看过并认可"了这份材料。
"""

from __future__ import annotations

import base64
from pathlib import Path

from ...core.enums import DocumentFormat, DocumentRole
from ...core.exceptions import CorruptedFileError, IngestError
from ...core.models import Material
from ..base import BaseLoader

#: 送入模型时单张图片的字节上限（约 4MB，避免超出多数服务的请求体限制）
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


class ImageLoader(BaseLoader):
    """图片材料解析器。"""

    formats = (DocumentFormat.IMAGE,)
    extensions = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
    requires = ("Pillow",)

    def load(self, path: Path | str, *, role: DocumentRole = DocumentRole.OTHER) -> Material:
        p = Path(path)
        try:
            from PIL import Image
        except ImportError as exc:
            raise self._missing_dep("Pillow", "图片材料") from exc

        material = self._base_material(p, role, DocumentFormat.IMAGE)
        try:
            with Image.open(p) as image:
                image.load()
                material.metadata["image"] = {
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "format": image.format or p.suffix.lstrip(".").upper(),
                }
                material.page_count = 1
                # 保留一份可送入多模态模型的缩略副本（内存中，不落盘）
                if material.size_bytes <= _MAX_IMAGE_BYTES:
                    material.metadata["image_base64_mime"] = _mime_of(image.format or "")
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_error(p, exc) from exc

        ocr_text, ocr_note = self._ocr(p)
        if ocr_text.strip():
            material.text = ocr_text
            material.metadata["ocr_performed"] = True
            material.metadata["ocr_note"] = ocr_note
        else:
            material.metadata["needs_ocr"] = True
            material.metadata["ocr_note"] = ocr_note
            info = material.metadata.get("image", {})
            material.text = (
                f"【图片材料：{p.name}】\n"
                f"尺寸 {info.get('width', '?')}×{info.get('height', '?')}，"
                f"格式 {info.get('format', '未知')}。\n"
                f"当前未执行文字识别（{ocr_note}）。\n"
                "该材料已登记为证据，但其中的文字内容尚未进入分析范围，"
                "请开启 OCR 或改用支持视觉输入的模型后重新审查。"
            )
        return self._finalize(material)

    # ---------------------------------------------------------- OCR

    def _ocr(self, path: Path) -> tuple[str, str]:
        if not self.context.ocr_enabled:
            return "", "设置中未开启 OCR"
        try:
            import pytesseract
            from PIL import Image
        except ImportError:
            return "", "未安装 pytesseract"
        try:
            with Image.open(path) as image:
                text = pytesseract.image_to_string(image, lang=self.context.ocr_language)
            return text, "pytesseract"
        except Exception as exc:  # noqa: BLE001
            return "", f"OCR 执行失败：{exc}"

    # ---------------------------------------------------------- 多模态

    def to_data_url(self, material: Material) -> str | None:
        """把材料转为 ``data:image/...;base64,...``，供支持视觉的模型使用。"""
        path = Path(material.path) if material.path else None
        if path is None or not path.is_file():
            return None
        if material.size_bytes > _MAX_IMAGE_BYTES:
            return None
        mime = material.metadata.get("image_base64_mime") or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"


def _mime_of(fmt: str) -> str:
    mapping = {
        "JPEG": "image/jpeg",
        "JPG": "image/jpeg",
        "PNG": "image/png",
        "BMP": "image/bmp",
        "TIFF": "image/tiff",
        "WEBP": "image/webp",
    }
    return mapping.get(fmt.upper(), "image/png")


__all__ = ["ImageLoader"]
