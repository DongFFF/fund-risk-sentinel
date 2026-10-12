"""材料上传页：文件 / 目录 / 粘贴文本三种入口，支持拖拽。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...core.enums import DOCUMENT_ROLE_LABELS, EVENT_TYPE_LABELS, DocumentRole, EventType
from ...ingest.base import LoaderRegistry
from ...ingest.pipeline import IngestResult
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


class UploadPage(QWidget):
    """材料上传与解析。"""

    def __init__(self, context: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.last_result: IngestResult | None = None
        self._role_overrides: dict[str, DocumentRole] = {}
        self.setAcceptDrops(True)
        self._build()

    # ---------------------------------------------------------- 构建

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 24)
        layout.setSpacing(14)

        title = QLabel("材料上传")
        title.setProperty("role", "title")
        layout.addWidget(title)
        layout.addWidget(
            hint_label(
                "支持 PDF、Word、Excel、CSV、JSON、图片、纯文本与 Markdown。"
                "可直接把文件拖到本页面。材料解析后会先做脱敏，再进入审查流程。"
            )
        )

        # ---------------- 操作区
        action_card = Card()
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        self.pick_files_button = primary_button("选择文件…")
        self.pick_files_button.clicked.connect(self.pick_files)
        self.pick_folder_button = QPushButton("选择文件夹…")
        self.pick_folder_button.clicked.connect(self.pick_folder)
        self.paste_button = QPushButton("粘贴文本…")
        self.paste_button.clicked.connect(self.paste_text)
        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self.clear)
        for button in (
            self.pick_files_button,
            self.pick_folder_button,
            self.paste_button,
            self.clear_button,
        ):
            button_row.addWidget(button)
        button_row.addStretch(1)

        self.next_button = primary_button("下一步：开始审查")
        self.next_button.clicked.connect(self.goto_review)
        self.next_button.setEnabled(False)
        button_row.addWidget(self.next_button)
        action_card.body().addLayout(button_row)

        supported = "、".join(LoaderRegistry.supported_extensions())
        action_card.body().addWidget(hint_label(f"当前支持的扩展名：{supported}"))
        layout.addWidget(action_card)

        # ---------------- 材料表
        splitter = QSplitter(Qt.Vertical)

        table_card = Card()
        table_card.body().addWidget(section_title("已解析材料"))
        self.table = DataTable(["文件名", "材料角色", "格式", "页数", "字符数", "脱敏", "状态"])
        self.table.setMinimumHeight(220)
        self.table.itemSelectionChanged.connect(self._sync_role_combo)
        table_card.body().addWidget(self.table)

        role_row = QHBoxLayout()
        role_row.setSpacing(8)
        role_row.addWidget(QLabel("修改选中材料的角色："))
        self.role_combo = QComboBox()
        for role in DocumentRole:
            self.role_combo.addItem(DOCUMENT_ROLE_LABELS.get(role, role.value), role.value)
        role_row.addWidget(self.role_combo)
        apply_button = QPushButton("应用")
        apply_button.clicked.connect(self.apply_role)
        role_row.addWidget(apply_button)
        role_row.addStretch(1)
        self.summary_label = QLabel("尚未上传材料")
        self.summary_label.setStyleSheet(f"color: {COLORS['text_muted']};")
        role_row.addWidget(self.summary_label)
        table_card.body().addLayout(role_row)
        splitter.addWidget(table_card)

        warn_card = Card()
        warn_card.body().addWidget(section_title("解析提示"))
        self.warning_list = QListWidget()
        self.warning_list.setStyleSheet("background: transparent; border: none;")
        warn_card.body().addWidget(self.warning_list)
        splitter.addWidget(warn_card)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, stretch=1)

    # ---------------------------------------------------------- 上传入口

    def pick_files(self) -> None:
        extensions = " ".join(f"*{e}" for e in LoaderRegistry.supported_extensions())
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择经费材料", "", f"支持的材料 ({extensions});;全部文件 (*)"
        )
        if files:
            self.load_paths(files)

    def pick_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择材料所在文件夹")
        if not folder:
            return
        recursive = (
            QMessageBox.question(
                self,
                "是否包含子文件夹",
                "是否递归扫描子文件夹中的材料？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            == QMessageBox.Yes
        )
        self.load_folder(folder, recursive=recursive)

    def paste_text(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("粘贴文本材料")
        dialog.resize(720, 520)
        layout = QVBoxLayout(dialog)
        layout.addWidget(hint_label("可直接粘贴一段制度条文或说明文字，作为材料参与审查。"))

        layout.addWidget(QLabel("材料名称："))
        name_edit = QLineEdit("手工录入材料")
        layout.addWidget(name_edit)

        content_edit = QPlainTextEdit()
        content_edit.setPlaceholderText("在此粘贴文本内容…")
        layout.addWidget(content_edit, stretch=1)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(dialog.reject)
        confirm = primary_button("导入")
        confirm.clicked.connect(dialog.accept)
        row.addWidget(cancel)
        row.addWidget(confirm)
        layout.addLayout(row)

        if dialog.exec() != QDialog.Accepted:
            return
        content = content_edit.toPlainText().strip()
        if not content:
            QMessageBox.information(self, "提示", "内容为空，未导入。")
            return
        result = self.context.pipeline.ingest.load_text(
            content, name=name_edit.text().strip() or "手工录入材料"
        )
        self._merge_result(result)

    def clear(self) -> None:
        self.last_result = None
        self._role_overrides.clear()
        self.context.pending_materials = []
        self.context.pending_title = ""
        self.table.setRowCount(0)
        self.warning_list.clear()
        self.summary_label.setText("尚未上传材料")
        self.next_button.setEnabled(False)

    # ---------------------------------------------------------- 加载

    def load_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        self._set_busy(True)
        try:
            result = self.context.pipeline.ingest.load_paths(paths)
        except Exception as exc:  # noqa: BLE001 - 解析异常要在界面呈现
            logger.exception("材料解析失败")
            QMessageBox.critical(self, "解析失败", f"材料解析过程出错：\n{exc}")
            return
        finally:
            self._set_busy(False)
        self._merge_result(result)

    def load_folder(self, folder: str, *, recursive: bool = True) -> None:
        self._set_busy(True)
        try:
            result = self.context.pipeline.ingest.load_folder(folder, recursive=recursive)
        except Exception as exc:  # noqa: BLE001
            logger.exception("目录解析失败")
            QMessageBox.critical(self, "解析失败", f"目录解析过程出错：\n{exc}")
            return
        finally:
            self._set_busy(False)
        self._merge_result(result)

    def _merge_result(self, result: IngestResult) -> None:
        """把新一批解析结果并入当前列表。"""
        if self.last_result is None:
            self.last_result = result
        else:
            self.last_result.materials.extend(result.materials)
            self.last_result.items.extend(result.items)
            self.last_result.warnings.extend(result.warnings)

        self.context.pending_materials = list(self.last_result.materials)
        self._refresh_table()
        self._refresh_warnings()
        self.next_button.setEnabled(bool(self.last_result.materials))

        if self.last_result.materials:
            self._log_ingest(self.last_result)

    def _log_ingest(self, result: IngestResult) -> None:
        try:
            self.context.audit.log_ingest(
                self.context.last_task.task_id if self.context.last_task else "(未创建任务)",
                summary=result.summary(),
                materials=[
                    {
                        "filename": m.filename,
                        "role": m.role.value,
                        "format": m.fmt.value,
                        "sha256": m.sha256[:16],
                        "chars": m.char_count,
                        "redacted": m.redaction_applied,
                    }
                    for m in result.materials
                ],
                warnings=result.warnings,
            )
        except Exception:  # noqa: BLE001 - 审计失败已在 AuditLogger 内部抛错，这里只记录
            logger.exception("写入上传审计日志失败")

    # ---------------------------------------------------------- 表格

    def _refresh_table(self) -> None:
        rows = []
        colors: dict[int, str] = {}
        for index, material in enumerate(self.context.pending_materials):
            item = None
            if self.last_result:
                for candidate in self.last_result.items:
                    if candidate.material_id == material.material_id:
                        item = candidate
                        break
            rows.append(
                [
                    material.filename,
                    DOCUMENT_ROLE_LABELS.get(material.role, material.role.value),
                    material.fmt.value,
                    material.page_count or "-",
                    f"{material.char_count:,}",
                    "已脱敏" if material.redaction_applied else "—",
                    "就绪" if (item is None or item.ok) else "异常",
                ]
            )
            if material.metadata.get("needs_ocr"):
                colors[index] = COLORS["warn"]
        self.table.set_rows(rows, colors=colors)
        self.summary_label.setText(
            f"共 {len(self.context.pending_materials)} 份材料，"
            f"合计 {sum(m.char_count for m in self.context.pending_materials):,} 字符"
        )

    def _refresh_warnings(self) -> None:
        self.warning_list.clear()
        if not self.last_result:
            return
        for item in self.last_result.failed:
            self.warning_list.addItem(f"[失败] {item.filename}：{item.error}")
        for item in self.last_result.duplicates:
            self.warning_list.addItem(f"[跳过] {item.filename}：{item.skipped_reason}")
        for warning in dict.fromkeys(self.last_result.warnings):
            self.warning_list.addItem(f"[提示] {warning}")
        if self.warning_list.count() == 0:
            self.warning_list.addItem("解析过程未发现问题。")

    def _sync_role_combo(self) -> None:
        row = self.table.selected_row()
        if row < 0 or row >= len(self.context.pending_materials):
            return
        role = self.context.pending_materials[row].role
        index = self.role_combo.findData(role.value)
        if index >= 0:
            self.role_combo.setCurrentIndex(index)

    def apply_role(self) -> None:
        row = self.table.selected_row()
        if row < 0 or row >= len(self.context.pending_materials):
            QMessageBox.information(self, "提示", "请先在表格中选择一份材料。")
            return
        role = DocumentRole.from_value(self.role_combo.currentData(), DocumentRole.OTHER)
        material = self.context.pending_materials[row]
        material.role = role
        material.metadata["role_inferred"] = False
        self._refresh_table()
        self.table.selectRow(row)

    def _set_busy(self, busy: bool) -> None:
        self.pick_files_button.setEnabled(not busy)
        self.pick_folder_button.setEnabled(not busy)
        self.paste_button.setEnabled(not busy)
        self.summary_label.setText("正在解析材料…" if busy else self.summary_label.text())

    # ---------------------------------------------------------- 拖拽

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        paths: list[str] = []
        folders: list[str] = []
        for url in event.mimeData().urls():
            path = Path(url.toLocalFile())
            if path.is_dir():
                folders.append(str(path))
            elif path.is_file():
                paths.append(str(path))
        if paths:
            self.load_paths(paths)
        for folder in folders:
            self.load_folder(folder, recursive=True)

    # ---------------------------------------------------------- 跳转

    def goto_review(self) -> None:
        if not self.context.pending_materials:
            QMessageBox.information(self, "提示", "请先上传至少一份材料。")
            return
        title, ok = QInputDialog.getText(
            self,
            "任务名称",
            "为本次审查命名（便于日后检索）：",
            text=f"{len(self.context.pending_materials)} 份材料的经费审查",
        )
        if not ok:
            return
        self.context.pending_title = title.strip() or "经费审查任务"
        window = self.window()
        if hasattr(window, "goto_page"):
            window.goto_page("review")
