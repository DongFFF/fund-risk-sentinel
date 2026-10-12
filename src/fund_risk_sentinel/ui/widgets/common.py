"""界面公共组件与样式。

界面遵循两条原则：

1. **风险等级的颜色语义**：高=红、中=橙、低=蓝、待核验=灰。颜色只用于风险等级，
   不用于装饰，避免"满屏颜色"导致真正重要的信号被淹没。
2. **降级必须显式**：任何降级执行的阶段都必须在界面上用醒目样式标出，
   不能让用户以为"这次跑的是完整能力"。
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------- 颜色

COLORS = {
    "primary": "#1a56db",
    "primary_dark": "#1544ad",
    "bg": "#f5f6f8",
    "surface": "#ffffff",
    "border": "#dcdfe6",
    "text": "#1f2329",
    "text_muted": "#6b7280",
    "high": "#d93025",
    "medium": "#e8a33d",
    "low": "#2b7cd3",
    "unknown": "#8a8f99",
    "ok": "#1a7f37",
    "warn": "#b45309",
    "degraded": "#b45309",
}

LEVEL_COLORS = {
    "high": COLORS["high"],
    "medium": COLORS["medium"],
    "low": COLORS["low"],
    "unknown": COLORS["unknown"],
}

STATUS_COLORS = {
    "ok": COLORS["ok"],
    "degraded": COLORS["degraded"],
    "failed": COLORS["high"],
    "skipped": COLORS["text_muted"],
    "pending": COLORS["text_muted"],
    "running": COLORS["primary"],
    "completed": COLORS["ok"],
    "needs_human": COLORS["medium"],
}

APP_STYLESHEET = f"""
QWidget {{
    background: {COLORS["bg"]};
    color: {COLORS["text"]};
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 13px;
}}
QLabel {{ background: transparent; }}
QLabel[role="title"] {{ font-size: 19px; font-weight: 600; }}
QLabel[role="subtitle"] {{ font-size: 13px; color: {COLORS["text_muted"]}; }}
QLabel[role="section"] {{ font-size: 15px; font-weight: 600; padding-top: 4px; }}
QLabel[role="hint"] {{ color: {COLORS["text_muted"]}; }}

QFrame[role="card"] {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 8px;
}}
QFrame[role="sidebar"] {{
    background: {COLORS["surface"]};
    border-right: 1px solid {COLORS["border"]};
}}
QFrame[role="divider"] {{ background: {COLORS["border"]}; max-height: 1px; border: none; }}

QListWidget[role="nav"] {{
    background: transparent;
    border: none;
    outline: none;
    padding: 6px;
}}
QListWidget[role="nav"]::item {{
    padding: 10px 14px;
    border-radius: 6px;
    margin: 2px 4px;
    color: {COLORS["text"]};
}}
QListWidget[role="nav"]::item:selected {{
    background: {COLORS["primary"]};
    color: #ffffff;
}}
QListWidget[role="nav"]::item:hover:!selected {{
    background: #eef1f6;
}}

