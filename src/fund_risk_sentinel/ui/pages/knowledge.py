"""制度知识库页：检索制度条款、查看库内构成。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...core.models import ClauseRef
from ..context import AppContext
from ..widgets.common import (
    COLORS,
    Card,
    DataTable,
    hint_label,
    primary_button,
    section_title,
)

logger = logging.getLogger(__name__)


class KnowledgePage(QWidget):
    """制度知识库检索与浏览。"""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.results: list[ClauseRef] = []
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("制度知识库")
        title.setProperty("role", "title")
        title_box.addWidget(title)
        title_box.addWidget(
            hint_label(
                "把国家办法、负面清单、校内细则放进 config/knowledge/ 目录即可被自动收录。"
                "审查时系统会按材料内容检索最相关的条款，作为风险提示的制度依据。"
                "支持 .md / .txt（按「第X条」自动切分）与 .yaml（结构化条款）。"
            )
        )
        header.addLayout(title_box)
        header.addStretch(1)
        self.reload_button = primary_button("重新加载知识库")
        self.reload_button.clicked.connect(self.reload)
        header.addWidget(self.reload_button)
        layout.addLayout(header)

        # ---------------- 检索
        search_card = Card()
        row = QHBoxLayout()
        row.setSpacing(8)
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("输入查询内容，例如：差旅费 报销 标准 超预算 审批权限")
        self.query_edit.returnPressed.connect(self.search)
        row.addWidget(self.query_edit, stretch=1)

        row.addWidget(QLabel("返回条数："))
        self.top_k_spin = QSpinBox()
        self.top_k_spin.setRange(1, 50)
        self.top_k_spin.setValue(8)
        row.addWidget(self.top_k_spin)

        search_button = primary_button("检索")
        search_button.clicked.connect(self.search)
        row.addWidget(search_button)
        search_card.body().addLayout(row)
        search_card.body().addWidget(
            hint_label(
                "检索基于字符二元组与 BM25 打分，不依赖向量模型，断网与低配环境同样可用。"
            )
        )
        layout.addWidget(search_card)

        # ---------------- 结果与构成
        splitter = QSplitter(Qt.Horizontal)

        left_card = Card()
        left_card.body().addWidget(section_title("检索结果"))
        self.result_list = QListWidget()
        self.result_list.itemSelectionChanged.connect(self._show_selected)
        left_card.body().addWidget(self.result_list)
        splitter.addWidget(left_card)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        detail_card = Card()
        detail_card.body().addWidget(section_title("条款原文"))
        self.detail_view = QPlainTextEdit()
        self.detail_view.setReadOnly(True)
        self.detail_view.setStyleSheet(
            f"background: #fafbfc; border: 1px solid {COLORS['border']}; border-radius: 6px;"
        )
        detail_card.body().addWidget(self.detail_view)
        right_layout.addWidget(detail_card, stretch=2)

        stats_card = Card()
        stats_card.body().addWidget(section_title("库内构成"))
        self.stats_table = DataTable(["制度文件", "条款数"])
        self.stats_table.setMinimumHeight(160)
        stats_card.body().addWidget(self.stats_table)
        self.stats_label = QLabel("—")
        self.stats_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        stats_card.body().addWidget(self.stats_label)
        right_layout.addWidget(stats_card, stretch=1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, stretch=1)

        self.refresh()

    # ---------------------------------------------------------- 操作

    def refresh(self) -> None:
        kb = self.context.knowledge_base
        stats = kb.stats()
        rows = [
            [item["title"] or "（未命名）", item["clauses"]]
            for item in stats.get("documents", [])
        ]
        self.stats_table.set_rows(rows)
        self.stats_label.setText(
            f"共 {stats.get('clause_count', 0)} 条条款，来自 {stats.get('document_count', 0)} 份制度文件"
            f"　|　索引词 {stats.get('token_count', 0)} 个"
        )

    def reload(self) -> None:
        messages = self.context.reload_pipeline()
        self.refresh()
        self.result_list.clear()
        self.detail_view.clear()
        QMessageBox.information(self, "重新加载", "\n".join(messages) or "完成。")

    def search(self) -> None:
        query = self.query_edit.text().strip()
        if not query:
            QMessageBox.information(self, "提示", "请输入查询内容。")
            return
        self.results = self.context.knowledge_base.search(query, top_k=self.top_k_spin.value())
        self.result_list.clear()
        self.detail_view.clear()
        if not self.results:
            self.result_list.addItem("未检索到相关条款。可尝试更换关键词，或向知识库目录补充制度文件。")
            return
        for index, ref in enumerate(self.results, start=1):
            text = ref.text.strip().replace("\n", " ")
            preview = text[:60] + ("…" if len(text) > 60 else "")
            item = QListWidgetItem(f"{index}. {ref.display()}\n     {preview}")
            self.result_list.addItem(item)
        self.result_list.setCurrentRow(0)

    def _show_selected(self) -> None:
        row = self.result_list.currentRow()
        if row < 0 or row >= len(self.results):
            return
        ref = self.results[row]
        lines = [
            f"【制度文件】{ref.doc_title or '（未标注）'}",
            f"【文号】{ref.doc_no or '—'}",
            f"【条款】{ref.article or '—'}",
            f"【施行日期】{ref.effective_date or '—'}",
        ]
        if ref.url:
            lines.append(f"【原文链接】{ref.url}")
        lines.extend(["", "【条款原文】", ref.text or "（无原文）"])
        self.detail_view.setPlainText("\n".join(lines))
