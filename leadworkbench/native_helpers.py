"""Shared QtWidgets presentation helpers for the native desktop client.

Business operations belong to the authenticated HTTP client. This module only
coordinates background callables and native widget presentation; it never
starts a model, touches evidence storage or opens a source URL by itself.
"""

from __future__ import annotations

import json
from base64 import b64decode
import logging
import socket
import threading
import uuid
import weakref
from datetime import date, datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Qt, QUrl, Signal, Slot, qRegisterResourceData
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QSizePolicy, QToolButton, QVBoxLayout, QWidget
from shiboken6 import isValid

__all__ = [
    "ApiExecutor", "CollapsibleSection", "DARK_STYLESHEET", "apply_dark_theme",
    "error_text", "safe_source_url", "display", "date_text", "DECISIONS",
    "STATUSES", "SOURCES", "ROLE_LABELS",
]

DECISIONS = {
    "unreviewed": "未审核", "keep": "保留", "exclude": "排除",
    "needs_info": "补查", "watch": "观察",
}
STATUSES = {
    "created": "待运行", "running": "运行中", "pause_requested": "正在暂停",
    "paused": "已暂停", "completed": "已完成", "completed_partial": "部分完成",
    "failed": "运行失败", "interrupted": "已中断",
}
SOURCES = {
    "demo": "演示体验", "import": "导入记录", "urls": "网页链接",
    "opencli": "平台搜索", "comments": "指定帖子评论",
}
ROLE_LABELS = {
    "buyer": "采购方", "seller": "供应方", "consumer": "消费者",
    "discussion": "讨论", "unknown": "身份待核",
}

_log = logging.getLogger(__name__)
_LIVE_TASKS: dict[str, "_ApiTask"] = {}
_LIVE_LOCK = threading.Lock()


def display(value: Any, empty: str = "待确认") -> str:
    """Display a stored value without inventing a missing fact."""
    if value is None or value == "":
        return empty
    if isinstance(value, (dict, list, tuple, bool)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            pass
    return str(value)


def date_text(value: Any) -> str:
    """Render ISO dates in the computer's local zone; retain unfamiliar input."""
    if value is None or value == "":
        return "时间未知"
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, date):
            return value.strftime("%Y-%m-%d")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            seconds = value / 1000 if abs(value) > 100_000_000_000 else value
            parsed = datetime.fromtimestamp(seconds).astimezone()
        else:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone()
        return parsed.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OverflowError, OSError):
        return str(value)


def safe_source_url(value: Any) -> QUrl | None:
    """Return a valid absolute HTTP(S) source URL, never a file or script URL."""
    if not isinstance(value, (str, QUrl)):
        return None
    text = value.toString() if isinstance(value, QUrl) else value.strip()
    if not text or any(ord(char) < 32 for char in text):
        return None
    url = QUrl(text, QUrl.ParsingMode.StrictMode)
    if (not url.isValid() or url.scheme().lower() not in {"http", "https"}
            or not url.host() or url.userName() or url.password()):
        return None
    return url


