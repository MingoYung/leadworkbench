"""Native task management and recycle bin, using the authenticated local API.

No store access is permitted here. In particular a table row is not authority
to delete anything: the server rechecks every selected task atomically.
"""
from __future__ import annotations

from urllib.parse import urlencode
from uuid import uuid4

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QHeaderView, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTabBar, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from .cli import ApiClient
from .native_helpers import ApiExecutor, STATUSES, date_text, error_text


_SCOPES = {"active": 0, "trash": 1}
_BLOCKED = {"running", "pause_requested", "queued"}
_STATUS_LABELS = {**STATUSES, "queued": "排队中"}
_ACTION_LABELS = {"trash": "移入回收站", "restore": "恢复", "purge": "永久删除"}
_PLAIN_HINT = int(Qt.ItemDataRole.UserRole) + 1


def _label(text, muted=False):
    label = QLabel(str(text))
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if muted:
        label.setProperty("muted", True)
    return label


def _api(client, method, path, body=None):
    return ApiClient(client.data_dir, timeout=30).request(method, path, body)


def _collection_count(run):
    summary = run.get("batch_summary") if isinstance(run.get("batch_summary"), dict) else {}
    counts = run.get("counts") if isinstance(run.get("counts"), dict) else {}
    def number(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    collected, stored = number(summary.get("collected")), number(summary.get("processed"))
    if stored is None:
        stored = number(counts.get("total"))
    fallback = collected is None or summary.get("collected_basis") == "stored_candidates"
    if collected is None:
        collected = stored
    ai_count = number(summary.get("ai_analyzed"))
    hints = []
    if fallback:
        hints.append("无采集快照，按已入库数量回退（兼容旧记录）。")
    hints.append("已入库：" + (str(stored) if stored is not None else "未提供") + " 条")
    hints.append("AI 已处理：" + (str(ai_count) if ai_count is not None else "未提供") + " 条")
    return collected if collected is not None else "—", "\n".join(hints)


class _TaskTable(QTableWidget):
    """Native elided cells with a tooltip that explicitly uses PlainText."""
    def __init__(self, parent=None):
        super().__init__(0, 4, parent)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._plain_tooltip = QLabel(self, Qt.WindowType.ToolTip)
        self._plain_tooltip.setTextFormat(Qt.TextFormat.PlainText)
        self._plain_tooltip.setWordWrap(True)
        self._plain_tooltip.setMaximumWidth(560)
        palette = self.palette()
        self._plain_tooltip.setStyleSheet("QLabel { background: " + palette.color(QPalette.ColorRole.ToolTipBase).name()
            + "; color: " + palette.color(QPalette.ColorRole.ToolTipText).name() + "; padding: 7px 9px; border-radius: 4px; }")
        self._tip_timer = QTimer(self)
        self._tip_timer.setSingleShot(True)
        self._tip_timer.timeout.connect(self._plain_tooltip.hide)
        self.viewport().installEventFilter(self)

    def eventFilter(self, watched, event):
        if watched is self.viewport():
            if event.type() == QEvent.Type.ToolTip:
                index = self.indexAt(event.pos())
                text = index.data(_PLAIN_HINT) if index.isValid() else None
                if isinstance(text, str) and text:
                    self._plain_tooltip.setText(text)
                    self._plain_tooltip.adjustSize()
                    position = event.globalPos() + QPoint(12, 18)
                    available = self.screen().availableGeometry()
                    position.setX(min(position.x(), available.right() - self._plain_tooltip.width() - 8))
                    position.setY(min(position.y(), available.bottom() - self._plain_tooltip.height() - 8))
                    self._plain_tooltip.move(position)
                    self._plain_tooltip.show()
                    self._tip_timer.start(8000)
                else:
                    self._plain_tooltip.hide()
                event.accept()
                return True
            if event.type() in {QEvent.Type.Leave, QEvent.Type.MouseButtonPress, QEvent.Type.Wheel, QEvent.Type.Hide}:
                self._plain_tooltip.hide()
        return super().eventFilter(watched, event)


class TaskManagerDialog(QDialog):
    """Task list / trash dialog. Successful mutations emit their exact receipt.

    ``initial_tab`` is ``active`` or ``trash``. ``refresh()`` reads only the
    selected scope. ``selected_run_ids()`` returns the current visible selection
    as sorted IDs; unselected/filtered rows never enter a mutation request.
    """
    changed = Signal(object)

    def __init__(self, client, parent=None, initial_tab="active"):
        super().__init__(parent)
        if not isinstance(initial_tab, str) or initial_tab not in _SCOPES:
            raise ValueError("initial_tab 应为 active 或 trash。")
        self.client = client
        self.executor = ApiExecutor(self)
        self._closed = False
        self._busy = False
        self._mutation_inflight = False
        self._rendering = False
        self._request_serial = 0
        self._runs = []
        self._loaded_scope = None
        self._operations = {}
        self._failed_operations = {}
        self._success_notice = None
        self._notice_kind = "info"
        self.setWindowTitle("任务管理")
        self.resize(960, 620)
        self.setMinimumSize(700, 440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        title_row = QHBoxLayout()
        title = _label("任务")
        title.setObjectName("pageTitle")
        title_row.addWidget(title)
        title_row.addStretch()
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        title_row.addWidget(self.refresh_button)
        layout.addLayout(title_row)
        self.tabs = QTabBar()
        self.tabs.setExpanding(False)
        self.tabs.addTab("任务管理")
        self.tabs.addTab("回收站")
        self.tabs.setTabData(0, "active")
        self.tabs.setTabData(1, "trash")
        self.tabs.setCurrentIndex(_SCOPES[initial_tab])
        layout.addWidget(self.tabs)
        filters = QHBoxLayout()
        filters.setSpacing(12)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索任务名称或 ID")
        self.search.setClearButtonEnabled(True)
        self.status_filter = QComboBox()
        self.status_filter.addItem("全部状态", "")
        for status, text in _STATUS_LABELS.items():
            self.status_filter.addItem(text, status)
        self.status_filter.setMinimumWidth(130)
        self.select_all_button = QPushButton("选择当前列表")
        self.select_all_button.setToolTip("选择当前筛选结果；也可在列表中使用 Ctrl / Shift 多选。每次最多操作 100 个任务。")
        self.select_all_button.clicked.connect(self.select_visible)
        self.clear_selection_button = QPushButton("清除选择")
        self.clear_selection_button.clicked.connect(lambda: self.table.clearSelection())
        filters.addWidget(self.search, 1)
        filters.addWidget(self.status_filter)
        filters.addWidget(self.select_all_button)
        filters.addWidget(self.clear_selection_button)
        layout.addLayout(filters)
        self.table = _TaskTable(self)
        self.table.setHorizontalHeaderLabels(["任务名称", "状态", "已采集", "创建时间"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for column in (1, 2, 3):
            self.table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setToolTip("按住 Ctrl / Shift 可多选；只操作明确选中的行。")
        layout.addWidget(self.table, 1)
        self.status = _label("正在读取任务…", True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.selection_count = _label("未选择任务", True)
        self.selection_count.setWordWrap(False)
        self.selection_count.setMinimumWidth(130)
        footer.addWidget(self.selection_count)
        footer.addStretch()
        self.trash_button = QPushButton("移入回收站")
        self.restore_button = QPushButton("恢复")
        self.restore_button.setProperty("primary", True)
        self.restore_button.setObjectName("primaryButton")
        self.purge_button = QPushButton("永久删除")
        self.purge_button.setProperty("variant", "danger")
        self.close_button = QPushButton("关闭")
        self.trash_button.clicked.connect(lambda: self.perform_action("trash"))
        self.restore_button.clicked.connect(lambda: self.perform_action("restore"))
        self.purge_button.clicked.connect(lambda: self.perform_action("purge"))
        self.close_button.clicked.connect(self.reject)
        for button in (self.trash_button, self.restore_button, self.purge_button, self.close_button):
            footer.addWidget(button)
        layout.addLayout(footer)
        self.search.textChanged.connect(self._render_runs)
        self.status_filter.currentIndexChanged.connect(self._render_runs)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.finished.connect(self._finished)
        self._update_scope_controls()
        self._update_actions()
        QTimer.singleShot(0, self.refresh)

    @property
    def scope(self):
        return self.tabs.tabData(self.tabs.currentIndex())

    def showEvent(self, event):
        super().showEvent(event)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            margin = max(32, self.frameGeometry().height() - self.height())
            self.resize(min(self.width(), max(700, available.width() - 32)),
                        min(self.height(), max(440, available.height() - margin - 24)))

    def _finished(self, _):
        self._closed = True
        self._request_serial += 1
        self.executor.shutdown(wait=False)

    def reject(self):
        if self._mutation_inflight:
            self._notice("操作尚未返回结果，请稍候。", "selection")
            return
        super().reject()

    def closeEvent(self, event):
        if self._mutation_inflight:
            self._notice("操作尚未返回结果，请稍候。", "selection")
            event.ignore()
            return
        super().closeEvent(event)

    def _notice(self, text, kind="info"):
        self._notice_kind = kind
        self.status.setText(str(text))
        self.status.setProperty("error", kind == "error")
        self.status.setProperty("warning", kind == "selection")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _update_scope_controls(self):
        active = self.scope == "active"
        self.trash_button.setVisible(active)
        self.restore_button.setVisible(not active)
        self.purge_button.setVisible(not active)
        self.table.horizontalHeaderItem(3).setText("创建时间" if active else "移入时间")

    def _set_busy(self, busy):
        self._busy = busy
        for widget in (self.tabs, self.search, self.status_filter, self.table, self.refresh_button):
            widget.setEnabled(not busy)
        self._update_actions()

    def selected_run_ids(self):
        return sorted({str(self.table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole))
                       for index in self.table.selectionModel().selectedRows(0)
                       if self.table.item(index.row(), 0) is not None})

    def _selected_runs(self):
        ids = set(self.selected_run_ids())
        return [run for run in self._runs if run.get("id") in ids]

    def _update_actions(self):
        ids = self.selected_run_ids()
        count = len(ids)
        ready = not self._busy and self._loaded_scope == self.scope
        valid = ready and 1 <= count <= 100
        blocked = any(run.get("status") in _BLOCKED for run in self._selected_runs())
        self.selection_count.setText(f"已选 {count} 个任务" if count else "未选择任务")
        self.select_all_button.setEnabled(ready and self.table.rowCount() > 0)
        self.clear_selection_button.setEnabled(ready and count > 0)
        self.trash_button.setEnabled(valid and self.scope == "active" and not blocked)
        self.restore_button.setEnabled(valid and self.scope == "trash")
        self.purge_button.setEnabled(valid and self.scope == "trash")
        self.trash_button.setToolTip("请先在任务页暂停运行中的任务，等待状态变为“已暂停”后刷新。" if blocked else "移入回收站后保留原文与审核，可恢复。")
        self.restore_button.setToolTip("恢复任务及其资料；不会自动开始或恢复采集。")
        self.purge_button.setToolTip("永久删除任务、原文证据与审核记录，无法恢复。")

    def _selection_changed(self):
        if self._rendering:
            return
        self._update_actions()
        if self._busy:
            return
        if len(self.selected_run_ids()) > 100:
            self._notice("每次最多操作 100 个任务，请缩小选择范围。", "selection")
        elif self.scope == "active" and any(run.get("status") in _BLOCKED for run in self._selected_runs()):
            self._notice("请先回任务页暂停运行中的任务，等显示“已暂停”后刷新。", "selection")
        elif self._notice_kind == "selection":
            self._notice(self._list_count_text())

    def select_visible(self):
        if not self._busy:
            self.table.selectAll()

    def _tab_changed(self, _):
        if self._closed:
            return
        self._runs = []
        self._loaded_scope = None
        self._update_scope_controls()
        self._render_runs()
        self.refresh()

    def _list_count_text(self):
        return f"{self.table.rowCount()} / {len(self._runs)} 个任务" if self.table.rowCount() != len(self._runs) else f"共 {len(self._runs)} 个任务"

    def _render_runs(self, *_):
        if self._closed:
            return
        selected = set(self.selected_run_ids())
        query = self.search.text().strip().casefold()
        status = self.status_filter.currentData()
        self._rendering = True
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(0)
            for run in self._runs:
                if status and run.get("status") != status:
                    continue
                if query and query not in (str(run.get("name", "")) + " " + str(run.get("id", ""))).casefold():
                    continue
                row = self.table.rowCount()
                self.table.insertRow(row)
                name = str(run.get("name") or "未命名任务")
                if run.get("is_demo") or run.get("source") == "demo":
                    name += " · 演示"
                count, count_hint = _collection_count(run)
                timestamp = run.get("deleted_at") if self.scope == "trash" else run.get("created_at")
                values = (name, _STATUS_LABELS.get(run.get("status"), str(run.get("status") or "未知状态")), count, date_text(timestamp))
                for column, value in enumerate(values):
                    item = QTableWidgetItem()
                    item.setData(Qt.ItemDataRole.DisplayRole, value)
                    item.setData(Qt.ItemDataRole.UserRole, run["id"])
                    if column == 0:
                        item.setData(_PLAIN_HINT, name)
                    if column == 2:
                        item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                        item.setData(_PLAIN_HINT, count_hint)
                    self.table.setItem(row, column, item)
                if run["id"] in selected:
                    for column in range(self.table.columnCount()):
                        self.table.item(row, column).setSelected(True)
        finally:
            self.table.blockSignals(False)
            self._rendering = False
        self._update_actions()
        if not self._busy and self._notice_kind != "error":
            self._notice(self._list_count_text())

    def _retry_state_notice(self):
        by_id = {run["id"]: run for run in self._runs}
        for (action, ids), snapshot in self._failed_operations.items():
            expected_scope = "active" if action == "trash" else "trash"
            if self.scope != expected_scope:
                continue
            if any(run_id not in by_id for run_id in ids):
                destination = "回收站" if action == "trash" else "任务管理"
                return f"上次{_ACTION_LABELS[action]}的结果未确认，部分任务已不在此列表。请检查{destination}；未自动重发。"
            if any(by_id[run_id].get("status") != old_status for run_id, old_status in snapshot.items()):
                return "上次操作后任务状态已变化，请核对后重试；同一操作会保留原操作 ID。"
        return None

    def refresh(self):
        if self._closed or self._busy:
            return
        self._request_serial += 1
        serial, scope = self._request_serial, self.scope
        self._set_busy(True)
        self._notice("正在读取任务…" if scope == "active" else "正在读取回收站…")
        def read():
            result = _api(self.client, "GET", "/api/runs?" + urlencode({"scope": scope}))
            items = result.get("items") if isinstance(result, dict) else None
            if not isinstance(items, list) or any(not isinstance(run, dict) or not isinstance(run.get("id"), str) for run in items):
                raise ValueError("任务列表格式异常，请刷新后重试。")
            return items
        def loaded(items):
            if self._closed or serial != self._request_serial or scope != self.scope:
                return
            self._runs, self._loaded_scope = items, scope
            self._render_runs()
            self._set_busy(False)
            retry_notice = self._retry_state_notice()
            self._notice(retry_notice or self._success_notice or self._list_count_text(), "selection" if retry_notice else "info")
            self._success_notice = None
        def failed(error):
            if self._closed or serial != self._request_serial:
                return
            self._set_busy(False)
            self._notice(error_text(error), "error")
        self.executor.submit(read, loaded, failed)

    def _confirm(self, action, count):
        if action == "restore":
            return True
        box = QMessageBox(self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setIcon(QMessageBox.Icon.Warning if action == "purge" else QMessageBox.Icon.Question)
        box.setWindowTitle(_ACTION_LABELS[action])
        if action == "purge":
            box.setText(f"永久删除 {count} 个任务？")
            box.setInformativeText("任务、采集原文、证据及审核记录将全部删除，无法恢复。")
        else:
            box.setText(f"将 {count} 个任务移入回收站？")
            box.setInformativeText("采集原文、证据与审核会保留，可在回收站恢复。")
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText(_ACTION_LABELS[action])
        box.button(QMessageBox.StandardButton.Cancel).setText("取消")
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.setEscapeButton(QMessageBox.StandardButton.Cancel)
        return box.exec() == QMessageBox.StandardButton.Yes

    def perform_action(self, action):
        if self._closed or self._busy:
            return
        expected_scope = "active" if action == "trash" else "trash"
        if action not in _ACTION_LABELS or self.scope != expected_scope or self._loaded_scope != self.scope:
            return
        ids = self.selected_run_ids()
        if not 1 <= len(ids) <= 100:
            self._notice("请选择 1–100 个任务。", "selection")
            return
        selected = self._selected_runs()
        if action == "trash" and any(run.get("status") in _BLOCKED for run in selected):
            self._notice("请先回任务页暂停运行中的任务，等显示“已暂停”后刷新。", "selection")
            return
        if not self._confirm(action, len(ids)):
            return
        key = (action, tuple(ids))
        operation_id = self._operations.setdefault(key, str(uuid4()))
        snapshot = {run["id"]: run.get("status") for run in selected}
        self._mutation_inflight = True
        self._set_busy(True)
        self._notice(f"正在{_ACTION_LABELS[action]} {len(ids)} 个任务…")
        def mutate():
            receipt = _api(self.client, "POST", f"/api/runs/{action}", {"run_ids": ids, "operation_id": operation_id})
            if (not isinstance(receipt, dict) or receipt.get("action") != action or receipt.get("operation_id") != operation_id
                    or not isinstance(receipt.get("run_ids"), list) or sorted(receipt["run_ids"]) != ids
                    or type(receipt.get("changed_count")) is not int or not 0 <= receipt["changed_count"] <= len(ids)):
                raise ValueError("服务器未返回完整操作回执，结果未确认；请刷新核对，重试会保留操作 ID。")
            return receipt
        def completed(receipt):
            if self._closed:
                return
            affected = set(ids)
            # A completed inverse action starts a new lifecycle. Do not reuse a
            # previous uncertain trash ID after an explicit successful restore.
            for cached in list(self._operations):
                if affected.intersection(cached[1]):
                    self._operations.pop(cached, None)
                    self._failed_operations.pop(cached, None)
            self._mutation_inflight = False
            self._set_busy(False)
            self._success_notice = f"已{_ACTION_LABELS[action]} {receipt['changed_count']} 个任务。"
            self.changed.emit(receipt)
            self.refresh()
        def failed(error):
            if self._closed:
                return
            self._mutation_inflight = False
            self._failed_operations[key] = snapshot
            self._set_busy(False)
            if getattr(error, "code", "") in {"RUN_RUNNING", "RUN_BUSY"}:
                message = "任务仍在运行或暂停请求尚未结束。请先暂停并等待状态稳定，再刷新重试。"
            else:
                message = error_text(error) + " 选择已保留；可刷新核对或重试同一操作。"
            self._notice(message, "error")
        self.executor.submit(mutate, completed, failed)
