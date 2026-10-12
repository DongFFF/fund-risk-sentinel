"""界面页面。每个页面是一个独立的 ``QWidget``，只依赖 ``AppContext``。"""

from __future__ import annotations

from .dashboard import DashboardPage
from .evaluation import EvaluationPage
from .knowledge import KnowledgePage
from .review import ReviewPage
from .rules import RulesPage
from .settings import SettingsPage
from .upload import UploadPage

__all__ = [
    "DashboardPage",
    "UploadPage",
    "ReviewPage",
    "RulesPage",
    "KnowledgePage",
    "EvaluationPage",
    "SettingsPage",
]