def error_text(exc: Any) -> str:
    """Translate common transport failures while preserving useful API messages."""
    if exc is None:
        return "操作未完成，请重试。"
    client_messages = {
        "SERVER_OFFLINE": "无法连接本地服务。请确认获客平台后台已启动。",
        "SERVER_CONFIG": "本地服务连接信息无效，请重新启动获客平台。",
        "REDIRECT_BLOCKED": "本地服务地址发生重定向，已停止请求。请重新启动获客平台。",
        "BODY_TOO_LARGE": "提交内容过大，请减少单次导入数量。",
        "RESPONSE_TOO_LARGE": "返回内容过大，请减少单页条数后重试。",
        "INVALID_RESPONSE": "本地服务返回内容无法识别，请重新连接后重试。",
    }
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in client_messages:
        return client_messages[code]
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "请求超时。请确认本地服务仍在运行，稍后重试。"
    if isinstance(exc, ConnectionRefusedError):
        return "无法连接本地服务。请确认获客平台后台已启动。"
    if isinstance(exc, HTTPError):
        messages = {
            400: "请求内容未通过校验，请检查输入。",
            401: "本地服务身份已失效，请重新连接。",
            403: "本地服务拒绝请求，请重新连接后重试。",
            404: "记录或接口不存在，请刷新列表。",
            409: "记录已被其他操作更新，请刷新后重新审核。",
            413: "提交内容过大，请减少单次导入数量。",
            429: "操作过于频繁，请稍后重试。",
            500: "本地服务处理失败，请查看服务日志后重试。",
            503: "本地服务暂时不可用，请稍后重试。",
        }
        return messages.get(exc.code, f"本地服务返回 HTTP {exc.code}，请稍后重试。")
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            return error_text(reason)
        return f"无法连接本地服务：{reason}"
    if isinstance(exc, FileNotFoundError):
        return f"找不到文件：{exc.filename}" if exc.filename else "找不到指定文件，请检查路径。"
    if isinstance(exc, PermissionError):
        return "没有权限访问指定文件或位置，请选择可读写的位置。"
    if isinstance(exc, json.JSONDecodeError):
        return f"JSON 格式有误：第 {exc.lineno} 行，第 {exc.colno} 列。"
    if isinstance(exc, dict):
        error = exc.get("error", exc)
        if isinstance(error, dict):
            return display(error.get("message") or error.get("code"), "操作未完成，请重试。")
    message = str(exc).strip()
    return message or "操作未完成，请重试。"


class _Callback:
    """Do not retain a bound widget solely because a request is in flight."""

    def __init__(self, callback: Callable[[Any], None] | None) -> None:
        self._strong = None
        self._weak = None
        receiver = getattr(callback, "__self__", None)
        self._receiver = weakref.ref(receiver) if isinstance(receiver, QObject) else None
        if callback is not None and self._receiver is not None:
            try:
                self._weak = weakref.WeakMethod(callback)
            except TypeError:
                self._strong = callback
        else:
            self._strong = callback

    def invoke(self, value: Any) -> None:
        if self._receiver is not None:
            receiver = self._receiver()
            if receiver is None or not isValid(receiver):
                return
        callback = self._weak() if self._weak is not None else self._strong
        if callback is not None:
            try:
                callback(value)
            except BaseException as exc:
                # A presentation callback must not unwind through Qt's event loop.
                # Avoid dumping response data, credentials or callback arguments.
                _log.error("Native API callback failed (%s)", type(exc).__name__)


class _TaskSignals(QObject):
    completed = Signal(str, object, bool)


class _ApiTask(QRunnable):
    def __init__(self, token: str, fn: Callable[[], Any]) -> None:
        super().__init__()
        self.token = token
        self.fn = fn
        self.signals = _TaskSignals()
        self.done = threading.Event()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:
        failed = False
        try:
            result = self.fn()
        except BaseException as exc:
            result = exc
            failed = True
        try:
            self.signals.completed.emit(self.token, result, failed)
        except RuntimeError:
            # The UI may have been destroyed while an HTTP request completed.
            pass
        finally:
            self.done.set()
            with _LIVE_LOCK:
                _LIVE_TASKS.pop(self.token, None)


