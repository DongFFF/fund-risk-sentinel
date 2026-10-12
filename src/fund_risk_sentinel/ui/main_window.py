"""主窗口：左侧导航 + 右侧页面栈 + 底部状态栏。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.enums import RISK_LEVEL_LABELS, RiskLevel
from .context import AppContext
from .pages.dashboard import DashboardPage
from .pages.evaluation import EvaluationPage
from .pages.knowledge import KnowledgePage
from .pages.review import ReviewPage
from .pages.rules import RulesPage
from .pages.settings import SettingsPage
from .pages.upload import UploadPage
from .widgets.common import APP_STYLESHEET, COLORS, hint_label

logger = logging.getLogger(__name__)

#: 导航项：(键, 标题, 说明)
NAV_ITEMS = [
    ("dashboard", "系统概览", "查看整体情况与系统状态"),
    ("upload", "材料上传", "导入报销单、合同、发票、制度等材料"),
    ("review", "风险审查", "执行审查并复核风险线索"),
    ("rules", "规则库", "维护额度、用途、程序、时点、授权规则"),
    ("knowledge", "制度知识库", "检索制度条款作为风险提示依据"),
    ("evaluation", "能力评测", "六维指标与一致性回放测试"),
    ("settings", "设置", "模型接入、审查参数与数据安全"),
]


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self, context: AppContext) -> None:
        super().__init__()
        self.context = context
        self.pages: dict[str, QWidget] = {}
        self.setWindowTitle(f"{context.config.settings.app_name}　v{context.config.settings.app_version}")
        self.resize(1440, 900)
        self.setStyleSheet(APP_STYLESHEET)
        self._build()
        self._build_menu()
        self.refresh_status()

    # ---------------------------------------------------------- 构建

    def _build(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ---------------- 侧边栏
        sidebar = QFrame()
        sidebar.setProperty("role", "sidebar")
        sidebar.setFixedWidth(228)
        sidebar_layout = QVBoxLayout(sidebar)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        brand = QWidget()
        brand_layout = QVBoxLayout(brand)
        brand_layout.setContentsMargins(18, 18, 18, 12)
        brand_layout.setSpacing(2)
        brand_title = QLabel("科研经费风险识别")
        brand_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        brand_sub = QLabel("fund-risk-sentinel")
        brand_sub.setStyleSheet(f"color: {COLORS['text_muted']}; font-size: 11px;")
        brand_layout.addWidget(brand_title)
        brand_layout.addWidget(brand_sub)
        sidebar_layout.addWidget(brand)

        self.nav = QListWidget()
        self.nav.setProperty("role", "nav")
        for key, title, tip in NAV_ITEMS:
            item = QListWidgetItem(title)
            item.setData(Qt.UserRole, key)
            item.setToolTip(tip)
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        sidebar_layout.addWidget(self.nav, stretch=1)

        self.sidebar_hint = hint_label("")
        self.sidebar_hint.setContentsMargins(16, 8, 16, 16)
        sidebar_layout.addWidget(self.sidebar_hint)
        layout.addWidget(sidebar)

        # ---------------- 页面栈
        self.stack = QStackedWidget()
        self.pages = {
            "dashboard": DashboardPage(self.context),
            "upload": UploadPage(self.context),
            "review": ReviewPage(self.context),
            "rules": RulesPage(self.context),
            "knowledge": KnowledgePage(self.context),
            "evaluation": EvaluationPage(self.context),
            "settings": SettingsPage(self.context),
        }
        for key, _title, _tip in NAV_ITEMS:
            self.stack.addWidget(self.pages[key])
        layout.addWidget(self.stack, stretch=1)

        self.nav.setCurrentRow(0)

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("文件(&F)")

        refresh_action = QAction("重新加载配置与规则库(&R)", self)
        refresh_action.setShortcut(QKeySequence("Ctrl+R"))
        refresh_action.triggered.connect(self.reload_all)
        file_menu.addAction(refresh_action)

        export_action = QAction("导出最近一次审查底稿(&E)", self)
        export_action.setShortcut(QKeySequence("Ctrl+E"))
        export_action.triggered.connect(self.export_last)
        file_menu.addAction(export_action)

        file_menu.addSeparator()
        quit_action = QAction("退出(&Q)", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("帮助(&H)")
        about_action = QAction("关于(&A)", self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)

    # ---------------------------------------------------------- 导航

    def _on_nav_changed(self, row: int) -> None:
        if row < 0 or row >= len(NAV_ITEMS):
            return
        key = NAV_ITEMS[row][0]
        self.stack.setCurrentIndex(row)
        page = self.pages[key]
        if hasattr(page, "refresh"):
            try:
                page.refresh()
            except Exception:  # noqa: BLE001 - 刷新失败不应阻断页面切换
                logger.exception("页面 %s 刷新失败", key)
        self.refresh_status()

    def goto_page(self, key: str) -> None:
        """跳转到指定页面。"""
        for row, (item_key, _title, _tip) in enumerate(NAV_ITEMS):
            if item_key == key:
                self.nav.setCurrentRow(row)
                return

    def open_task(self, task_id: str) -> None:
        """打开历史任务的结论。"""
        self.goto_page("review")
        review_page = self.pages.get("review")
        if review_page is not None and hasattr(review_page, "open_task"):
            review_page.open_task(task_id)

    # ---------------------------------------------------------- 状态栏

    def refresh_status(self) -> None:
        provider = self.context.config.provider()
        engine_stats = self.context.rule_engine.describe()
        kb_size = len(self.context.knowledge_base)
        problems = self.context.config.validate()

        parts = [
            f"模型：{provider.name} / {provider.model or '-'}"
            + ("（本地）" if provider.is_local else "（云端）"),
            f"规则：{engine_stats.get('enabled', 0)} 条启用",
            f"知识库：{kb_size} 条条款",
        ]
        if problems:
            parts.append(f"⚠ 配置待处理 {len(problems)} 项")
        self.statusBar().showMessage("　|　".join(parts))

        self.sidebar_hint.setText(
            "输出为可核验线索，\n不构成合规认定；\n须经人工复核后处置。"
        )

    # ---------------------------------------------------------- 菜单动作

    def reload_all(self) -> None:
        try:
            messages = self.context.reload_all()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "重新加载失败", f"{type(exc).__name__}: {exc}")
            return
        self.pages["rules"].refresh()
        self.pages["knowledge"].refresh()
        self.pages["settings"].refresh()
        self.pages["dashboard"].refresh()
        self.refresh_status()
        QMessageBox.information(self, "重新加载完成", "\n".join(messages) or "完成。")

    def export_last(self) -> None:
        review_page = self.pages.get("review")
        if review_page is None:
            return
        if review_page.conclusion is None:
            QMessageBox.information(
                self, "提示", "还没有可导出的审查结论。请先在「风险审查」页执行一次审查，"
                "或从概览页双击历史任务载入结论。"
            )
            return
        review_page.export_markdown()

    def show_about(self) -> None:
        provider = self.context.config.provider()
        QMessageBox.about(
            self,
            "关于",
            f"""<b>{self.context.config.settings.app_name}</b><br>
版本：{self.context.config.settings.app_version}<br>
项目：fund-risk-sentinel<br><br>

本程序面向高校科研经费<b>事前预警、事中拦截</b>场景，按
「事项—规则—证据—风险—处置」五元关联框架组织审查流程，
把报销单、合同、发票、审批意见等材料与制度条款关联，
输出可核验的风险线索（风险等级 + 命中依据 + 关联材料 + 建议复核点）。

<br><b>能力边界</b><br>
· 输出的是待核验线索，不作出合规与否的认定；<br>
· 复核判断、授权处置与例外认定由具有相应权限的人员承担；<br>
· 模型不可用时会降级为规则引擎结果，并在结论中明确标注。<br>

<br><b>当前模型</b><br>
{provider.name} / {provider.model or '-'}
（{'本地部署，材料不出本机' if provider.is_local else '云端服务'}）<br><br>

配置目录：{self.context.config.settings.loaded_from or '（内置默认值）'}<br>
数据目录：{self.context.config.settings.resolved_data_dir()}
""",
        )
