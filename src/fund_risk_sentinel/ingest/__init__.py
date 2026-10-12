"""材料上传与解析层。

对外三件事：

.. code-block:: python

    from fund_risk_sentinel.ingest import IngestPipeline, LoaderRegistry

    pipeline = IngestPipeline()
    result = pipeline.load_folder("D:/某项目材料")          # 整目录导入
    result = pipeline.load_paths(["报销单.pdf", "发票.jpg"])  # 指定文件
    result = pipeline.load_bytes("报销单.docx", raw_bytes)    # 接口上传
    result = pipeline.load_text("...", name="制度摘录.txt")   # 手工录入

    for item in result.failed:
        print(item.filename, item.error)

    print(result.summary())
"""

from .base import (
    EXTENSION_MAP,
    BaseLoader,
    LoaderContext,
    LoaderRegistry,
    detect_format,
    file_sha256,
)
from .loaders import register_all
from .pipeline import IngestItem, IngestPipeline, IngestResult, ProgressCallback
from .redaction import Redactor
from .roles import describe_role, infer_role

__all__ = [
    "BaseLoader",
    "LoaderContext",
    "LoaderRegistry",
    "EXTENSION_MAP",
    "detect_format",
    "file_sha256",
    "register_all",
    "IngestPipeline",
    "IngestResult",
    "IngestItem",
    "ProgressCallback",
    "Redactor",
    "infer_role",
    "describe_role",
]
