"""PySide6 桌面界面层。

页面划分与「三类核心能力」对应：

- ``pages/upload.py``      材料上传与解析（能力①的前置）
- ``pages/review.py``      审查执行与线索复核（能力①②③的汇总出口）
- ``pages/rules.py``       规则库维护（能力②的知识来源）
- ``pages/knowledge.py``   制度条款检索（能力②的检索依据）
- ``pages/evaluation.py``  六维能力评测（课题表2 落地）
- ``pages/settings.py``    模型接入与数据安全（"可调用任何模型 API"的入口）
- ``pages/dashboard.py``   概览与系统状态

界面只负责"呈现与收集"，全部判断逻辑都在 ``review/`` ``rules/`` 等
业务层，便于脱离界面做自动化测试。
"""

from __future__ import annotations

__all__ = ["MainWindow", "AppContext"]


def __getattr__(name: str):  # pragma: no cover - 延迟导入，避免无 Qt 环境 import 失败
    if name == "MainWindow":
        from .main_window import MainWindow

        return MainWindow
    if name == "AppContext":
        from .context import AppContext

        return AppContext
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
