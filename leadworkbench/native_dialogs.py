"""Native QtWidgets workflows. Business operations use the local HTTP API only."""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote, urlencode
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox,
    QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget, QWizard, QWizardPage,
)

from .cli import ApiClient
from .native_forms import (
    MAX_INPUT_BYTES, FormError, collection_payload, parse_records, preview_payload,
    review_payload, run_payload, settings_payload, split_lines, targeting_payload,
    valid_http_url,
)
from .native_helpers import ApiExecutor, CollapsibleSection


SOURCE_LABELS = [("社交平台搜索", "opencli"), ("指定帖子的评论", "comments"),
                 ("导入已有原文", "import"), ("公开网页链接", "urls"), ("合成演示", "demo")]
DECISION_LABELS = {"keep": "保留", "exclude": "排除", "needs_info": "待补查", "watch": "观察"}


def _label(text, muted=False):
    widget = QLabel(str(text))
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if muted:
        widget.setObjectName("muted")
        widget.setProperty("muted", True)
    return widget


def _editor(placeholder="", height=95, readonly=False):
    widget = QPlainTextEdit()
    widget.setPlaceholderText(placeholder)
    widget.setFixedHeight(height)
    widget.setReadOnly(readonly)
    return widget


def _combo(items):
    widget = QComboBox()
    for text, value in items:
        widget.addItem(text, value)
    return widget


def _set_combo(widget, value):
    index = widget.findData(value)
    if index >= 0:
        widget.setCurrentIndex(index)


def _spin(low, high, value):
    widget = QSpinBox()
    widget.setRange(low, high)
    widget.setValue(value)
    return widget


class _FormLayout(QFormLayout):
    def addRow(self, *items):
        if len(items) == 2 and isinstance(items[0], str):
            label = _label(items[0])
            label.setMinimumWidth(96)
            super().addRow(label, items[1])
        else:
            super().addRow(*items)


def _form():
    form = _FormLayout()
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    form.setHorizontalSpacing(24)
    form.setVerticalSpacing(16)
    return form


def _scroll(content):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QScrollArea.Shape.NoFrame)
    scroll.setWidget(content)
    return scroll


def _request(client, method, path, body=None, timeout=30):
    # Each worker gets its own urllib opener and rereads the instance token.
    return ApiClient(client.data_dir, timeout=timeout).request(method, path, body)


def _error_text(error):
    message = str(error)
    code = getattr(error, "code", "")
    if code in {"CONFLICT", "VERSION_CONFLICT", "REVIEW_CONFLICT"}:
        message += " 请导出当前版本重新复核；不要覆盖后续审核。"
    return message


class _WizardPage(QWizardPage):
    def __init__(self, owner, step, title, subtitle):
        super().__init__(owner)
        self.owner, self.step = owner, step
        self.setTitle(title)
        self.setSubTitle(subtitle)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setSpacing(20)
        self.body_layout.setContentsMargins(20, 16, 24, 20)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_scroll(self.body))

    def validatePage(self):
        return self.owner.validate_step(self.step)

    def isComplete(self):
        return self.owner.loaded and not self.owner._busy

    def initializePage(self):
        if self.step == 2:
            self.owner.update_summary()


class _WizardStatus:
    """Keep a readable native message in each scrollable step."""
    def __init__(self, pages):
        self.labels = []
        for page in pages:
            label = _label("正在读取本地设置和平台能力…", True)
            page.body_layout.insertWidget(0, label)
            self.labels.append(label)

    def setText(self, text):
        for label in self.labels:
            label.setText(str(text))
            label.setVisible(bool(text))