class ApiExecutor(QObject):
    """Run blocking HTTP work in Qt's global pool and deliver results on the GUI.

    Construct and submit from the application thread. ``fn`` takes no arguments;
    callbacks receive one result or one exception respectively. Bound QObject
    callbacks are weakly held and skipped after their receiver is destroyed.
    The executor never owns a QThread, so closing a dialog cannot destroy an
    active thread. ``shutdown(False)`` drops callbacks but lets work finish;
    ``shutdown(True)`` additionally waits for this executor's worker functions.
    """

    busy_changed = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        app = QApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RuntimeError("ApiExecutor 必须在 Qt 应用的界面线程中创建。")
        self._pool = QThreadPool.globalInstance()
        self._pending: dict[str, tuple[_Callback, _Callback, threading.Event]] = {}
        self._events: dict[str, threading.Event] = {}
        self._closed = False

    @property
    def is_busy(self) -> bool:
        return bool(self._pending)

    def submit(
        self,
        fn: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> str:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("请在界面线程提交后台操作。")
        if self._closed:
            raise RuntimeError("后台执行器已关闭，无法提交新操作。")
        if not callable(fn):
            raise TypeError("后台操作必须是可调用函数。")
        if on_success is not None and not callable(on_success):
            raise TypeError("成功回调必须是可调用函数。")
        if on_error is not None and not callable(on_error):
            raise TypeError("错误回调必须是可调用函数。")
        was_busy = self.is_busy
        token = uuid.uuid4().hex
        task = _ApiTask(token, fn)
        task.signals.completed.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._pending[token] = (_Callback(on_success), _Callback(on_error), task.done)
        self._events[token] = task.done
        with _LIVE_LOCK:
            _LIVE_TASKS[token] = task
        try:
            self._pool.start(task)
        except BaseException:
            self._pending.pop(token, None)
            self._events.pop(token, None)
            with _LIVE_LOCK:
                _LIVE_TASKS.pop(token, None)
            raise
        if not was_busy:
            self.busy_changed.emit(True)
        return token

    @Slot(str, object, bool)
    def _deliver(self, token: str, result: Any, failed: bool) -> None:
        self._events.pop(token, None)
        pending = self._pending.pop(token, None)
        if pending is None or self._closed:
            return
        if not self.is_busy:
            self.busy_changed.emit(False)
        success, error, _done = pending
        (error if failed else success).invoke(result)

    def shutdown(self, wait: bool = False) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("请在界面线程关闭后台执行器。")
        was_busy = self.is_busy
        events = list(self._events.values())
        self._closed = True
        self._pending.clear()
        if was_busy:
            self.busy_changed.emit(False)
        if wait:
            for event in events:
                event.wait()
            self._events.clear()


# Tiny PNG glyphs are embedded in Qt's resource registry, so the native
# indicator marks work from source and a frozen executable without loose assets
# or temporary files. Generated with Qt rcc; keep the byte buffers alive.
_INDICATOR_RESOURCE = (
    b64decode(
        b"AAAAAAACAAAAAQAAAAEAAAAAAAAAAAAAAAAAAgAAAAEAAAACAAAAAAAAAAAAAAAgAAIAAAABAAAAAwAAAAAAAAAA"
        b"AAAAMgACAAAAAwAAAAQAAAAAAAAAAAAAAGIAAAAAAAEAAAOzAAABoZm1M4sAAABKAAAAAAABAAAAAAAAAaGZtTOK"
        b"AAAAegAAAAAAAQAABa4AAAGhmbUzig=="
    ),
    b64decode(
        b"AA0Md/mYAGwAZQBhAGQAdwBvAHIAawBiAGUAbgBjAGgABgdIsMUAbgBhAHQAaQB2AGUACQr5a+IAaQBuAGQAaQBj"
        b"AGEAdABvAHIACQuehIcAYwBoAGUAYwBrAC4AcABuAGcACQsCp6cAcgBhAGQAaQBvAC4AcABuAGcACw+SRAcAcABh"
        b"AHIAdABpAGEAbAAuAHAAbgBn"
    ),
    b64decode(
        b"AAADr4lQTkcNChoKAAAADUlIRFIAAAAwAAAAMAgGAAAAVwL5hwAAAAlwSFlzAAAPYQAAD2EBqD+naQAAA2FJREFU"
        b"aIHtl19oHFUUh7+TxE03Wtp0ZqmSxs7uBhF8EftgZ9KWUrW0UkippC21GPEPWCyliIqIDyqCgkIVRaWCYqHUmpY2"
        b"1YDQPgRxN/Wh0AcbyEOyA1FEc5NuVUrSdvf2Ie1Dws7dyezG9GG+x73nnt/v7jlzZi7ExMTExMTExERGFtvAXOyM"
        b"uw3kebQsQdOn/NxhoBwUf0cdwE67r4J8OOtH0V+q0cF9QXvumAPYjnsQkUOV1soNTfdPjvw8VmmtYWFthcNKd+4P"
        b"Mg/QWC45QWuLfgAr4+0T9KeGkOnp6+VLQYtNC+ApNHbaexHN56YYgdf++X1wMmh90SqQyrjPAYdNMVrzxnghb6oO"
        b"jXV1FRLL6ewBvsYwRATeUn7+/Wq5/vcpZKW9vQJHjNqat5WffydMvjAtJCuyG9rv7VifCukxENvxdgt8i8G8hveU"
        b"n383bE5jBezMugegfAzNI7fS/3BDNz9T9AeKYQVuk0q73Ro5hqFtRcsH437uTUCHzWs4QHejnf7jN+DBORvO64Rs"
        b"VsO5f8OKWJnOHaL19xifOflIFXKvMw/zYGghO/vnw8wxz0z2tVzT/StXbr47jIDleF2i9XFM5kU+jmIeoo/R9aWW"
        b"//pWrXKTpiA7424ToRfD+0bQn6nR3CtEMA+GA6iR+y4Cw4a9j00lONnRsbW50mLKcbeg5SRwV2AGzRfjhcEDRDQP"
        b"xgr0lkC6gL+CDcjWYunKcdasmWVyRbrzCS1yGkgYtL9Sfn4/NZiHKi2kCrnhUgOPC0wYwrqsyeajbNzYBGBlvU0N"
        b"6DNAxcoAaM03qpB/CcN3fliqvomnLo/93bKs7Rwiu4AllWIEHmq5UsomrfailPkRCHw2BDmi/LYXYKhm87e0w2E5"
        b"6x4VKZ8Fltagd1QV2npm2rM+hJ5CE/4vv4rIk8DVSErCd2p14tl6mp9JO0+srLdJyvQT0E6V0NA7sTqxh4GBG/PV"
        b"q0akj7mU427RIn2Yp8yMgMip8dapXVy4cD2KVtX8UTdajtclwgnMl6IzKlnsZmjoWlSdakS+0Ez4+T5BniZ4FPYv"
        b"b1y2cyHNQ40XmqvFsUvJ1vaCwHZmVVN+ukcnnhodPTddo7+q1OVCk0p7GzT6ZUFaNXJWJS9/stD/fExMTExMTExM"
        b"HbgJQTD6TUxCyI8AAAAASUVORK5CYIIAAAH3iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAACXBI"
        b"WXMAAA9hAAAPYQGoP6dpAAABqUlEQVRoge2Yv0oDQRCHv0mutLVLJQgpfApbL2opaGGraPAJInmFU+zEFArp/ENi"
        b"6VMIFkK6VGlT6o5FtgwmO3snqPu1d/Ob+e3c7e0cJBKJRCKRSPxfpEyxq4GufcAesCmwAaz6SxOFV+Alg/5xLqOy"
        b"cpZioHjWRs3RVTgEagtudwK9utI5ask4Nne0gcuh7qhyC6wEhk6B/dNcnmLyL1qtbymG2lblnvDi8TEPxVDbMTWY"
        b"O+BX/iEmuUeBXWsnTAaKZ22I4w3bys9jmilNyztheoRqji7lFQ+w8iF0LYHBHbga6NonvBP5/szB1WE9dIsNLsLv"
        b"82UXD1Dz2mFBhkSbhpjKtIMN+C9sJVi0LR1YXXyLmWDtKp7lH8ViYFJ6FRHawQb8qbISLNqWDrwYYirTDjaQQR9w"
        b"oXFL4Lx2EMEGjnMZCfRC45bgxjLomHahutJhdp4vi2mmnFsCTQaOWjIW4YDZUTgWBfat05n5O3CyJY8qnBFnQlU4"
        b"i5nKokfKi4FuA3f8xpES4DSXp0xpAtcstzs54DpTmrHFwx/4rZJIJBKJRCLxn/kCYGt8d6pGqjYAAAAASUVORK5C"
        b"YIIAAAD1iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAACXBIWXMAAA9hAAAPYQGoP6dpAAAAp0lE"
        b"QVRoge3UvQ6CMBSG4a+oq4tMIBdiSLgQLtALaWK8EH8mXVxR6tLJ5RQTiIb3mU96/tpKAAAAAADMkxsSnFd1ERb9"
        b"eqxiJMm9ssftdLgmx6cEbba70q3cXlLzdWXD+NCF9n4+XqzALOW0iYuXpCbmNJkbyKu60DKYkxjF05XWdUrawC8z"
        b"G4gT8BPU8smnPOakDYQutJq2CR9zmv7+GwUAAAAAYK7et+4yhZJhqJ0AAAAASUVORK5CYII="
    ),
)
qRegisterResourceData(0x03, *_INDICATOR_RESOURCE)


DARK_STYLESHEET = """
QWidget { background: transparent; color: #EEEFF3; font-family: "Segoe UI", "Microsoft YaHei UI"; font-size: 13px; font-weight: 400; }
QMainWindow, QDialog, QWizard, QWidget#workspace, QWidget#applicationBody, QWidget#reader { background: #101114; }
QWidget#appRail { background: #131418; border-right: 1px solid #282A33; }
QWidget#inboxPanel { background: #191A20; border-right: 1px solid #282A33; }
QWidget#detailPanel, QWidget#readerHeader, QWidget#reviewBar { background: #101114; }
QWidget#taskDrawer { background: #191A20; border: 1px solid #282A33; }
QWidget#workspaceHeader { background: #101114; border-bottom: 1px solid #282A33; }
QFrame#batchOverview { background: #131418; border-bottom: 1px solid #282A33; }
QFrame#batchOverview QLabel { font-size: 12px; }
QFrame#batchOverview QToolButton { font-size: 12px; padding: 5px 7px; }
QComboBox#candidateOrder { background: transparent; border: 0; color: #8C8E98; font-size: 12px; padding: 5px 19px 5px 5px; min-height: 16px; }
QComboBox#candidateOrder:hover { color: #EEEFF3; }
QWidget#inboxHeader { background: transparent; border-bottom: 1px solid #282A33; }
QWidget#reviewBar { border-top: 1px solid #282A33; }
QLabel { background: transparent; border: 0; }
QLabel[muted="true"], QLabel#muted, QLabel#subtleLabel, QLabel#subtitle { color: #8C8E98; font-size: 12px; }
QLabel[heading="true"], QLabel#sectionTitle { font-size: 15px; font-weight: 500; }
QLabel#pageTitle { font-size: 21px; font-weight: 500; color: #EEEFF3; }
QLabel#detailTitle { font-size: 18px; font-weight: 500; color: #EEEFF3; }
QLabel#brand { font-size: 19px; font-weight: 500; color: #131418; background: #9BAFF5; border-radius: 11px; }
QLabel#runTitle { font-size: 14px; font-weight: 500; color: #EEEFF3; }
QLabel#platformAvatar { font-size: 17px; font-weight: 500; color: #C5CCE4; background: #262934; border-radius: 11px; }
QLabel#connectionLabel { font-size: 11px; color: #777A86; }
QWidget#emptyState { background: transparent; border: 0; }
QLabel#emptyTitle { font-size: 18px; font-weight: 400; color: #BABDC9; }
QLabel#progressNote { font-size: 12px; color: #8C8E98; }
QLabel[error="true"], QLabel#errorBanner { color: #DBA2AA; font-size: 12px; }
QLabel[warning="true"] { color: #CAB48A; font-size: 12px; }
QLabel[success="true"] { color: #9EBEAE; font-size: 12px; }
QLabel:disabled { color: #595B66; }
QAbstractScrollArea, QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: 0; }
QListWidget, QListView { background: transparent; border: 0; outline: 0; padding: 0; }
QListWidget::item, QListView::item { border: 0; padding: 0; margin: 0; background: transparent; }
QListWidget::item:hover, QListView::item:hover { background: #1D1F27; }
QListWidget::item:selected, QListView::item:selected { background: #222635; color: #EEEFF3; }
QListWidget::item:selected:!active, QListView::item:selected:!active { background: #20232E; }
QTreeWidget, QTreeView, QTableWidget, QTableView { background: transparent; alternate-background-color: #16171D; border: 0; outline: 0; selection-background-color: #242938; }
QTreeWidget::item, QTableWidget::item { padding: 8px 6px; border: 0; }
QTreeWidget::item:selected, QTableWidget::item:selected { background: #242938; color: #EEEFF3; }
QHeaderView { background: transparent; border: 0; }
QHeaderView::section { background: #191A20; color: #8C8E98; padding: 9px 8px; border: 0; border-bottom: 1px solid #282A33; font-size: 12px; }
QTableCornerButton::section { background: #191A20; border: 0; }
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QDateTimeEdit, QTimeEdit { background: #17181E; color: #EEEFF3; border: 1px solid #30323D; border-radius: 5px; padding: 7px 9px; selection-background-color: #35405D; selection-color: #FFFFFF; font-size: 13px; }
QLineEdit:hover, QTextEdit:hover, QPlainTextEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover { border-color: #414451; }
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: #9BAFF5; }
QLineEdit:disabled, QTextEdit:disabled, QPlainTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: #595B66; border-color: #252730; }
QPlainTextEdit#sourceEvidence, QPlainTextEdit#sourceEvidence:read-only, QPlainTextEdit#sourceEvidence:hover, QPlainTextEdit#sourceEvidence:focus { background: transparent; color: #EEEFF3; border: 0; border-radius: 0; padding: 0; font-size: 15px; selection-background-color: #35405D; }
QPlainTextEdit#sourceEvidence QWidget { background: transparent; border: 0; }
QPlainTextEdit#reviewReason { background: #17181D; border: 1px solid #282A33; border-radius: 6px; padding: 9px 11px; font-size: 13px; }
QPlainTextEdit#reviewReason:focus { border-color: #9BAFF5; }
QWidget#inboxHeader QLineEdit { background: #131418; border: 1px solid transparent; padding: 8px 10px; }
QWidget#inboxHeader QLineEdit:focus { border-color: #9BAFF5; }
QPushButton, QToolButton { background: transparent; color: #A5A7B2; border: 1px solid transparent; border-radius: 6px; padding: 7px 10px; min-height: 18px; font-size: 13px; font-weight: 400; }
QPushButton:hover, QToolButton:hover { background: #22242C; color: #EEEFF3; }
QPushButton:pressed, QToolButton:pressed { background: #2B2E38; }
QPushButton:focus, QToolButton:focus { border-color: #9BAFF5; }
QPushButton:checked, QToolButton:checked { background: #242A3B; color: #C3CFF9; }
QPushButton:disabled, QToolButton:disabled { background: transparent; color: #51535E; border-color: transparent; }
QPushButton#primaryButton, QPushButton[primary="true"], QPushButton[variant="primary"], QPushButton[variant="positive"] { background: #9BAFF5; border-color: #9BAFF5; color: #101114; font-weight: 500; }
QPushButton#primaryButton:hover, QPushButton[primary="true"]:hover, QPushButton[variant="primary"]:hover, QPushButton[variant="positive"]:hover { background: #AFBFF8; border-color: #AFBFF8; }
QPushButton#primaryButton:focus, QPushButton[primary="true"]:focus, QPushButton[variant="primary"]:focus, QPushButton[variant="positive"]:focus { border: 2px solid #EEEFF3; padding: 6px 9px; }
QPushButton#primaryButton:disabled, QPushButton[primary="true"]:disabled, QPushButton[variant="primary"]:disabled, QPushButton[variant="positive"]:disabled { background: #343B52; border-color: #343B52; color: #7B8298; }
QPushButton[variant="danger"] { background: transparent; border-color: transparent; color: #96949C; }
QPushButton[variant="danger"]:hover { background: #2B2228; color: #DBA2AA; }
QPushButton[variant="flat"], QToolButton#collapsibleHeader { background: transparent; color: #8C8E98; border-color: transparent; }
QPushButton[variant="flat"]:hover, QToolButton#collapsibleHeader:hover { color: #EEEFF3; background: #202229; }
QToolButton#collapsibleHeader { text-align: left; padding: 6px 0; font-size: 12px; }
QToolButton#collapsibleHeader:focus { border-color: #9BAFF5; }
QWidget#appRail QToolButton, QWidget#appRail QPushButton { border-radius: 8px; padding: 8px; min-width: 20px; min-height: 20px; }
QWidget#appRail QToolButton#railButton { padding: 0; min-width: 20px; min-height: 20px; font-size: 11px; }
QWidget#appRail QToolButton:checked, QWidget#appRail QPushButton:checked { background: #252A39; color: #BDCBFB; }
QToolButton::menu-button { border: 0; width: 15px; background: transparent; }
QComboBox { background: #17181E; color: #EEEFF3; border: 1px solid #30323D; border-radius: 5px; padding: 7px 25px 7px 9px; min-height: 18px; font-size: 13px; }
QComboBox:hover { border-color: #414451; }
QComboBox:focus, QComboBox:on { border-color: #9BAFF5; }
QComboBox:disabled { color: #595B66; border-color: #252730; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 22px; border: 0; background: transparent; }
QWidget#inboxHeader QComboBox { background: transparent; border-color: transparent; color: #8C8E98; padding-left: 0; font-size: 12px; }
QWidget#inboxHeader QComboBox:focus { border-color: #9BAFF5; }
QComboBox QAbstractItemView { background: #191A20; color: #EEEFF3; border: 1px solid #30323D; padding: 5px; outline: 0; selection-background-color: #292E3E; }
QComboBox QAbstractItemView::item { min-height: 26px; padding: 5px 8px; }
QCheckBox, QRadioButton { background: transparent; spacing: 8px; color: #B9BBC5; padding: 3px 0; font-size: 13px; }
QCheckBox:hover, QRadioButton:hover { color: #EEEFF3; }
QCheckBox:disabled, QRadioButton:disabled { color: #595B66; }
QCheckBox::indicator, QRadioButton::indicator, QAbstractItemView::indicator { width: 15px; height: 15px; background: #131418; border: 1px solid #636775; border-radius: 4px; }
QCheckBox::indicator:hover, QRadioButton::indicator:hover, QAbstractItemView::indicator:hover { border-color: #ADBDF3; background: #202431; }
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked { background: #9BAFF5; border-color: #9BAFF5; image: url(:/leadworkbench/native/indicator/check.png); }
QCheckBox::indicator:indeterminate, QAbstractItemView::indicator:indeterminate { background: #9BAFF5; border-color: #9BAFF5; image: url(:/leadworkbench/native/indicator/partial.png); }
QRadioButton::indicator { border-radius: 8px; }
QRadioButton::indicator:checked { background: #131418; border-color: #9BAFF5; image: url(:/leadworkbench/native/indicator/radio.png); }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled, QAbstractItemView::indicator:disabled { background: #1A1C23; border-color: #363945; }
QCheckBox::indicator:checked:disabled, QCheckBox::indicator:indeterminate:disabled, QAbstractItemView::indicator:checked:disabled, QAbstractItemView::indicator:indeterminate:disabled { background: #555E7D; border-color: #555E7D; }
QGroupBox { background: transparent; border: 0; border-top: 1px solid #282A33; margin-top: 15px; padding: 17px 0 5px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 0; padding: 0 8px 0 0; color: #B4B6C1; background: #101114; font-size: 12px; }
QTabWidget::pane { border: 0; background: transparent; top: 0; }
QTabBar { background: transparent; }
QTabBar::tab { color: #8C8E98; background: transparent; padding: 10px 0; border: 0; border-bottom: 2px solid transparent; margin-right: 25px; font-size: 13px; }
QTabBar::tab:hover { color: #DADCE5; }
QTabBar::tab:selected { color: #EEEFF3; border-bottom-color: #9BAFF5; }
QTabBar::tab:disabled { color: #595B66; }
QMenuBar { background: #131418; color: #A5A7B2; border: 0; }
QMenuBar::item { padding: 7px 12px; background: transparent; }
QMenuBar::item:selected { background: #22242C; color: #EEEFF3; }
QMenu { background: #191A20; color: #D4D6E0; border: 1px solid #30323D; padding: 5px; }
QMenu::item { padding: 8px 25px 8px 12px; border-radius: 4px; }
QMenu::item:selected { background: #292D3A; color: #EEEFF3; }
QMenu::item:disabled { color: #595B66; }
QMenu::separator { height: 1px; background: #30323D; margin: 5px 7px; }
QStatusBar { background: #101114; color: #747782; border-top: 1px solid #282A33; font-size: 12px; }
QStatusBar::item { border: 0; }
QSplitter, QSplitter::handle { background: #282A33; }
QSplitter::handle:horizontal { width: 1px; margin: 0; }
QSplitter::handle:vertical { height: 1px; margin: 0; }
QSplitter::handle:hover { background: #9BAFF5; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 1px; border: 0; }
QScrollBar::handle:vertical { background: #3A3D49; min-height: 28px; border-radius: 3px; margin: 1px 2px; }
QScrollBar::handle:vertical:hover { background: #565B6B; }
QScrollBar:horizontal { background: transparent; height: 8px; margin: 1px; border: 0; }
QScrollBar::handle:horizontal { background: #3A3D49; min-width: 28px; border-radius: 3px; margin: 2px 1px; }
QScrollBar::handle:horizontal:hover { background: #565B6B; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; border: 0; background: transparent; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QProgressBar { border: 0; border-radius: 2px; background: #282A33; color: #A9B7E8; min-height: 4px; text-align: center; font-size: 12px; }
QProgressBar::chunk { background: #9BAFF5; border-radius: 2px; }
QToolTip { background: #202229; color: #DADCE5; border: 1px solid #3A3D49; padding: 7px 9px; font-size: 12px; }
QMessageBox, QFileDialog, QWizardPage { background: #101114; }
QMessageBox QLabel { color: #D4D6E0; }
QDialogButtonBox { background: transparent; }
QFrame[frameShape="4"], QFrame[frameShape="5"] { color: #282A33; }
"""


def apply_dark_theme(app: QApplication) -> None:
    """Apply one consistent native-widget palette, font and focus treatment."""
    app.setStyle("Fusion")
    font = QFont()
    font.setFamilies(["Segoe UI", "Microsoft YaHei UI"])
    font.setPixelSize(13)
    font.setWeight(QFont.Weight.Normal)
    app.setFont(font)
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#101114", QPalette.ColorRole.WindowText: "#EEEFF3",
        QPalette.ColorRole.Base: "#101114", QPalette.ColorRole.AlternateBase: "#191A20",
        QPalette.ColorRole.ToolTipBase: "#202229", QPalette.ColorRole.ToolTipText: "#DADCE5",
        QPalette.ColorRole.Text: "#EEEFF3", QPalette.ColorRole.Button: "#191A20",
        QPalette.ColorRole.ButtonText: "#A5A7B2", QPalette.ColorRole.BrightText: "#FFFFFF",
        QPalette.ColorRole.Link: "#9BAFF5", QPalette.ColorRole.LinkVisited: "#B9B8E3",
        QPalette.ColorRole.Highlight: "#303950", QPalette.ColorRole.HighlightedText: "#EEEFF3",
        QPalette.ColorRole.PlaceholderText: "#6C6F7C", QPalette.ColorRole.Light: "#3A3D49",
        QPalette.ColorRole.Midlight: "#30323D", QPalette.ColorRole.Mid: "#282A33",
        QPalette.ColorRole.Dark: "#131418", QPalette.ColorRole.Shadow: "#090A0D",
    }
    for role, color in colors.items():
        palette.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#595B66"))
    palette.setColor(QPalette.ColorGroup.Inactive, QPalette.ColorRole.Highlight, QColor("#282E40"))
    app.setPalette(palette)
    app.setStyleSheet(DARK_STYLESHEET)


class CollapsibleSection(QWidget):
    """A keyboard-accessible disclosure for occasional native form controls."""

    toggled = Signal(bool)

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._expanded = False
        self.header = QToolButton(self)
        self.header.setObjectName("collapsibleHeader")
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.ArrowType.RightArrow)
        self.header.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.header.setAccessibleName(title)
        self.header.setAccessibleDescription("展开或收起补充选项")
        self.content = QWidget(self)
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(16, 8, 0, 8)
        self.content_layout.setSpacing(10)
        self.content.setVisible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(self.header)
        layout.addWidget(self.content)
        self.header.toggled.connect(self.set_expanded)

    @property
    def expanded(self) -> bool:
        return self._expanded

    @Slot(bool)
    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self.header.blockSignals(True)
        try:
            self.header.setChecked(expanded)
        finally:
            self.header.blockSignals(False)
        self.header.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.content.setVisible(expanded)
        self.toggled.emit(expanded)