QPushButton {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 6px;
    padding: 7px 16px;
    color: {COLORS["text"]};
}}
QPushButton:hover {{ border-color: {COLORS["primary"]}; color: {COLORS["primary"]}; }}
QPushButton:disabled {{ color: #b8bcc4; border-color: #e6e8ec; }}
QPushButton[role="primary"] {{
    background: {COLORS["primary"]};
    border-color: {COLORS["primary"]};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton[role="primary"]:hover {{ background: {COLORS["primary_dark"]}; }}
QPushButton[role="danger"] {{ color: {COLORS["high"]}; border-color: #f0c4c0; }}
QPushButton[role="ghost"] {{ border: none; color: {COLORS["primary"]}; padding: 4px 8px; }}

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextEdit {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: {COLORS["primary"]};
}}
QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {COLORS["primary"]};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}

QTableWidget {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 8px;
    gridline-color: #eef0f3;
    selection-background-color: #e8efff;
    selection-color: {COLORS["text"]};
}}
QHeaderView::section {{
    background: #f0f2f5;
    border: none;
    border-bottom: 1px solid {COLORS["border"]};
    padding: 8px 6px;
    font-weight: 600;
    color: {COLORS["text_muted"]};
}}
QTableWidget::item {{ padding: 4px 6px; }}

QProgressBar {{
    background: #eef0f3;
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{ background: {COLORS["primary"]}; border-radius: 4px; }}

QScrollArea {{ border: none; background: transparent; }}
QCheckBox {{ spacing: 6px; background: transparent; }}
QGroupBox {{
    background: {COLORS["surface"]};
    border: 1px solid {COLORS["border"]};
    border-radius: 8px;
    margin-top: 14px;
    padding: 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {COLORS["text_muted"]};
}}
QTabWidget::pane {{ border: 1px solid {COLORS["border"]}; border-radius: 8px; background: {COLORS["surface"]}; }}
QTabBar::tab {{
    background: transparent;
    padding: 8px 18px;
    border: none;
    color: {COLORS["text_muted"]};
}}
QTabBar::tab:selected {{ color: {COLORS["primary"]}; font-weight: 600; }}
QSplitter::handle {{ background: {COLORS["border"]}; }}
"""


# ---------------------------------------------------------------- 小组件

class Card(QFrame):
    """白底卡片容器。"""

    def __init__(self, parent: QWidget | None = None, *, padding: int = 16) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(padding, padding, padding, padding)
        self._layout.setSpacing(10)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget) -> QWidget:
        self._layout.addWidget(widget)
        return widget


class StatCard(QFrame):
    """指标卡片：一个大数字 + 一行说明。"""

    def __init__(self, label: str, value: str = "-", hint: str = "", color: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setProperty("role", "card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(4)

        self.value_label = QLabel(value)
        font = QFont()
        font.setPointSize(22)
        font.setBold(True)
        self.value_label.setFont(font)
        if color:
            self.value_label.setStyleSheet(f"color: {color};")
        self.title_label = QLabel(label)
        self.title_label.setProperty("role", "hint")
        self.hint_label = QLabel(hint)
        self.hint_label.setProperty("role", "hint")
        self.hint_label.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")

        layout.addWidget(self.value_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.hint_label)

    def set_value(self, value: str, hint: str = "", color: str = "") -> None:
        self.value_label.setText(value)
        if hint:
            self.hint_label.setText(hint)
        if color:
            self.value_label.setStyleSheet(f"color: {color};")


class Badge(QLabel):
    """彩色标签：风险等级 / 状态。"""

    def __init__(self, text: str, color: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.set_color(color or COLORS["text_muted"])

    def set_color(self, color: str) -> None:
        self.setStyleSheet(
            f"background: {color}1a; color: {color}; border: 1px solid {color}55;"
            "border-radius: 4px; padding: 2px 8px; font-size: 12px;"
        )


def level_badge(level: str, label: str = "") -> Badge:
    from ..core.enums import RISK_LEVEL_LABELS, RiskLevel

    member = RiskLevel.from_value(level, RiskLevel.UNKNOWN)
    return Badge(label or RISK_LEVEL_LABELS.get(member, level), LEVEL_COLORS.get(member.value, ""))


def status_badge(status: str, label: str = "") -> Badge:
    return Badge(label or status, STATUS_COLORS.get(status, COLORS["text_muted"]))


def section_title(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "section")
    return label


def hint_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("role", "hint")
    label.setWordWrap(True)
    return label


def divider() -> QFrame:
    line = QFrame()
    line.setProperty("role", "divider")
    line.setFixedHeight(1)
    line.setFrameShape(QFrame.HLine)
    return line


def primary_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("role", "primary")
    return button


def ghost_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("role", "ghost")
    return button


def hbox(*widgets: QWidget, spacing: int = 8) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for widget in widgets:
        layout.addWidget(widget)
    layout.addStretch(1)
    return container


class DataTable(QTableWidget):
    """统一的表格：只读、整行选中、表头自适应。"""

    def __init__(self, headers: Sequence[str], parent=None, *, stretch_last: bool = True) -> None:
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(list(headers))
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        if stretch_last:
            header.setStretchLastSection(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setSortingEnabled(False)

    def set_rows(
        self,
        rows: Iterable[Sequence[Any]],
        *,
        colors: dict[int, str] | None = None,
        tooltips: dict[int, str] | None = None,
    ) -> None:
        """填充数据。``colors`` 以行号为键指定整行文字颜色。"""
        row_list = [list(r) for r in rows]
        self.setRowCount(len(row_list))
        for r, row in enumerate(row_list):
            for c, value in enumerate(row):
                text = "" if value is None else str(value)
                item = QTableWidgetItem(text)
                item.setToolTip(text if len(text) < 300 else text[:300] + "…")
                if colors and r in colors:
                    item.setForeground(QColor(colors[r]))
                self.setItem(r, c, item)
            if tooltips and r in tooltips:
                for c in range(self.columnCount()):
                    cell = self.item(r, c)
                    if cell:
                        cell.setToolTip(tooltips[r])
        self.resizeRowsToContents()

    def selected_row(self) -> int:
        items = self.selectedItems()
        return items[0].row() if items else -1

    def current_row_data(self) -> list[str]:
        row = self.selected_row()
        if row < 0:
            return []
        return [
            self.item(row, c).text() if self.item(row, c) else ""
            for c in range(self.columnCount())
        ]


def read_only_text(value: str, *, height: int = 120) -> QWidget:
    """只读文本块（用于展示推理链条、依据等长文本）。"""
    from PySide6.QtWidgets import QPlainTextEdit

    editor = QPlainTextEdit()
    editor.setPlainText(value)
    editor.setReadOnly(True)
    editor.setMinimumHeight(height)
    editor.setStyleSheet(
        f"background: #fafbfc; border: 1px solid {COLORS['border']}; border-radius: 6px;"
    )
    return editor


__all__ = [
    "COLORS",
    "LEVEL_COLORS",
    "STATUS_COLORS",
    "APP_STYLESHEET",
    "Card",
    "StatCard",
    "Badge",
    "level_badge",
    "status_badge",
    "section_title",
    "hint_label",
    "divider",
    "primary_button",
    "ghost_button",
    "hbox",
    "DataTable",
    "read_only_text",
]