class NewRunDialog(QWizard):
    """Three steps; Accepted exposes .payload and .start_now for main window."""
    def __init__(self, client, parent=None, source="opencli", analysis_mode=None):
        super().__init__(parent)
        if analysis_mode is not None and (not isinstance(analysis_mode, str) or analysis_mode not in {"local", "collect_only"}):
            raise FormError("批次处理方式应为 local、collect_only 或 None。")
        self.client, self.payload, self.start_now = client, None, True
        self.requested_analysis_mode = analysis_mode
        self.executor = ApiExecutor(self)
        self.catalog, self.settings = [], {}
        self.loaded, self._busy, self._closed = False, False, False
        self._validated_text, self._validated_records = None, None
        self._market_drafts, self._last_market = {}, "cn"
        self.setWindowTitle("创建获客任务")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage)
        self.setOption(QWizard.WizardOption.NoCancelButtonOnLastPage, False)
        self.setButtonText(QWizard.WizardButton.NextButton, "下一步")
        self.setButtonText(QWizard.WizardButton.BackButton, "上一步")
        self.setButtonText(QWizard.WizardButton.FinishButton, "创建并运行")
        self.setButtonText(QWizard.WizardButton.CancelButton, "取消")
        self.button(QWizard.WizardButton.FinishButton).setProperty("primary", True)
        self.button(QWizard.WizardButton.FinishButton).setObjectName("primaryButton")
        for kind in (QWizard.WizardButton.NextButton, QWizard.WizardButton.FinishButton):
            button = self.button(kind)
            button.setProperty("primary", True)
            button.setObjectName("primaryButton")
            button.style().unpolish(button)
            button.style().polish(button)
        self.resize(840, 680)
        self.setMinimumSize(700, 560)
        self.target_page = _WizardPage(self, 0, "目标顾客", "1 / 3")
        self.source_page = _WizardPage(self, 1, "数据来源", "2 / 3")
        self.mode_page = _WizardPage(self, 2, "批量处理", "3 / 3")
        for page in (self.target_page, self.source_page, self.mode_page):
            self.addPage(page)
        self._build_target()
        self._build_source(source)
        self._build_processing()
        self.status = _WizardStatus((self.target_page, self.source_page, self.mode_page))
        self.currentIdChanged.connect(lambda _: QTimer.singleShot(0, self._style_heading))
        self.finished.connect(self._finish)
        self._refresh_navigation()
        QTimer.singleShot(0, self._load)

    def showEvent(self, event):
        super().showEvent(event)
        screen = self.screen()
        if screen is not None:
            available = screen.availableGeometry()
            frame_margin = max(32, self.frameGeometry().height() - self.height())
            self.resize(min(self.width(), max(700, available.width() - 32)),
                        min(self.height(), max(560, available.height() - frame_margin - 24)))

    def _style_heading(self):
        page = self.currentPage()
        if self._closed or page is None:
            return
        for label in self.findChildren(QLabel):
            if label.text() == page.title():
                label.setObjectName("pageTitle")
                label.style().unpolish(label)
                label.style().polish(label)

    def _finish(self, _):
        self._closed = True
        self.executor.shutdown(wait=False)

    def _load(self):
        self._async("正在读取设置…", lambda: _request(self.client, "GET", "/api/bootstrap"), self._loaded)

    def _loaded(self, result):
        self.settings = result.get("settings", {})
        self.catalog = result.get("capabilities", {}).get("platforms", [])
        self.loaded = True
        self.retry.setVisible(False)
        capability_maximum = int(result.get("capabilities", {}).get("max_batch_candidates", 50))
        maximum = max(1, min(capability_maximum, int(self.settings.get("limits", {}).get("max_candidates", 50))))
        self.limit.setMaximum(maximum)
        self.limit.setValue(min(20, maximum))
        self.limit.setToolTip(f"采集来源记录的数量预算（帖子、网页或评论），不是 AI 初筛后保留的数量。当前有效上限 {maximum} 条；实际数量可能因来源不足、去重或读取限制而更少。")
        _set_combo(self.analysis_mode, "local" if self.settings.get("model", {}).get("enabled") else "collect_only")
        if self.requested_analysis_mode is not None:
            _set_combo(self.analysis_mode, self.requested_analysis_mode)
        self._render_platforms()
        self._source_changed()
        self.update_mode_hint()
        self.status.setText("")
        self._refresh_navigation()

    def _async(self, message, fn, success):
        if self._busy or self._closed:
            return
        self._busy = True
        self.status.setText(message)
        self._refresh_navigation()
        def done(result):
            if self._closed:
                return
            self._busy = False
            self._refresh_navigation()
            self.status.setText("")
            success(result)
        def failed(error):
            if self._closed:
                return
            self._busy = False
            self._refresh_navigation()
            self.status.setText(_error_text(error))
            if not self.loaded:
                self.retry.setVisible(True)
        self.executor.submit(fn, done, failed)

    def _refresh_navigation(self):
        for page in (self.target_page, self.source_page, self.mode_page):
            page.completeChanged.emit()
        for kind in (QWizard.WizardButton.NextButton, QWizard.WizardButton.FinishButton):
            button = self.button(kind)
            if button:
                button.setEnabled(self.loaded and not self._busy)
        for attr in ("preview_button", "validate_button", "retry"):
            button = getattr(self, attr, None)
            if button:
                button.setEnabled(not self._busy)

    def _build_target(self):
        layout = self.target_page.body_layout
        basic = _form()
        self.name = QLineEdit()
        self.name.setMaxLength(160)
        self.name.setPlaceholderText("例如：苏州家装业主")
        basic.addRow("任务名称", self.name)
        self.market = _combo([("国内", "cn"), ("海外", "global")])
        self.customer = _combo([("个人消费者 / 业主", "consumer"), ("商家 / 企业", "business"), ("类型不限", "any")])
        market_row = QWidget()
        h = QHBoxLayout(market_row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.market)
        h.addWidget(self.customer, 1)
        basic.addRow("市场与客群", market_row)
        self.industry = QLineEdit("家装")
        self.industry.setMaxLength(100)
        basic.addRow("行业", self.industry)
        self.regions = QLineEdit()
        self.regions.setPlaceholderText("例如：苏州，昆山；最多 10 个地区")
        self.regions.setToolTip("地区用于搜索词定向；是否属于目标地区仍需核对来源原文。最多 10 个地区。")
        basic.addRow("服务地区", self.regions)
        layout.addLayout(basic)
        self.include_tags = QLineEdit("准备装修，旧房改造，找施工队")
        self.include_tags.setPlaceholderText("用逗号分隔，最多 20 个")
        self.exclude_tags = QLineEdit("同行广告，招商，招聘")
        tags = _form()
        self.include_tags.setToolTip("需求信号用于搜索和审核，并不证明来源作者已满足条件。最多 20 个，以逗号分隔。")
        self.exclude_tags.setToolTip("按作者身份和语境排除；求推荐装修公司不会因出现“装修公司”而直接排除。")
        tags.addRow("需求信号", self.include_tags)
        tags.addRow("排除类型", self.exclude_tags)
        layout.addLayout(tags)
        self.needs = _editor("补充顾客特征、购买阶段或你能提供的服务。", 85)
        self.needs.setPlainText("寻找有明确装修计划、正在询价或求推荐施工团队的业主。")
        layout.addWidget(_label("补充需求"))
        layout.addWidget(self.needs)
        advanced = CollapsibleSection("更多条件")
        self.custom_table = QTableWidget(0, 2)
        self.custom_table.setHorizontalHeaderLabels(["条件名称（最多 50 字）", "条件内容（最多 300 字）"])
        self.custom_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.custom_table.verticalHeader().setVisible(False)
        self.custom_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.custom_table.setMinimumHeight(135)
        advanced.content_layout.addWidget(self.custom_table)
        buttons = QHBoxLayout()
        add = QPushButton("添加条件")
        remove = QPushButton("移除选中条件")
        add.clicked.connect(self._add_condition)
        remove.clicked.connect(self._remove_condition)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch()
        advanced.content_layout.addLayout(buttons)
        self.goal = _editor("本轮目标；留空时根据以上条件生成描述。", 72)
        advanced.content_layout.addWidget(_label("本轮目标"))
        advanced.content_layout.addWidget(self.goal)
        layout.addWidget(advanced)
        preset = QPushButton("家装示例")
        preset.setToolTip("填入家装业主的示例条件；请自行补充实际服务地区。")
        preset.clicked.connect(self._home_preset)
        layout.addWidget(preset, alignment=Qt.AlignmentFlag.AlignLeft)
        self.retry = QPushButton("重新连接本地程序")
        self.retry.setVisible(False)
        self.retry.clicked.connect(self._load)
        layout.addWidget(self.retry)
        layout.addStretch()
        self.market.currentIndexChanged.connect(self._market_changed)

    def _add_condition(self):
        if self.custom_table.rowCount() >= 12:
            self.status.setText("一轮最多添加 12 项补充条件。")
            return
        row = self.custom_table.rowCount()
        self.custom_table.insertRow(row)
        self.custom_table.setItem(row, 0, QTableWidgetItem(""))
        self.custom_table.setItem(row, 1, QTableWidgetItem(""))
        self.custom_table.setCurrentCell(row, 0)
        self.custom_table.editItem(self.custom_table.item(row, 0))

    def _remove_condition(self):
        rows = sorted({index.row() for index in self.custom_table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.custom_table.removeRow(row)

    def _home_preset(self):
        _set_combo(self.market, "cn")
        _set_combo(self.customer, "consumer")
        self.industry.setText("家装")
        self.include_tags.setText("准备装修，旧房改造，找施工队")
        self.exclude_tags.setText("同行广告，招商，招聘")
        self.needs.setPlainText("寻找有明确装修计划、正在询价或求推荐施工团队的业主。")
        self.status.setText("已填入家装示例，请补充你实际服务的地区。")

    def _target_draft(self):
        return {"industry": self.industry.text(), "customer_type": self.customer.currentData(), "regions": self.regions.text(),
                "include_tags": self.include_tags.text(), "exclude_tags": self.exclude_tags.text(), "needs": self.needs.toPlainText(),
                "custom_fields": self._custom_rows(), "goal": self.goal.toPlainText()}

    def _market_changed(self):
        new = self.market.currentData()
        self._market_drafts[self._last_market] = self._target_draft()
        self._market_drafts[self._last_market]["queries"] = self.queries.toPlainText()
        self._market_drafts[self._last_market]["platforms"] = [key for key, check in self.platform_checks.items() if check.isChecked()]
        self._last_market = new
        draft = self._market_drafts.get(new, {"industry": "", "customer_type": "business", "regions": "", "include_tags": "", "exclude_tags": "", "needs": "", "custom_fields": [], "goal": ""})
        for field in ("industry", "regions", "include_tags", "exclude_tags"):
            getattr(self, field).setText(draft[field])
        _set_combo(self.customer, draft["customer_type"])
        self.needs.setPlainText(draft["needs"])
        self.goal.setPlainText(draft["goal"])
        self.custom_table.setRowCount(0)
        for item in draft["custom_fields"]:
            row = self.custom_table.rowCount()
            self.custom_table.insertRow(row)
            self.custom_table.setItem(row, 0, QTableWidgetItem(item["key"]))
            self.custom_table.setItem(row, 1, QTableWidgetItem(item["value"]))
        self.queries.setPlainText(draft.get("queries", ""))
        self._render_platforms(draft.get("platforms", []))

    def _build_source(self, source):
        layout = self.source_page.body_layout
        self.source = _combo(SOURCE_LABELS)
        _set_combo(self.source, source)
        if source == "demo":
            self.name.setText("演示批次 · 需求证据复核")
        layout.addWidget(self.source)
        self.source_hint = _label("", True)
        layout.addWidget(self.source_hint)
        self.source_stack = QStackedWidget()
        layout.addWidget(self.source_stack)
        self.search_panel = QWidget()
        search = QVBoxLayout(self.search_panel)
        search.setContentsMargins(0, 0, 0, 0)
        self.platform_box = QGroupBox("平台")
        self.platform_layout = QGridLayout(self.platform_box)
        self.platform_layout.setHorizontalSpacing(24)
        self.platform_checks = {}
        search.addWidget(self.platform_box)
        self.queries = _editor("每行一个搜索词", 120)
        self.queries.setToolTip("根据目标条件生成确定性的搜索词，也可直接编辑。生成过程不访问平台、不调用模型。")
        query_header = QHBoxLayout()
        query_header.addWidget(_label("搜索词"))
        query_header.addStretch()
        self.preview_button = QPushButton("生成搜索词")
        self.preview_button.setToolTip("按第一步的条件生成搜索方案，不访问平台或模型。")
        self.preview_button.clicked.connect(lambda: self.generate_preview(True))
        query_header.addWidget(self.preview_button)
        search.addLayout(query_header)
        search.addWidget(self.queries)
        self.preview_details = CollapsibleSection("搜索方案")
        self.preview_result = _editor("生成后可查看搜索范围与限制。", 130, True)
        self.preview_details.content_layout.addWidget(self.preview_result)
        search.addWidget(self.preview_details)
        self.source_stack.addWidget(self.search_panel)
        self.links_panel = QWidget()
        links = QVBoxLayout(self.links_panel)
        links.setContentsMargins(0, 0, 0, 0)
        self.urls = _editor("每行一个完整的 https:// 链接", 160)
        links.addWidget(self.urls)
        self.link_help = _label("", True)
        links.addWidget(self.link_help)
        self.source_stack.addWidget(self.links_panel)
        self.import_panel = QWidget()
        imports = QVBoxLayout(self.import_panel)
        imports.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        choose = QPushButton("选择文件")
        choose.setToolTip("支持 UTF-8 编码的 JSON、CSV、TSV 和 JSONL，最多 3.5 MB。")
        choose.clicked.connect(self.choose_import_file)
        self.validate_button = QPushButton("校验原文")
        self.validate_button.clicked.connect(self.validate_import)
        row.addWidget(choose)
        row.addWidget(self.validate_button)
        row.addStretch()
        imports.addLayout(row)
        self.records = _editor('JSON 示例：[{"source_url":"https://…","content":"来源原文","platform":"import"}]', 190)
        imports.addWidget(self.records)
        self.import_result = _editor("校验后显示可导入条数。", 68, True)
        self.import_result.setToolTip("校验会保留来源链接、原文和部分采集标记。没有正文的资料不会伪造为需求。")
        imports.addWidget(self.import_result)
        self.source_stack.addWidget(self.import_panel)
        demo = _label("使用合成样本体验采集与审核。")
        demo.setToolTip("演示是虚构样本，不是真实客户；不访问平台，也不调用本地模型。")
        self.source_stack.addWidget(demo)
        self.collection_group = QGroupBox("采集范围")
        c = QVBoxLayout(self.collection_group)
        self.scope = _combo([("帖子", "posts"), ("评论", "comments"), ("帖子与评论", "posts_and_comments")])
        c.addWidget(self.scope)
        self.comment_fields = QWidget()
        f = _form()
        self.comment_fields.setLayout(f)
        self.comment_keywords = QLineEdit()
        self.comment_keywords.setPlaceholderText("例如：怎么联系，报价，求推荐；留空保留所读评论")
        f.addRow("评论关键词", self.comment_keywords)
        self.comment_match = _combo([("包含任一关键词", "any"), ("包含全部关键词", "all")])
        f.addRow("匹配方式", self.comment_match)
        self.max_posts = _spin(1, 20, 3)
        self.comments_per_post = _spin(1, 100, 20)
        caps = QWidget()
        row = QHBoxLayout(caps)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(_label("最多帖子"))
        row.addWidget(self.max_posts)
        row.addWidget(_label("每帖最多评论"))
        row.addWidget(self.comments_per_post)
        f.addRow("样本预算", caps)
        self.with_replies = QCheckBox("包含回复")
        self.with_replies.setToolTip("只读取平台支持的有界回复样本；完整性以实际采集结果为准。")
        f.addRow("", self.with_replies)
        c.addWidget(self.comment_fields)
        self.comment_hint = _label("", True)
        c.addWidget(self.comment_hint)
        layout.addWidget(self.collection_group)
        layout.addStretch()
        self.source.currentIndexChanged.connect(self._source_changed)
        self.scope.currentIndexChanged.connect(self._scope_changed)

    def _render_platforms(self, selected=None):
        if not hasattr(self, "platform_layout"):
            return
        selected = set(selected) if selected is not None else {key for key, check in self.platform_checks.items() if check.isChecked()}
        eligible = {item["id"] for item in self.catalog if item.get("market") == self.market.currentData() and item.get("search_supported") and (not self._comments_active() or item.get("comments_supported"))}
        selected &= eligible
        if not selected:
            selected = {"xiaohongshu", "weibo"} if self._comments_active() else ({"douyin", "xiaohongshu"} if self.market.currentData() == "cn" else {"tiktok"})
        while self.platform_layout.count():
            item = self.platform_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.platform_checks = {}
        position = 0
        for item in self.catalog:
            if item.get("market") != self.market.currentData():
                continue
            available = item.get("search_supported") and (not self._comments_active() or item.get("comments_supported"))
            suffix = " · 实验性" if item.get("experimental") else ""
            check = QCheckBox(item.get("label", item["id"]) + suffix)
            check.setEnabled(bool(available))
            check.setChecked(bool(available and item["id"] in selected))
            limitation = item.get("comments_limitations") if self._comments_active() else item.get("limitations")
            check.setToolTip(str(limitation or "需要 Browser Bridge 和相关账号登录。"))
            self.platform_layout.addWidget(check, position // 2, position % 2)
            position += 1
            self.platform_checks[item["id"]] = check

    def _comments_active(self):
        return hasattr(self, "source") and (self.source.currentData() == "comments" or
            (self.source.currentData() == "opencli" and hasattr(self, "scope") and self.scope.currentData() != "posts"))

    def _source_changed(self):
        source = self.source.currentData()
        self.source_stack.setCurrentIndex({"opencli": 0, "urls": 1, "comments": 1, "import": 2, "demo": 3}[source])
        self.collection_group.setVisible(source in {"opencli", "comments"})
        self.scope.setVisible(source == "opencli")
        if source == "comments":
            _set_combo(self.scope, "comments")
        self.link_help.setText("知乎使用回答链接，小红书保留完整 xsec_token。" if source == "comments" else "每行一个公开网页链接。")
        self.link_help.setToolTip("评论是指定帖子下的有界样本，不是全站索引。" if source == "comments" else "登录、验证码和纯动态页面可能导致读取失败或部分内容。")
        self.source_hint.setText({"opencli": "需连接浏览器并登录相应平台。",
            "comments": "读取指定帖子下的评论样本。",
            "import": "JSON、CSV 或 JSONL · 最多 3.5 MB",
            "urls": "读取公开网页原文。", "demo": "合成演示 · 非真实客户"}[source])
        self.source_hint.setToolTip({"opencli": "需要安装 OpenCLI、连接 Browser Bridge 并登录相应账号。适配器尚未完成真实账号端到端验收；所有采集均为只读。",
            "comments": "关键词在读取后逐条匹配，不是全站评论索引。评论者与父帖作者分别保留。",
            "import": "支持 UTF-8 文件，最多 1000 条；原文仅作为数据展示。",
            "urls": "动态社交平台内容可改用平台搜索或原文导入。", "demo": "虚构样本，不访问平台或模型。"}[source])
        self.processing_group.setVisible(source != "demo")
        for widget in (self.market, self.customer, self.industry, self.regions, self.include_tags,
                       self.exclude_tags, self.needs, self.custom_table, self.goal):
            widget.setEnabled(source != "demo")
        self._scope_changed()

    def _scope_changed(self):
        self.comment_fields.setVisible(self._comments_active())
        if hasattr(self, "comment_hint"):
            supported = [p.get("label", p["id"]) + ("回答" if p["id"] == "zhihu" else "") for p in self.catalog if p.get("comments_supported")]
            self.comment_hint.setVisible(self._comments_active())
            self.comment_hint.setText("支持：" + "、".join(supported))
            self.comment_hint.setToolTip("评论为所选帖子下的有界采样，回复支持和上限以实际结果为准。零关键词命中只描述本次样本。")
        self._render_platforms()

    def _build_processing(self):
        layout = self.mode_page.body_layout
        self.summary = _editor("", 104, True)
        self.summary.setObjectName("taskSummary")
        layout.addWidget(self.summary)
        self.processing_group = QGroupBox("整批处理")
        f = QVBoxLayout(self.processing_group)
        self.analysis_mode = _combo([("只批量采集，稍后审核", "collect_only"), ("本地 AI 先初筛，再交给人工 / Codex", "local")])
        f.addWidget(self.analysis_mode)
        self.mode_hint = _label("", True)
        f.addWidget(self.mode_hint)
        layout.addWidget(self.processing_group)
        form = _form()
        self.limit = _spin(1, 50, 20)
        self.limit.setToolTip("采集来源记录的数量预算，不是 AI 初筛后保留的数量。")
        form.addRow("本批最多采集", self.limit)
        layout.addLayout(form)
        self.run_policy = _label("单次运行：达到上限或本轮来源读完，就结束采集。", True)
        self.run_policy.setToolTip("默认20条，当前总上限以服务能力为准，所有平台共享。结果不足时不会一直凑数，也不会自动定时重跑；之后完成可选的AI初筛。暂停会等待当前网络或推理步骤结束。")
        layout.addWidget(self.run_policy)
        self.start_check = QCheckBox("创建后立即运行")
        self.start_check.setChecked(True)
        self.start_check.toggled.connect(lambda checked: self.setButtonText(QWizard.WizardButton.FinishButton, "创建并运行" if checked else "创建任务"))
        layout.addWidget(self.start_check)
        self.analysis_mode.setToolTip("程序只连接已启动的本地服务，不启停控制器，也不会回退到云端模型。设置可改变新任务默认，每批独立保存选择。")
        layout.addStretch()
        self.analysis_mode.currentIndexChanged.connect(self.update_mode_hint)
        self.analysis_mode.currentIndexChanged.connect(self.update_summary)
        self.limit.valueChanged.connect(self.update_summary)

    def update_mode_hint(self):
        base = self.settings.get("model", {}).get("base_url", "http://127.0.0.1:8080/v1")
        self.mode_hint.setText("整批自动初筛，无需逐条点 AI。" if self.analysis_mode.currentData() == "local" else "整批保存原文，稍后交给人工或 Codex。")
        self.mode_hint.setToolTip(f"连接 {base}。请使用现有模型控制器自行启动服务；一条任务会自动处理整批。" if self.analysis_mode.currentData() == "local" else "本轮不会调用模型规划或分析。首次默认只采集；设置可改变新任务默认，本轮选择独立保存。")

    def _custom_rows(self):
        return [{"key": self.custom_table.item(row, 0).text() if self.custom_table.item(row, 0) else "",
                 "value": self.custom_table.item(row, 1).text() if self.custom_table.item(row, 1) else ""} for row in range(self.custom_table.rowCount())]

    def values(self):
        return {"name": self.name.text(), "source": self.source.currentData(), "market": self.market.currentData(),
                **self._target_draft(), "queries": self.queries.toPlainText(),
                "platforms": [key for key, check in self.platform_checks.items() if check.isEnabled() and check.isChecked()],
                "urls": self.urls.toPlainText(), "records_text": self.records.toPlainText(),
                "scope": "comments" if self.source.currentData() == "comments" else self.scope.currentData(),
                "keywords": self.comment_keywords.text(), "match_mode": self.comment_match.currentData(),
                "max_posts": self.max_posts.value(), "max_comments_per_post": self.comments_per_post.value(),
                "include_replies": self.with_replies.isChecked(), "analysis_mode": self.analysis_mode.currentData(), "limit": self.limit.value()}

    def choose_import_file(self):
        filename, _ = QFileDialog.getOpenFileName(self, "导入来源原文", "", "原文文件 (*.json *.jsonl *.ndjson *.csv *.tsv);;所有文件 (*)")
        if not filename:
            return
        snapshot = self.records.toPlainText()
        def read_file():
            path = Path(filename)
            if path.stat().st_size > MAX_INPUT_BYTES:
                raise FormError("文件超过 3.5 MB，请拆成多个批次。")
            return path.read_text(encoding="utf-8-sig")
        def loaded(text):
            if snapshot != self.records.toPlainText():
                self.status.setText("读取文件期间内容已修改，未覆盖编辑；请重新选择文件。")
                return
            self.records.setPlainText(text)
            self.import_result.setPlainText("已读取文件，请校验原文。")
        self._async("正在读取导入文件…", read_file, loaded)

    def validate_import(self, advance=False):
        text = self.records.toPlainText()
        def validated(result):
            if text != self.records.toPlainText():
                self.status.setText("导入内容已变化，请重新校验。")
                return
            normalized = result if isinstance(result, list) else result.get("records", result.get("normalized_records", []))
            warnings = [] if isinstance(result, list) else result.get("warnings", [])
            self._validated_text, self._validated_records = text, normalized
            self.import_result.setPlainText(f"通过校验：{len(normalized)} 条。\n" + "\n".join(str(w) for w in warnings))
            if not normalized:
                self.status.setText("没有通过校验的原文，请检查来源链接和正文。")
            elif advance:
                self.next()
        self._async("正在解析并通过本地 API 校验原文…", lambda: _request(self.client, "POST", "/api/import/validate", {"records": parse_records(text)}), validated)

    def generate_preview(self, replace=True, advance=False):
        try:
            body = preview_payload(self.values())
            if not body["platforms"]:
                raise FormError("请至少选择一个支持当前范围的平台。")
            if replace:
                body["queries"] = []
            snapshot = preview_payload(self.values())
        except ValueError as error:
            self.status.setText(str(error))
            return
        def previewed(result):
            if snapshot != preview_payload(self.values()):
                self.status.setText("目标或搜索词已变化，请按当前条件重新生成。")
                return
            queries = result.get("queries", [])
            if not queries:
                self.status.setText("未生成搜索词，请补充行业、需求或地区。")
                return
            if replace or not self.queries.toPlainText().strip():
                self.queries.setPlainText("\n".join(queries))
            lines = [str(result.get("summary", "")), "", *queries]
            lines += ["", *[str(item) for item in result.get("warnings", [])]]
            links = result.get("manual_links", [])
            if links:
                lines += ["", "可复制到浏览器人工查找的公开搜索页："]
                lines += [str(item.get("label", item.get("platform", ""))) + " " + str(item.get("url", "")) for item in links]
            self.preview_result.setPlainText("\n".join(lines))
            if advance:
                self.next()
        self._async("正在生成确定性的搜索方案，不访问平台或模型…", lambda: _request(self.client, "POST", "/api/search/preview", body), previewed)

    def update_summary(self):
        value = self.values()
        names = dict((p["id"], p.get("label", p["id"])) for p in self.catalog)
        lines = [value["name"] or "未命名任务"]
        if value["source"] != "demo":
            lines += [" / ".join(filter(None, ["国内" if value["market"] == "cn" else "海外", value["industry"], value["regions"]]))]
        source_line = dict((v, k) for k, v in SOURCE_LABELS).get(value["source"], "")
        if value["source"] == "opencli":
            source_line += " · " + "、".join(names.get(p, p) for p in value["platforms"])
        lines += [source_line]
        if value["source"] in {"comments", "opencli"}:
            scope = {"posts": "帖子", "comments": "评论样本", "posts_and_comments": "帖子与评论样本"}[value["scope"]]
            source_line += " · " + scope
            lines[-1] = source_line
        if value["source"] == "demo":
            lines += [f"最多 {value['limit']} 条合成样本 → 体验审核"]
        elif value["analysis_mode"] == "local":
            lines += [f"批量采集最多 {value['limit']} 条 → 本地 AI 初筛 → 人工 / Codex"]
        else:
            lines += [f"批量采集最多 {value['limit']} 条 → 保存原文 → 稍后审核"]
        self.summary.setPlainText("\n".join(lines))

    def validate_step(self, step):
        if self._busy or not self.loaded:
            return False
        try:
            values = self.values()
            if step == 0:
                if not values["name"].strip():
                    self.name.setFocus()
                    raise FormError("请先为任务起个名字。")
                if values["source"] != "demo":
                    targeting_payload(values)
            elif step == 1:
                source = values["source"]
                if source == "import":
                    if self._validated_text != values["records_text"] or not self._validated_records:
                        self.validate_import(advance=True)
                        return False
                if source in {"urls", "comments"}:
                    urls = split_lines(values["urls"])
                    if not urls or any(not valid_http_url(url) for url in urls):
                        raise FormError("请每行填写一个完整的 http:// 或 https:// 来源链接。")
                if source in {"comments", "opencli"}:
                    collection_payload(values, source)
                if source == "opencli":
                    if not values["platforms"]:
                        raise FormError("请至少选择一个支持当前范围的平台。")
                    if not values["queries"].strip():
                        self.generate_preview(False, advance=True)
                        return False
            else:
                self.payload = run_payload(values, self.catalog, self.limit.maximum(), self._validated_records if self._validated_text == values["records_text"] else None)
                self.start_now = self.start_check.isChecked()
            self.status.setText("")
            return True
        except (ValueError, TypeError) as error:
            self.status.setText(str(error))
            return False


class SettingsDialog(QDialog):
    saved = Signal(object)

    def __init__(self, client, parent=None):
        super().__init__(parent)
        self.client, self.executor = client, ApiExecutor(self)
        self._closed, self._busy, self._loaded_ok = False, False, False
        self.setWindowTitle("设置")
        self.resize(760, 680)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(26, 24, 26, 22)
        outer.setSpacing(18)
        title = _label("设置")
        title.setObjectName("pageTitle")
        outer.addWidget(title)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 4, 8, 8)
        layout.setSpacing(18)
        section = _label("本地模型")
        section.setObjectName("sectionTitle")
        section.setToolTip("连接你已启动的服务。保存不会启停模型，也不会使用云端备用模型。")
        layout.addWidget(section)
        form = _form()
        self.protocol = _combo([("OpenAI 兼容接口", "openai"), ("Ollama", "ollama")])
        self.base_url = QLineEdit("http://127.0.0.1:8080/v1")
        self.model = QLineEdit("local-model")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.clear_key = QCheckBox("清除已保存的密钥")
        self.enabled = QCheckBox("新任务默认使用本地初筛")
        self.enabled.setToolTip("仅决定新任务的默认处理方式，每批仍可改为只采集；不会启动模型。")
        for label, widget in (("协议", self.protocol), ("服务地址", self.base_url), ("模型 / alias", self.model), ("API 密钥", self.api_key)):
            form.addRow(label, widget)
        form.addRow("", self.clear_key)
        form.addRow("", self.enabled)
        layout.addLayout(form)
        params = CollapsibleSection("模型参数")
        p = _form()
        self.timeout_seconds = _spin(5, 600, 180)
        self.max_tokens = _spin(128, 16384, 2048)
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0, 1)
        self.temperature.setSingleStep(.05)
        self.temperature.setValue(.1)
        self.extra_body = _editor("{}", 85)
        self.extra_body.setPlainText("{}")
        for label, widget in (("请求超时（秒）", self.timeout_seconds), ("最大输出 token", self.max_tokens), ("Temperature", self.temperature), ("附加参数 JSON", self.extra_body)):
            p.addRow(label, widget)
        p.addRow("并发", _label("1 · 本地串行执行"))
        params.content_layout.addLayout(p)
        layout.addWidget(params)
        tools = CollapsibleSection("平台连接")
        f = _form()
        tools.content_layout.addLayout(f)
        self.executable = QLineEdit("opencli")
        self.node_entry = QLineEdit()
        self.profile = QLineEdit()
        self.opencli_timeout = _spin(5, 600, 90)
        self.executable.setToolTip("OpenCLI 可执行文件或 Node 程序路径。使用 Node 时请填写脚本入口。")
        self.node_entry.setPlaceholderText("可留空")
        self.profile.setPlaceholderText("可留空")
        self.profile.setToolTip("OpenCLI 的浏览器 profile；不读取或导出浏览器凭证。")
        f.addRow("程序路径", self.executable)
        f.addRow("Node 入口", self.node_entry)
        f.addRow("浏览器 profile", self.profile)
        f.addRow("读取超时（秒）", self.opencli_timeout)
        layout.addWidget(tools)
        tools.setToolTip("采集前请连接 OpenCLI Browser Bridge 并完成相应平台登录。")
        layout.addStretch()
        outer.addWidget(_scroll(content), 1)
        self.result = _editor("正在读取设置…", 64, True)
        self.result.setObjectName("statusOutput")
        outer.addWidget(self.result)
        self.detected_models = _combo([("测试后可选择接口返回的模型", "")])
        self.detected_models.currentIndexChanged.connect(lambda: self.model.setText(self.detected_models.currentData()) if self.detected_models.currentData() else None)
        self.detected_models.setVisible(False)
        outer.addWidget(self.detected_models)
        buttons = QHBoxLayout()
        self.prefill_button = QPushButton("本地服务示例")
        self.prefill_button.setToolTip("填入通用本机服务示例；模型名称请以服务返回的实际 ID 为准，不会保存或启动模型。")
        self.prefill_button.clicked.connect(self._prefill)
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._test)
        self.save_button = QPushButton("保存设置")
        self.save_button.setProperty("primary", True)
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self._save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        for button in (self.prefill_button, self.test_button):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        outer.addLayout(buttons)
        self.finished.connect(self._finish)
        self._set_busy(True)
        QTimer.singleShot(0, self._load)

    def _finish(self, _):
        self._closed = True
        self.executor.shutdown(wait=False)

    def _set_busy(self, busy):
        self._busy = busy
        for button in (self.test_button, self.save_button, self.prefill_button):
            button.setEnabled(not busy and self._loaded_ok)

    def _async(self, fn, callback):
        if self._closed:
            return
        self._set_busy(True)
        def done(result):
            if not self._closed:
                self._set_busy(False)
                callback(result)
        def failed(error):
            if not self._closed:
                self._set_busy(False)
                self.result.setPlainText(_error_text(error))
        self.executor.submit(fn, done, failed)

    def _load(self):
        self._async(lambda: _request(self.client, "GET", "/api/settings"), self._loaded)

    def _loaded(self, settings):
        self._loaded_ok = True
        model, cli = settings.get("model", {}), settings.get("opencli", {})
        _set_combo(self.protocol, model.get("protocol", "openai"))
        for key in ("base_url", "model"):
            getattr(self, key).setText(str(model.get(key, "")))
        for key in ("timeout_seconds", "max_tokens", "temperature"):
            getattr(self, key).setValue(model.get(key, getattr(self, key).value()))
        self.enabled.setChecked(bool(model.get("enabled")))
        self.api_key.setPlaceholderText("已保存密钥；留空保留" if model.get("api_key_configured") else "服务未设密钥时可留空")
        self.extra_body.setPlainText(json.dumps(model.get("extra_body", {}), ensure_ascii=False, indent=2))
        for key in ("executable", "node_entry", "profile"):
            getattr(self, key).setText(str(cli.get(key, "opencli" if key == "executable" else "")))
        self.opencli_timeout.setValue(int(cli.get("timeout_seconds", 90)))
        self.result.setPlainText("配置已载入")
        self._set_busy(False)

    def values(self):
        return {"protocol": self.protocol.currentData(), "base_url": self.base_url.text(), "model": self.model.text(),
                "api_key": self.api_key.text(), "clear_key": self.clear_key.isChecked(), "enabled": self.enabled.isChecked(),
                "timeout_seconds": self.timeout_seconds.value(), "max_tokens": self.max_tokens.value(),
                "temperature": self.temperature.value(), "extra_body": self.extra_body.toPlainText(),
                "executable": self.executable.text(), "node_entry": self.node_entry.text(), "profile": self.profile.text(),
                "opencli_timeout": self.opencli_timeout.value()}

    def _prefill(self):
        _set_combo(self.protocol, "openai")
        self.base_url.setText("http://127.0.0.1:8080/v1")
        self.model.setText("local-model")
        self.result.setPlainText("已填入本地服务示例，请设置实际模型 ID；尚未保存。")

    def _test(self):
        if self._busy:
            return
        try:
            body = settings_payload(self.values())["model"]
        except ValueError as error:
            self.result.setPlainText(str(error))
            return
        self.result.setPlainText("正在请求本地模型接口，请保持服务运行；界面仍可操作。")
        def tested(result):
            lines = ["接口可达" if result.get("reachable") else "连接未通过"]
            for key, title in (("model", "模型"), ("latency_ms", "耗时 ms"), ("error", "错误"), ("health", "状态")):
                if key in result:
                    lines.append(title + "：" + (json.dumps(result[key], ensure_ascii=False) if isinstance(result[key], (dict, list)) else str(result[key])))
            self.result.setPlainText("\n".join(lines))
            self.detected_models.clear()
            self.detected_models.addItem("选择接口返回的模型…", "")
            for item in result.get("models", []):
                name = item if isinstance(item, str) else item.get("id", item.get("name", "")) if isinstance(item, dict) else ""
                if name:
                    self.detected_models.addItem(name, name)
            self.detected_models.setVisible(self.detected_models.count() > 1)
        self._async(lambda: _request(self.client, "POST", "/api/model/test", body, timeout=300), tested)

    def _save(self):
        if self._busy:
            return
        try:
            payload = settings_payload(self.values())
        except ValueError as error:
            self.result.setPlainText(str(error))
            return
        self.result.setPlainText("正在保存设置…")
        def saved(result):
            self.saved.emit(result)
            self.accept()
        self._async(lambda: _request(self.client, "PUT", "/api/settings", payload), saved)


class HistoryDialog(QDialog):
    changed = Signal()

    def __init__(self, client, run_id, parent=None):
        super().__init__(parent)
        self.client, self.run_id = client, run_id
        self.executor, self._closed, self._busy = ApiExecutor(self), False, False
        self._operations = {}
        self.setWindowTitle("审核历史")
        self.resize(880, 610)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        title = _label("审核历史")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        hint = _label("每次审核均可追溯。", True)
        hint.setToolTip("撤销会恢复审核前的意见并保留原文证据；如存在后续审核，程序会拒绝整批撤销。")
        layout.addWidget(hint)
        split = QSplitter()
        self.history = QListWidget()
        self.history.setMinimumWidth(290)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        split.addWidget(self.history)
        split.addWidget(self.detail)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.status = _label("正在读取此批审核历史…", True)
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.load)
        self.revert_button = QPushButton("撤销选中审核")
        self.revert_button.clicked.connect(self.revert)
        close = QPushButton("关闭")
        close.clicked.connect(self.reject)
        row.addWidget(self.refresh_button)
        row.addStretch()
        row.addWidget(self.revert_button)
        row.addWidget(close)
        layout.addLayout(row)
        self.history.currentItemChanged.connect(self._selected)
        self.finished.connect(self._finish)
        self._selected()
        QTimer.singleShot(0, self.load)

    def _finish(self, _):
        self._closed = True
        self.executor.shutdown(wait=False)

    def _set_busy(self, busy):
        self._busy = busy
        self.refresh_button.setEnabled(not busy)
        self._selected()

    def _selected(self, *_):
        item = self.history.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item else None
        self.revert_button.setEnabled(bool(not self._busy and data and data.get("status") == "applied"))
        if data:
            self.detail.setPlainText(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            self.detail.setPlainText("尚无审核历史。人工和 Codex 的审核都会保留在这里。")

    def _async(self, fn, callback):
        if self._busy or self._closed:
            return
        self._set_busy(True)
        def done(result):
            if not self._closed:
                self._set_busy(False)
                callback(result)
        def fail(error):
            if not self._closed:
                self._set_busy(False)
                self.status.setText(_error_text(error))
        self.executor.submit(fn, done, fail)

    def load(self):
        path = "/api/reviews?" + urlencode({"run_id": self.run_id})
        self.status.setText("正在读取此批审核历史…")
        def loaded(result):
            self.history.clear()
            items = result if isinstance(result, list) else result.get("items", [])
            for data in items:
                state = "已撤销" if data.get("status") == "reverted" else "已应用"
                item = QListWidgetItem(f"{data.get('reviewer', '')} · {len(data.get('changes', []))} 条 · {state}\n{data.get('created_at', '')}")
                item.setData(Qt.ItemDataRole.UserRole, data)
                self.history.addItem(item)
            self.status.setText(f"此批共 {len(items)} 次审核。")
            if self.history.count():
                self.history.setCurrentRow(0)
            else:
                self._selected()
        self._async(lambda: _request(self.client, "GET", path), loaded)

    def revert(self):
        item = self.history.currentItem()
        data = item.data(Qt.ItemDataRole.UserRole) if item else None
        if self._busy or not data or data.get("status") != "applied":
            return
        answer = QMessageBox.question(self, "撤销本次审核", "将撤销这次审核的全部修改，恢复之前的审核意见。\n原始证据保持原样；如有后续审核，整批撤销会被拒绝。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return
        changeset_id = data["id"]
        # Keep the same operation ID when retrying an uncertain response.
        operation = self._operations.setdefault(changeset_id, str(uuid4()))
        self.status.setText("正在撤销所选审核…")
        def reverted(_):
            self.changed.emit()
            self.load()
        self._async(lambda: _request(self.client, "POST", f"/api/reviews/{quote(changeset_id, safe='')}/revert", {"operation_id": operation}), reverted)


class CodexImportDialog(QDialog):
    """Accepted exposes a validated proposal; main window submits atomically."""
    def __init__(self, client, run_id, parent=None):
        super().__init__(parent)
        self.client, self.run_id, self.payload = client, run_id, None
        self.executor, self._closed, self._busy = ApiExecutor(self), False, False
        self.setWindowTitle("导入 Codex 审核")
        self.resize(820, 650)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        title = _label("导入审核")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        hint = _label("粘贴 Codex 审核 JSON，或从文件导入。", True)
        hint.setToolTip("支持 review / proposed_review / review_template 或 changes 数组，保留候选版本、证据引用与 operation_id。")
        layout.addWidget(hint)
        self.choose_button = QPushButton("选择 JSON 文件")
        self.choose_button.clicked.connect(self._choose)
        layout.addWidget(self.choose_button, alignment=Qt.AlignmentFlag.AlignLeft)
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText('{"run_id":"…","operation_id":"…","changes":[{"candidate_id":"…","base_version":1,"decision":"needs_info","reason":"原文依据与待确认项","evidence_ids":[],"tags":[]}]}')
        layout.addWidget(self.editor, 1)
        self.status = _label("提交时会校验版本与证据引用。", True)
        self.status.setToolTip("请先导出此批当前证据交给 Codex。版本冲突时需要重新导出复核。")
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("应用 Codex 审核")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primaryButton")
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.finished.connect(self._finish)

    def _finish(self, _):
        self._closed = True
        self.executor.shutdown(wait=False)

    def _async(self, message, fn, callback):
        if self._busy or self._closed:
            return
        self._busy = True
        self.choose_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.status.setText(message)
        def done(result):
            if self._closed:
                return
            self._busy = False
            self.choose_button.setEnabled(True)
            self.apply_button.setEnabled(True)
            callback(result)
        def failed(error):
            if self._closed:
                return
            self._busy = False
            self.choose_button.setEnabled(True)
            self.apply_button.setEnabled(True)
            self.status.setText(_error_text(error))
        self.executor.submit(fn, done, failed)

    def _choose(self):
        name, _ = QFileDialog.getOpenFileName(self, "选择 Codex 审核 JSON", "", "JSON 文件 (*.json);;所有文件 (*)")
        if not name:
            return
        snapshot = self.editor.toPlainText()
        def read_file():
            path = Path(name)
            if path.stat().st_size > MAX_INPUT_BYTES:
                raise FormError("审核文件超过 3.5 MB，请分批处理。")
            return path.read_text(encoding="utf-8-sig")
        def loaded(text):
            if snapshot != self.editor.toPlainText():
                self.status.setText("读取文件期间内容已修改，未覆盖编辑；请重新选择文件。")
                return
            self.editor.setPlainText(text)
            self.status.setText("已读取审核文件；应用时会再次核对批次、版本和证据引用。")
        self._async("正在读取审核文件…", read_file, loaded)

    def _accept(self):
        snapshot = self.editor.toPlainText()
        def parse():
            text = snapshot.lstrip("\ufeff")
            if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
                raise FormError("审核内容超过 3.5 MB，请分批处理。")
            return review_payload(json.loads(text), self.run_id)
        def ready(payload):
            if snapshot != self.editor.toPlainText():
                self.status.setText("审核内容已变化，请按当前内容重新应用。")
                return
            self.payload = payload
            self.editor.setPlainText(json.dumps(self.payload, ensure_ascii=False, indent=2))
            self.accept()
        self._async("正在校验审核 JSON…", parse, ready)
