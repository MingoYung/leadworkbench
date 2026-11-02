"""Actual native Qt Widgets workbench; every business operation uses the HTTP API."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import uuid

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QDesktopServices, QFont, QIcon, QTextBlockFormat
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSplitter, QStackedWidget, QTabWidget, QToolButton, QVBoxLayout, QWidget,
)
from .native_presenters import NativeListDelegate, line_icon
from .cli import ApiClient, ApiError, api_path, object_id
from .native_helpers import (
    ApiExecutor, CollapsibleSection, DECISIONS, ROLE_LABELS, SOURCES, STATUSES,
    apply_dark_theme, date_text, display, error_text, safe_source_url,
)

PLATFORMS = {'douyin':'抖音','weibo':'微博','xiaohongshu':'小红书','kuaishou':'快手',
             'bilibili':'哔哩哔哩','zhihu':'知乎','tieba':'百度贴吧','tiktok':'TikTok',
             'instagram':'Instagram','import':'导入','website':'网页'}
FACT_LABELS = {'brand_name':'品牌名称','country':'国家 / 市场','product':'服务 / 产品',
               'quantity':'数量','destination':'交付目的地','deadline':'期望时间',
               'author_handle':'作者账号','company_website':'网站','budget':'预算',
               'property_type':'户型','area':'面积','renovation_stage':'装修阶段',
               'start_date':'期望时间','posted_at':'发布日期'}
ACTIVE_STATUSES = {'running','pause_requested'}


def candidate_title(candidate):
    if candidate.get('kind') == 'comment':
        return '评论者：' + candidate['author_handle'] if candidate.get('author_handle') else '评论者待核实'
    return str(candidate.get('brand_name') or candidate.get('author_handle') or candidate.get('product') or '未识别主体')


def domestic_target(candidate, run):
    return ((candidate.get('analysis') or {}).get('targeting') or ((run or {}).get('config') or {}).get('targeting') or {})


def candidate_region(candidate, run):
    if domestic_target(candidate, run).get('market') == 'cn':
        match = (candidate.get('analysis') or {}).get('target_match') or candidate.get('target_match') or {}
        return str(match.get('matched_region') or '地区待核实')
    return str(candidate.get('country') or '国家待确认')


def evidenced_budget(candidate):
    if candidate.get('budget') not in (None, ''):
        return candidate['budget']
    match = (candidate.get('analysis') or {}).get('target_match') or candidate.get('target_match') or {}
    budget = (match.get('custom_evidence') or {}).get('预算')
    return budget.get('value') if isinstance(budget, dict) and budget.get('quote') else None


def candidate_excerpt(candidate, run):
    mode = ((run or {}).get('config') or {}).get('analysis_mode')
    if candidate.get('kind') == 'comment' or candidate.get('analysis_status') == 'not_requested' or mode == 'collect_only':
        return str(candidate.get('content_excerpt') or '原文已保存，打开后查看。')
    if domestic_target(candidate, run).get('market') == 'cn':
        parts = [str(candidate['product'])] if candidate.get('product') else []
        budget = evidenced_budget(candidate)
        if budget not in (None, ''):
            parts.append('预算 ' + display(budget))
        return ' · '.join(parts) or str(candidate.get('local_reason') or '等待核对原文')
    return ' · '.join(str(candidate[key]) for key in ('product','quantity') if candidate.get(key)) or str(candidate.get('local_reason') or '等待核对原文')


def source_facts(candidate, run):
    target = domestic_target(candidate, run)
    if target.get('market') == 'cn':
        match = (candidate.get('analysis') or {}).get('target_match') or candidate.get('target_match') or {}
        rows = [('地区',match.get('matched_region')),('服务 / 产品',candidate.get('product')),('预算',evidenced_budget(candidate))]
        if any(word in target.get('industry','') for word in ('家装','装修','装饰','整装','全屋','房产')):
            rows.extend([('户型',candidate.get('property_type')),('面积',candidate.get('area')),('装修阶段',candidate.get('renovation_stage'))])
        rows.append(('期望时间',candidate.get('start_date') or candidate.get('deadline')))
    else:
        rows = [('国家 / 市场',candidate.get('country')),('商品',candidate.get('product')),('数量',candidate.get('quantity')),('目的地',candidate.get('destination')),('需求截止',candidate.get('deadline'))]
    rows.append(('账号身份',ROLE_LABELS.get(candidate.get('role'),'待确认')))
    return rows


def label(text='', name='', wrap=True):
    result = QLabel(text)
    result.setTextFormat(Qt.TextFormat.PlainText)
    result.setWordWrap(wrap)
    result.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    if name:
        result.setObjectName(name)
    if name == 'muted':
        result.setProperty('muted',True)
    elif name in ('brand','detailTitle'):
        result.setProperty('heading',True)
    elif name == 'errorBanner':
        result.setProperty('error',True)
    elif name == 'warning':
        result.setProperty('warning',True)
    return result


class ElidedLabel(QLabel):
    """Keep the full accessible title while visually containing long task names."""
    def __init__(self,text='',parent=None):
        super().__init__(parent)
        self._full_text=''
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setText(text)

    def setText(self,text):
        self._full_text=str(text)
        self.setToolTip(self._full_text)
        self.setAccessibleDescription(self._full_text)
        self._elide()

    def _elide(self):
        QLabel.setText(self,self.fontMetrics().elidedText(self._full_text,Qt.TextElideMode.ElideRight,max(20,self.contentsRect().width())))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self._elide()


class EvidenceReader(QPlainTextEdit):
    """Plain source text that follows its content rather than looking like a form.

    Ordinary and long excerpts use the outer evidence scrollbar. Exceptionally
    large documents retain the native editor's own scrolling at a bounded height.
    """
    def __init__(self,text):
        super().__init__()
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setPlainText(text)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Fixed)
        self._fitting=False
        cursor=self.textCursor()
        cursor.select(cursor.SelectionType.Document)
        paragraph=QTextBlockFormat()
        paragraph.setLineHeight(155,QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
        cursor.mergeBlockFormat(paragraph)
        cursor.clearSelection()
        self.setTextCursor(cursor)
        self.setFixedHeight(70)

    def _fit(self):
        if self._fitting:return
        self._fitting=True
        try:
            height=16.0
            block=self.document().begin()
            while block.isValid() and height<20000:
                height+=self.blockBoundingRect(block).height()
                block=block.next()
            wanted=max(64,min(20000,int(height+10)))
            if abs(self.height()-wanted)>1:self.setFixedHeight(wanted)
        finally:
            self._fitting=False

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if event.oldSize().width()!=event.size().width():QTimer.singleShot(0,self._fit)

    def showEvent(self,event):
        super().showEvent(event)
        QTimer.singleShot(0,self._fit)


def plain(text, minimum=64, maximum=20000):
    return EvidenceReader(text)


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            widget = item.widget()
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout():
            clear_layout(item.layout())


class NativeWindow(QMainWindow):
    """Three native widget columns backed by nonblocking authenticated HTTP requests."""
    def __init__(self,data_dir,service_info=None):
        super().__init__()
        self.data_dir = Path(data_dir)
        self.client = ApiClient(self.data_dir)
        self.service_info = service_info or {}
        self.executor = ApiExecutor(self)
        self.runs, self.candidates, self.bootstrap = [], [], {}
        self.run, self.detail, self.selected_id = None, None, None
        self.page_size, self.offset, self.total = 50, 0, 0
        self._refreshing = self._action_busy = False
        self._detail_request = self._run_epoch = 0
        self._refresh_serial = 0
        self._detail_key = self._run_list_key = self._candidate_key = ''
        self._draft_reasons, self._checked = {}, set()
        self._pending_review = None
        self._pending_task_operation = None
        self._close_checking = self._closing_pending = self._can_close = False
        self._dialogs = []
        self.setWindowTitle('获客平台')
        self.setObjectName('acquisitionPlatform')
        self.resize(1380,860)
        self.setMinimumSize(1040,640)
        self._build_ui()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(3000)
        self.refresh_timer.timeout.connect(self.refresh)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(300)
        self.search_timer.timeout.connect(self._filters_changed)
        self.close_timer = QTimer(self)
        self.close_timer.setInterval(1000)
        self.close_timer.timeout.connect(self._check_paused_close)
        QTimer.singleShot(0,self._initialize)

    def _client(self,timeout=30):
        return ApiClient(self.data_dir,timeout=timeout)

    def _build_ui(self):
        body = QWidget()
        body.setObjectName('applicationBody')
        self.setCentralWidget(body)
        outer = QHBoxLayout(body)
        outer.setContentsMargins(0,0,0,0)
        outer.setSpacing(0)

        rail = QFrame()
        rail.setObjectName('appRail')
        rail.setFixedWidth(76)
        navigation = QVBoxLayout(rail)
        navigation.setContentsMargins(12,24,12,14)
        navigation.setSpacing(12)
        brand = label('获','brand',False)
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setFixedSize(38,38)
        brand.setToolTip('获客平台')
        navigation.addWidget(brand,0,Qt.AlignmentFlag.AlignHCenter)
        navigation.addSpacing(18)
        self.new_button = QPushButton()
        self.new_button.setIcon(line_icon('plus','#101114'))
        self.new_button.setObjectName('primary')
        self.new_button.setProperty('variant','primary')
        self.new_button.setFixedSize(44,40)
        self.new_button.setAccessibleName('新建任务')
        self.new_button.setToolTip('新建任务')
        self.new_button.clicked.connect(lambda:self.show_new())
        navigation.addWidget(self.new_button,0,Qt.AlignmentFlag.AlignHCenter)
        self.inbox_button = self._rail_button('线索','inbox',self.close_tasks)
        self.inbox_button.setChecked(True)
        navigation.addWidget(self.inbox_button)
        self.tasks_button = self._rail_button('任务','layers',self.toggle_tasks)
        navigation.addWidget(self.tasks_button)
        navigation.addStretch(1)
        navigation.addWidget(self._rail_button('帮助','help',self.show_help,checkable=False))
        navigation.addWidget(self._rail_button('设置','settings',self.show_settings,checkable=False))
        outer.addWidget(rail)

        self.task_drawer = QFrame()
        self.task_drawer.setObjectName('taskDrawer')
        self.task_drawer.setFixedWidth(244)
        drawer = QVBoxLayout(self.task_drawer)
        drawer.setContentsMargins(16,26,16,18)
        drawer.setSpacing(18)
        drawer_heading = QHBoxLayout()
        drawer_heading.addWidget(label('全部任务','sectionTitle'),1)
        self.refresh_button = QToolButton()
        self.refresh_button.setIcon(line_icon('refresh'))
        self.refresh_button.setAccessibleName('刷新任务')
        self.refresh_button.setToolTip('刷新任务')
        self.refresh_button.setProperty('variant','flat')
        self.refresh_button.clicked.connect(lambda:self.refresh(force=True))
        drawer_heading.addWidget(self.refresh_button)
        drawer.addLayout(drawer_heading)
        self.run_list = QListWidget()
        self.run_list.setObjectName('runList')
        self.run_list.setAccessibleName('我的任务')
        self.run_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.run_list.setItemDelegate(NativeListDelegate(self.run_list))
        self.run_list.currentItemChanged.connect(self._run_selected)
        self.run_list.itemClicked.connect(lambda:self.close_tasks())
        self.run_list.itemActivated.connect(lambda:self.close_tasks())
        self.run_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.run_list.customContextMenuRequested.connect(self._task_context_menu)
        drawer.addWidget(self.run_list,1)
        self.context_section = CollapsibleSection('任务详情')
        self.context_text = label('选择任务后查看条件与运行信息。','muted')
        self.context_section.content_layout.addWidget(self.context_text)
        drawer.addWidget(self.context_section)
        self.manage_tasks_button = QPushButton('任务管理 / 回收站')
        self.manage_tasks_button.setProperty('variant','flat')
        self.manage_tasks_button.setAccessibleName('任务管理与回收站')
        self.manage_tasks_button.clicked.connect(lambda:self.show_task_manager())
        drawer.addWidget(self.manage_tasks_button)
        self.task_drawer.hide()
        outer.addWidget(self.task_drawer)

        right = QWidget()
        right.setObjectName('workspace')
        main = QVBoxLayout(right)
        main.setContentsMargins(0,0,0,0)
        main.setSpacing(0)
        header = QFrame()
        header.setObjectName('workspaceHeader')
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(26,21,26,19)
        header_row.setSpacing(14)
        header_row.addWidget(label('线索收件箱','pageTitle',False))
        header_row.addSpacing(10)
        task_heading = QVBoxLayout()
        task_heading.setSpacing(4)
        self.run_name = ElidedLabel('选择一个任务')
        self.run_name.setObjectName('runTitle')
        self.run_name.setMinimumWidth(70)
        self.run_name.setMaximumWidth(430)
        self.run_name.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.run_name.setAccessibleName('当前任务名称')
        task_heading.addWidget(self.run_name)
        self.run_status = label('','statusBadge',False)
        task_heading.addWidget(self.run_status)
        header_row.addLayout(task_heading,1)
        switch_task = QToolButton()
        switch_task.setIcon(line_icon('chevron'))
        switch_task.setAccessibleName('切换任务')
        switch_task.setToolTip('切换任务')
        switch_task.setProperty('variant','flat')
        switch_task.clicked.connect(self.toggle_tasks)
        header_row.addWidget(switch_task)
        self.connection_label = label('连接中','connectionLabel',False)
        self.connection_label.setProperty('muted',True)
        header_row.addWidget(self.connection_label)
        self.control_button = QPushButton('开始采集')
        self.control_button.setProperty('variant','primary')
        self.control_button.clicked.connect(self.control_run)
        self.control_button.hide()
        header_row.addWidget(self.control_button)
        self.more_button = QToolButton()
        self.more_button.setIcon(line_icon('more'))
        self.more_button.setAccessibleName('更多任务操作')
        self.more_button.setToolTip('任务操作')
        self.more_button.setProperty('variant','flat')
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more_button)
        for text,callback in [('审核历史',self.show_history),('导入 Codex 审核',self.show_review_import),('导出 JSON',lambda:self.export_run('json')),('导出 CSV',lambda:self.export_run('csv'))]:
            menu.addAction(text,callback)
        menu.addSeparator()
        menu.addAction('任务管理',lambda:self.show_task_manager())
        menu.addAction('回收站',lambda:self.show_task_manager(initial_tab='trash'))
        self.trash_action = menu.addAction('移到回收站',lambda:self.trash_run())
        self.more_button.setMenu(menu)
        self.more_button.setEnabled(False)
        header_row.addWidget(self.more_button)
        main.addWidget(header)
        self.batch_overview = QFrame()
        self.batch_overview.setObjectName('batchOverview')
        batch_row = QHBoxLayout(self.batch_overview)
        batch_row.setContentsMargins(26,10,26,10)
        batch_row.setSpacing(12)
        self.batch_caption = ElidedLabel('批量采集与初筛')
        self.batch_caption.setObjectName('batchCaption')
        self.batch_caption.setMinimumWidth(90)
        self.batch_caption.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        batch_row.addWidget(self.batch_caption,1)
        self.batch_detail = ElidedLabel('')
        self.batch_detail.setProperty('muted',True)
        self.batch_detail.setMinimumWidth(90)
        self.batch_detail.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        batch_row.addWidget(self.batch_detail,1)
        self.batch_progress = QProgressBar()
        self.batch_progress.setAccessibleName('整批处理进度')
        self.batch_progress.setTextVisible(False)
        self.batch_progress.setFixedWidth(90)
        self.batch_progress.setFixedHeight(5)
        batch_row.addWidget(self.batch_progress)
        self.ai_count_buttons = {}
        for decision,title in [('keep','AI推荐'),('needs_info','AI补查'),('exclude','AI排除')]:
            button = QToolButton()
            button.setText(title+' 0')
            button.setProperty('variant','flat')
            button.setToolTip('本批全部候选中的模型建议；不等于人工已审核。')
            button.clicked.connect(lambda _checked=False,d=decision:self.filter_ai(d))
            self.ai_count_buttons[decision] = button
            batch_row.addWidget(button)
        self.new_ai_button = QToolButton()
        self.new_ai_button.setText('新建 AI 批次')
        self.new_ai_button.setProperty('variant','flat')
        self.new_ai_button.setToolTip('创建一个新的批量任务，并预选本地 AI 初筛。原批次与审核保持不变。')
        self.new_ai_button.clicked.connect(lambda:self.show_new(analysis_mode='local'))
        batch_row.addWidget(self.new_ai_button)
        self.batch_overview.hide()
        main.addWidget(self.batch_overview)
        self.error_banner = label('','errorBanner')
        self.error_banner.hide()
        main.addWidget(self.error_banner)
        self.progress_label = label('','progressNote')
        self.progress_label.setProperty('muted',True)
        self.progress_label.hide()
        main.addWidget(self.progress_label)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        list_panel = QFrame()
        list_panel.setObjectName('inboxPanel')
        list_panel.setMinimumWidth(290)
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(0,0,0,0)
        list_layout.setSpacing(0)
        inbox_header = QFrame()
        inbox_header.setObjectName('inboxHeader')
        inbox_top = QVBoxLayout(inbox_header)
        inbox_top.setContentsMargins(20,20,20,17)
        inbox_top.setSpacing(15)
        list_head = QHBoxLayout()
        self.candidate_count = label('全部线索','sectionTitle',False)
        list_head.addWidget(self.candidate_count,1)
        self.order_combo = QComboBox()
        self.order_combo.setObjectName('candidateOrder')
        self.order_combo.setAccessibleName('线索排序')
        self.order_combo.setToolTip('优先展示模型建议保留的线索；所有候选仍可筛选、复核。')
        self.order_combo.addItem('AI优先','ai_priority')
        self.order_combo.addItem('采集顺序','')
        self.order_combo.currentIndexChanged.connect(self._filters_changed)
        list_head.addWidget(self.order_combo)
        self.selection_menu = QToolButton()
        self.selection_menu.setIcon(line_icon('check'))
        self.selection_menu.setAccessibleName('批量审核')
        self.selection_menu.setToolTip('批量选择与审核')
        self.selection_menu.setProperty('variant','flat')
        self.selection_menu.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        selection_actions = QMenu(self.selection_menu)
        for decision in ('keep','needs_info','watch','exclude'):
            action = selection_actions.addAction(DECISIONS[decision])
            action.triggered.connect(lambda _checked=False,d=decision:self.batch_review(d))
        selection_actions.addSeparator()
        selection_actions.addAction('选择本页',self._select_page)
        selection_actions.addAction('取消选择',self._clear_checked)
        self.selection_menu.setMenu(selection_actions)
        list_head.addWidget(self.selection_menu)
        inbox_top.addLayout(list_head)
        filters = QHBoxLayout()
        filters.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText('搜索线索')
        self.search.setAccessibleName('搜索候选')
        self.search.addAction(line_icon('search'),QLineEdit.ActionPosition.LeadingPosition)
        self.search.textChanged.connect(lambda:self.search_timer.start())
        self.filter_combo = QComboBox()
        self.filter_combo.setAccessibleName('审核状态筛选')
        self.filter_combo.setMinimumWidth(104)
        choices = [('', '全部'),('ai:keep','AI推荐'),('ai:needs_info','AI补查'),
                   ('ai:watch','AI观察'),('ai:exclude','AI排除'),('ai:unanalysed','AI未初筛')]
        choices += [(value,'复核'+title if value!='unreviewed' else '尚未复核') for value,title in DECISIONS.items()]
        for value,title in choices:
            self.filter_combo.addItem(title,value)
        self.filter_combo.currentIndexChanged.connect(self._filters_changed)
        filters.addWidget(self.search,1)
        filters.addWidget(self.filter_combo)
        inbox_top.addLayout(filters)
        list_layout.addWidget(inbox_header)
        self.candidate_stack = QStackedWidget()
        self.candidate_list = QListWidget()
        self.candidate_list.setObjectName('candidateList')
        self.candidate_list.setAccessibleName('候选线索')
        self.candidate_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.candidate_list.setItemDelegate(NativeListDelegate(self.candidate_list))
        self.candidate_list.setWordWrap(False)
        self.candidate_list.currentItemChanged.connect(self._candidate_selected)
        self.candidate_list.itemChanged.connect(self._check_changed)
        self.candidate_stack.addWidget(self.candidate_list)
        self.empty_candidates = self._empty_view('暂无候选线索','选择来源，开始第一轮采集。','search')
        self.candidate_stack.addWidget(self.empty_candidates)
        self.candidate_stack.setCurrentIndex(1)
        list_layout.addWidget(self.candidate_stack,1)
        page = QHBoxLayout()
        page.setContentsMargins(20,12,20,14)
        self.page_label = label('0 / 0','muted',False)
        page.addWidget(self.page_label,1)
        self.prev_button,self.next_button = QPushButton('上页'),QPushButton('下页')
        for button in (self.prev_button,self.next_button):
            button.setProperty('variant','flat')
        self.prev_button.clicked.connect(lambda:self.change_page(-1))
        self.next_button.clicked.connect(lambda:self.change_page(1))
        page.addWidget(self.prev_button);page.addWidget(self.next_button)
        list_layout.addLayout(page)
        self.splitter.addWidget(list_panel)

        detail_panel = QFrame()
        detail_panel.setObjectName('detailPanel')
        detail_panel.setMinimumWidth(420)
        detail_outer = QVBoxLayout(detail_panel)
        detail_outer.setContentsMargins(0,0,0,0)
        self.detail_stack = QStackedWidget()
        self.detail_empty = self._empty_view('选择一条线索','核对原文，再决定是否跟进。','inbox')
        self.detail_stack.addWidget(self.detail_empty)
        reader = QWidget()
        reader.setObjectName('reader')
        detail_layout = QVBoxLayout(reader)
        detail_layout.setContentsMargins(30,25,30,24)
        detail_layout.setSpacing(18)
        reader_header = QWidget()
        reader_header.setObjectName('readerHeader')
        reader_heading = QHBoxLayout(reader_header)
        reader_heading.setContentsMargins(0,0,0,0)
        reader_heading.setSpacing(13)
        self.platform_avatar = label('','platformAvatar',False)
        self.platform_avatar.setFixedSize(40,40)
        self.platform_avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        reader_heading.addWidget(self.platform_avatar)
        reader_identity = QVBoxLayout()
        reader_identity.setSpacing(5)
        self.detail_title = ElidedLabel('选择一条线索')
        self.detail_title.setObjectName('detailTitle')
        self.detail_title.setSizePolicy(QSizePolicy.Policy.Ignored,QSizePolicy.Policy.Preferred)
        self.detail_title.setMinimumHeight(25)
        reader_identity.addWidget(self.detail_title)
        self.detail_kicker = label('','muted',False)
        reader_identity.addWidget(self.detail_kicker)
        reader_heading.addLayout(reader_identity,1)
        self.source_button = QPushButton('原链接')
        self.source_button.setProperty('variant','flat')
        self.source_button.setIcon(line_icon('external'))
        self.source_button.setEnabled(False)
        self.source_button.clicked.connect(self.open_detail_source)
        reader_heading.addWidget(self.source_button)
        detail_layout.addWidget(reader_header)
        self.facts_label = label('','muted')
        self.facts_label.hide()
        detail_layout.addWidget(self.facts_label)
        self.tabs = QTabWidget()
        self.tabs.setObjectName('detailTabs')
        self.evidence_widget,self.evidence_layout,self.evidence_scroll = self._scroll_tab('原文')
        self.analysis_widget,self.analysis_layout,self.analysis_scroll = self._scroll_tab('判断')
        self.reviews_widget,self.reviews_layout,self.reviews_scroll = self._scroll_tab('记录')
        detail_layout.addWidget(self.tabs,1)

        review_bar = QFrame()
        review_bar.setObjectName('reviewBar')
        review_layout = QVBoxLayout(review_bar)
        review_layout.setContentsMargins(0,18,0,0)
        review_layout.setSpacing(12)
        review_top = QHBoxLayout()
        self.review_label = label('未审核','muted',False)
        review_top.addWidget(self.review_label,1)
        self.reason_toggle = QToolButton()
        self.reason_toggle.setText('添加理由')
        self.reason_toggle.setProperty('variant','flat')
        self.reason_toggle.setCheckable(True)
        self.reason_toggle.setAccessibleName('展开审核理由')
        review_top.addWidget(self.reason_toggle)
        review_layout.addLayout(review_top)
        self.reason = QPlainTextEdit()
        self.reason.setObjectName('reviewReason')
        self.reason.setPlaceholderText('写下判断依据（选填）')
        self.reason.setAccessibleName('人工审核理由')
        self.reason.setFixedHeight(68)
        self.reason.hide()
        self.reason.textChanged.connect(self._save_reason_draft)
        self.reason_toggle.toggled.connect(self.reason.setVisible)
        review_layout.addWidget(self.reason)
        self.review_buttons = {}
        actions = QHBoxLayout()
        actions.setSpacing(8)
        for decision in ('keep','needs_info','watch','exclude'):
            button = QPushButton('保留线索' if decision=='keep' else DECISIONS[decision])
            button.setProperty('variant','primary' if decision=='keep' else 'flat')
            if decision=='exclude':button.setObjectName('excludeButton')
            button.setMinimumWidth(100 if decision=='keep' else 62)
            button.clicked.connect(lambda _checked=False,d=decision:self.submit_review(d))
            self.review_buttons[decision] = button
            actions.addWidget(button)
        actions.addStretch(1)
        review_layout.addLayout(actions)
        detail_layout.addWidget(review_bar)
        self.detail_stack.addWidget(reader)
        detail_outer.addWidget(self.detail_stack)
        self.splitter.addWidget(detail_panel)
        self.splitter.setSizes([360,850])
        self.splitter.setStretchFactor(0,0)
        self.splitter.setStretchFactor(1,1)
        main.addWidget(self.splitter,1)
        outer.addWidget(right,1)
        self.statusBar().setSizeGripEnabled(False)
        self.statusBar().showMessage('就绪')
        self._set_review_enabled(False)

    def _rail_button(self,text,icon,callback,checkable=True):
        button = QToolButton()
        button.setText(text)
        button.setIcon(line_icon(icon))
        button.setIconSize(QSize(20,20))
        button.setObjectName('railButton')
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        button.setCheckable(checkable)
        button.setFixedSize(52,58)
        button.setAccessibleName(text)
        button.clicked.connect(callback)
        return button

    def toggle_tasks(self):
        visible = not self.task_drawer.isVisible()
        self.task_drawer.setVisible(visible)
        self.tasks_button.setChecked(visible)
        self.inbox_button.setChecked(not visible)
        if visible:self.run_list.setFocus()

    def close_tasks(self):
        self.task_drawer.hide()
        self.tasks_button.setChecked(False)
        self.inbox_button.setChecked(True)
        self.candidate_list.setFocus()

    def keyPressEvent(self,event):
        if event.key()==Qt.Key.Key_Escape and self.task_drawer.isVisible():
            self.close_tasks()
            event.accept()
            return
        super().keyPressEvent(event)

    def _empty_view(self,title,subtitle,icon):
        widget = QWidget()
        widget.setObjectName('emptyState')
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(24,24,24,24)
        layout.setSpacing(12)
        layout.addStretch(1)
        glyph = QLabel()
        glyph.setPixmap(line_icon(icon,'#555B6B',36).pixmap(36,36))
        glyph.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(glyph)
        heading = label(title,'emptyTitle')
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        description = label(subtitle,'muted')
        description.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(description)
        layout.addStretch(1)
        return widget


    def _scroll_tab(self,title):
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0,19,8,10)
        layout.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        self.tabs.addTab(scroll,title)
        return content,layout,scroll

    def _initialize(self):
        def done(data):
            self.bootstrap = data
            self.connection_label.setText('已连接')
            self.connection_label.setToolTip('本机服务 '+str(data.get('app_version','')))
            self.refresh_timer.start()
            self.refresh(force=True)
        self.executor.submit(lambda:self._client().request('GET','/api/bootstrap'),done,self._error)

    def _error(self,error,*,dialog=False):
        text = error_text(error)
        self.error_banner.setText(text)
        self.error_banner.show()
        self.statusBar().showMessage(text,12000)
        if dialog:
            QMessageBox.warning(self,'操作未完成',text)

    def refresh(self,force=False):
        if self._refreshing or self._action_busy or self._closing_pending or self._can_close:
            return
        if not force and QApplication.activeModalWidget():
            return
        self._refreshing = True
        self._refresh_serial += 1
        serial = self._refresh_serial
        run_id,epoch = (self.run or {}).get('id'),self._run_epoch
        query,filter_value,offset = self.search.text(),self.filter_combo.currentData(),self.offset
        order = self.order_combo.currentData()
        decision,ai_decision = ('',filter_value[3:]) if str(filter_value).startswith('ai:') else (filter_value,'')
        def work():
            client = self._client()
            runs = client.request('GET','/api/runs')
            result = {'runs':runs.get('items',[]) if isinstance(runs,dict) else runs}
            if run_id:
                if not any(row['id']==run_id for row in result['runs']):
                    result['missing_run'] = True
                else:
                    try:
                        result['run'] = client.request('GET',f'/api/runs/{object_id(run_id)}')
                        result['candidates'] = client.request('GET',api_path('/api/candidates',run_id=run_id,q=query,decision=decision,ai_decision=ai_decision,order=order,limit=self.page_size,offset=offset))
                    except ApiError as error:
                        if error.code not in {'RUN_DELETED','NOT_FOUND'}:raise
                        result['missing_run'] = True
            return result
        def done(result):
            if serial!=self._refresh_serial:return
            self._refreshing = False
            self.error_banner.hide()
            self.runs = result['runs']
            if result.get('missing_run') and epoch==self._run_epoch:
                self._clear_current_run()
                self._render_runs()
                if self.runs:self.select_run(self.runs[0]['id'])
                return
            self._render_runs()
            if (query,filter_value,offset,order)!=(self.search.text(),self.filter_combo.currentData(),self.offset,self.order_combo.currentData()):
                QTimer.singleShot(0,lambda:self.refresh(force=True))
                return
            if epoch==self._run_epoch and run_id==(self.run or {}).get('id') and result.get('run'):
                self.run = result['run']
                page = result['candidates']
                self.candidates,self.total = page.get('items',[]),int(page.get('total',0))
                self._render_header()
                self._render_candidates()
                summary = next((c for c in self.candidates if c['id']==self.selected_id),None)
                if summary and (not self.detail or summary.get('version')!=self.detail.get('version')):
                    self.load_detail(summary['id'])
            elif not run_id and self.runs and not self.run:
                self.select_run(self.runs[0]['id'])
        def failed(error):
            if serial!=self._refresh_serial:return
            self._refreshing = False
            self._error(error)
        self.executor.submit(work,done,failed)

    def _render_runs(self):
        key = json.dumps(self.runs,ensure_ascii=False,sort_keys=True)
        if key==self._run_list_key:
            return
        self._run_list_key = key
        scroll,selected = self.run_list.verticalScrollBar().value(),(self.run or {}).get('id')
        self.run_list.blockSignals(True)
        self.run_list.clear()
        for run in self.runs:
            demo = '演示 · ' if run.get('is_demo') else ''
            item = QListWidgetItem(f"{demo}{run.get('name','未命名任务')}\n{STATUSES.get(run.get('status'),run.get('status',''))} · {(run.get('counts') or {}).get('total',0)} 条")
            item.setData(Qt.ItemDataRole.UserRole,run['id'])
            item.setData(Qt.ItemDataRole.UserRole+1,{'kind':'task','title':run.get('name','未命名任务'),'subtitle':STATUSES.get(run.get('status'),run.get('status','')),'badge':str((run.get('counts') or {}).get('total',0)),'demo':bool(run.get('is_demo'))})
            item.setToolTip(f"{run.get('name','')}\n{SOURCES.get(run.get('source'),run.get('source',''))}")
            self.run_list.addItem(item)
            if run['id']==selected:
                self.run_list.setCurrentItem(item)
        self.run_list.verticalScrollBar().setValue(scroll)
        self.run_list.blockSignals(False)

    def _run_selected(self,current,previous):
        if current:
            self.select_run(current.data(Qt.ItemDataRole.UserRole))

    def select_run(self,run_id):
        if (self.run or {}).get('id')==run_id:
            return
        self._save_reason_draft()
        self._run_epoch += 1
        self._refresh_serial += 1
        self.run = next((r for r in self.runs if r['id']==run_id),{'id':run_id,'name':'正在读取任务…'})
        self.selected_id,self.detail = None,None
        self._detail_key = self._candidate_key = ''
        self._checked.clear()
        self.offset = self.total = 0
        self.search.blockSignals(True);self.search.clear();self.search.blockSignals(False)
        self.filter_combo.blockSignals(True);self.filter_combo.setCurrentIndex(0);self.filter_combo.blockSignals(False)
        local_mode = (self.run.get('config') or {}).get('analysis_mode')=='local'
        self.order_combo.blockSignals(True)
        self.order_combo.setCurrentIndex(0 if local_mode else 1)
        self.order_combo.blockSignals(False)
        self._clear_detail()
        self._render_header()
        self._run_list_key = ''
        self._render_runs()
        self._refreshing = False
        self.refresh(force=True)

    def _clear_current_run(self):
        self._save_reason_draft()
        self._run_epoch += 1
        self._detail_request += 1
        self._refresh_serial += 1
        self._refreshing = False
        self.run,self.detail,self.selected_id = None,None,None
        self.candidates = []
        self.total = self.offset = 0
        self._checked.clear()
        self._detail_key = self._candidate_key = self._run_list_key = ''
        self._pending_review = None
        self.run_name.setText('暂无任务')
        self._pending_task_operation = None
        self.run_status.clear()
        self.control_button.hide()
        self.more_button.setEnabled(False)
        self.batch_overview.hide()
        self.progress_label.hide()
        self.context_text.setText('新建任务后查看单次采集设置与进度。')
        self._clear_detail()
        self._render_candidates()

    def _task_context_menu(self,point):
        item = self.run_list.itemAt(point)
        menu = QMenu(self.run_list)
        if item:
            run_id = item.data(Qt.ItemDataRole.UserRole)
            row = next((run for run in self.runs if run['id']==run_id),None)
            action = menu.addAction('移到回收站',lambda:self.trash_run(run_id))
            action.setEnabled(bool(row) and row.get('status') not in ACTIVE_STATUSES and not self._action_busy)
            if row and row.get('status') in ACTIVE_STATUSES:
                action.setToolTip('先暂停任务并等待当前请求结束。')
            menu.addSeparator()
        menu.addAction('任务管理',lambda:self.show_task_manager())
        menu.addAction('回收站',lambda:self.show_task_manager(initial_tab='trash'))
        menu.exec(self.run_list.viewport().mapToGlobal(point))

    def _tasks_changed(self,receipt):
        affected = set(receipt.get('run_ids') or [])
        pending = set((self._pending_task_operation or {}).get('run_ids') or [])
        if affected.intersection(pending):self._pending_task_operation=None
        if receipt.get('action') in {'trash','purge'} and (self.run or {}).get('id') in affected:
            self._clear_current_run()
        else:
            self._refresh_serial += 1
            self._refreshing = False
            self._run_list_key = ''
        self.refresh(force=True)

    def show_task_manager(self,initial_tab='active'):
        if self._action_busy or self._close_checking or self._closing_pending:return
        from .native_tasks import TaskManagerDialog
        dialog = TaskManagerDialog(self.client,self,initial_tab=initial_tab)
        dialog.changed.connect(self._tasks_changed)
        self._dialog(dialog)

    def trash_run(self,run_id=None):
        if self._action_busy or self._close_checking or self._closing_pending:return
        run_id = run_id or (self.run or {}).get('id')
        run = next((row for row in self.runs if row['id']==run_id),None)
        if not run:return
        if run.get('status') in ACTIVE_STATUSES:
            self._error(ValueError('先暂停任务，等待当前请求结束后再移到回收站。'),dialog=True)
            return
        box = QMessageBox(self)
        box.setWindowTitle('移到回收站')
        box.setTextFormat(Qt.TextFormat.PlainText)
        box.setText('将这个任务移到回收站？')
        box.setInformativeText(f"{run.get('name','未命名任务')}\n{(run.get('counts') or {}).get('total',0)} 条候选，原文和审核记录会保留，可在回收站恢复。")
        move = box.addButton('移到回收站',QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton('取消',QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        if box.clickedButton()!=move:return
        if not self._pending_task_operation or self._pending_task_operation['run_ids']!=[run_id]:
            self._pending_task_operation={'run_ids':[run_id],'operation_id':str(uuid.uuid4())}
        payload=dict(self._pending_task_operation)
        self._action_busy=True
        self._set_review_enabled(False)
        def done(receipt):
            self._action_busy=False
            self._pending_task_operation=None
            self._tasks_changed(receipt)
            self._set_review_enabled(bool(self.detail))
            self.statusBar().showMessage('已移到回收站，可在任务管理中恢复。',10000)
        def failed(error):
            self._action_busy=False
            if getattr(error,'code','')=='TASK_STATE_CHANGED':
                self._pending_task_operation=None
                self.refresh(force=True)
            self._set_review_enabled(bool(self.detail))
            self._error(error,dialog=True)
        def work():
            client=self._client()
            receipt=client.request('POST','/api/runs/trash',payload)
            current=client.request('GET','/api/runs?scope=trash')
            if not any(row['id']==run_id for row in current.get('items',[])):
                raise ApiError('TASK_STATE_CHANGED','任务状态已经被其他操作改变，请刷新后重新确认。')
            return receipt
        self.executor.submit(work,done,failed)

    def _render_header(self):
        if not self.run:
            return
        run = self.run
        self.run_name.setText(run.get('name','未命名任务'))
        self.run_name.setToolTip(run.get('name',''))
        self.run_status.setText(('合成演示 · ' if run.get('is_demo') else '')+STATUSES.get(run.get('status'),run.get('status','')))
        self.more_button.setEnabled(True)
        self.trash_action.setEnabled(run.get('status') not in ACTIVE_STATUSES and not self._action_busy)
        control = {'running':('暂停采集','pause'),'paused':('继续采集','resume'),'interrupted':('继续采集','resume'),'created':('开始采集','start'),'failed':('重试采集','start')}.get(run.get('status'))
        self.control_button.setVisible(control is not None or run.get('status')=='pause_requested')
        self.control_button.setText(control[0] if control else '正在暂停')
        self.control_button.setProperty('runAction',control[1] if control else '')
        self.control_button.setEnabled(bool(control) and not self._action_busy)
        config,target = run.get('config') or {},(run.get('config') or {}).get('targeting') or {}
        lines = [run.get('goal') or '未补充目标',f"来源：{SOURCES.get(run.get('source'),run.get('source',''))}"]
        if target:
            lines += [f"目标：{'国内' if target.get('market')=='cn' else '海外'} / {target.get('industry','')} / {'、'.join(target.get('regions',[]))}",f"标签：{'、'.join(target.get('include_tags',[]))}"]
            if target.get('exclude_tags'):
                lines.append('排除：'+'、'.join(target['exclude_tags']))
            if target.get('custom_fields'):
                lines.append('补充条件：'+display(target['custom_fields']))
        maximum = (run.get('batch_summary') or {}).get('effective_limit',min(int(config.get('limit',20)),50))
        lines += [f"运行方式：单次批次，最多 {maximum} 条来源（所有平台合计）。",
                  '采集结束：达到上限，或本轮来源已读完；结果不足时不会一直凑数。',
                  '本批结束后不自动重跑。暂停会等待当前网络 / 推理步骤结束。',
                  f"创建：{date_text(run.get('created_at'))}",f"任务 ID：{run['id']}"]
        self.context_text.setText('\n'.join(lines))
        mode = config.get('analysis_mode')
        text = '合成演示，不调用模型，不代表真实客户。' if run.get('is_demo') else '只采集 · 待人工 / Codex 审核' if mode=='collect_only' else '本地模型初筛' if mode=='local' else '采集与审核'
        progress = run.get('progress') or {}
        if progress.get('message'):
            text += f" · {progress['message']} ({progress.get('current',0)} / {progress.get('total',0)})"
        summary = progress.get('collection_summary')
        if summary:
            text += f"\n评论采样：已读 {summary.get('comments_read',0)}，匹配 {summary.get('comments_matched',0)}，返回 {summary.get('comments_returned',0)}；限于本轮帖子。"
            if not summary.get('comments_matched'):
                text += ' 零命中不代表全平台没有需求。'
        if run.get('error'):
            text += '\n任务错误：'+display(run['error'])
        self.progress_label.setToolTip(text)
        self.context_text.setText(self.context_text.text()+'\n'+text)
        if run.get('error'):
            failure = run['error']
            message = failure.get('message') if isinstance(failure,dict) else failure
            self.progress_label.setText('任务未完成：'+display(message))
        elif run.get('status')=='completed_partial':
            self.progress_label.setText('部分来源不完整，已保存的原文可继续审核。')
        elif run.get('status') in ACTIVE_STATUSES:
            self.progress_label.setText(progress.get('message') or '正在采集，原文会陆续保存。')
        else:
            self.progress_label.setText('')
        self.progress_label.setVisible(bool(self.progress_label.text()))
        self._render_batch_overview()

    def filter_ai(self,decision):
        index = self.filter_combo.findData('ai:'+decision)
        if index>=0:self.filter_combo.setCurrentIndex(index)

    def _render_batch_overview(self):
        run = self.run or {}
        summary = run.get('batch_summary') or {}
        config = run.get('config') or {}
        mode = config.get('analysis_mode')
        demo = run.get('source')=='demo'
        local = mode=='local' and not demo
        count = int(summary.get('collected',(run.get('counts') or {}).get('total',0)))
        analyzed = int(summary.get('ai_analyzed',0))
        processed = int(summary.get('processed',(run.get('counts') or {}).get('total',0)))
        maximum = int(summary.get('effective_limit',min(int(config.get('limit',20)),50)))
        phase = summary.get('phase') or run.get('status','created')
        if run.get('status')=='completed_partial':phase='completed_partial'
        names = {'created':'待运行','collecting':'采集中','analyzing':'AI初筛中','saving':'保存中',
                 'completed':'已完成','completed_partial':'部分完成','paused':'已暂停',
                 'pause_requested':'正在暂停','failed':'运行失败','interrupted':'已中断'}
        self.batch_caption.setText(('演示批次' if demo else '整批采集 + AI初筛' if local else '批量采集 · 未启用AI' if mode=='collect_only' else '批量任务')+'  '+names.get(phase,phase))
        detail = f'采集 {count} 条 / AI {analyzed} 条' if local else f'已保存 {processed} 条'
        if phase=='created':detail=f'单次 · 最多 {maximum} 条'
        self.batch_detail.setText(detail)
        self.batch_detail.setToolTip(f'本批最多采集 {maximum} 条来源；已取得 {count} 条，已入库 {processed} 条，真实AI初筛 {analyzed} 条。数量是上限，不保证每个平台都有足够结果。')
        suggestions = summary.get('ai_suggestions') or {}
        for decision,title in [('keep','AI推荐'),('needs_info','AI补查'),('exclude','AI排除')]:
            button = self.ai_count_buttons[decision]
            number = int(suggestions.get(decision,0))
            button.setText(f'{title} {number}')
            button.setVisible(local)
            button.setEnabled(number>0)
        self.new_ai_button.setVisible(not local and not demo)
        if phase=='collecting' or phase=='created':
            upper,value=maximum,count
        elif local:
            upper,value=max(count,1),analyzed
        else:
            upper,value=max(count,1),processed
        self.batch_progress.setRange(0,max(1,upper))
        self.batch_progress.setValue(value)
        self.batch_progress.setToolTip('一条任务自动处理整批；本地模型按顺序逐条执行，不需要手动逐条点击。')
        self.batch_overview.setVisible(bool(run))

    def _filters_changed(self,*_):
        if self.run:
            self.offset = 0
            self._checked.clear()
            self._candidate_key = ''
            self.refresh(force=True)

    def _render_candidates(self):
        key = json.dumps([self.candidates,self.selected_id,sorted(self._checked)],ensure_ascii=False,sort_keys=True)
        if key!=self._candidate_key:
            self._candidate_key = key
            scroll = self.candidate_list.verticalScrollBar().value()
            self.candidate_list.blockSignals(True)
            self.candidate_list.clear()
            for candidate in self.candidates:
                decision = DECISIONS.get(candidate.get('effective_decision'),'未审核')
                ai_decision = candidate.get('ai_decision')
                real_review = candidate.get('effective_decision') not in (None,'unreviewed')
                badge = decision if real_review else {'keep':'AI推荐','needs_info':'AI补查','watch':'AI观察','exclude':'AI排除'}.get(ai_decision,'未复核')
                badge_decision = candidate.get('effective_decision') if real_review else ai_decision or 'unreviewed'
                flag = '合成演示 · ' if candidate.get('is_demo') else ''
                excerpt = candidate_excerpt(candidate,self.run).replace('\n',' ')
                if len(excerpt)>150:
                    excerpt = excerpt[:150]+'…'
                text = f"{flag}{candidate_title(candidate)}\n{PLATFORMS.get(candidate.get('platform'),candidate.get('platform','来源'))} / {candidate_region(candidate,self.run)} / {ROLE_LABELS.get(candidate.get('role'),'身份待核')}\n{excerpt}\n{badge} · {date_text(candidate.get('posted_at'))}"
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole,candidate['id'])
                item.setData(Qt.ItemDataRole.UserRole+1,{'kind':'candidate','title':candidate_title(candidate).removeprefix('评论者：'),'subtitle':candidate_region(candidate,self.run),'excerpt':excerpt,'platform':PLATFORMS.get(candidate.get('platform'),candidate.get('platform','来源')),'time':date_text(candidate['posted_at']) if candidate.get('posted_at') else '', 'badge':badge,'decision':badge_decision,'demo':bool(candidate.get('is_demo'))})
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if candidate['id'] in self._checked else Qt.CheckState.Unchecked)
                self.candidate_list.addItem(item)
                if candidate['id']==self.selected_id:
                    self.candidate_list.setCurrentItem(item)
            self.candidate_list.verticalScrollBar().setValue(scroll)
            self.candidate_list.blockSignals(False)
        self.candidate_stack.setCurrentIndex(0 if self.candidates else 1)
        title = '筛选结果' if self.filter_combo.currentData() or self.search.text() else '全部线索'
        self.candidate_count.setText(f'{title}  {self.total}'+(f'  已选 {len(self._checked)}' if self._checked else ''))
        self.selection_menu.setEnabled(bool(self.candidates) and not self._action_busy)
        first = self.offset+1 if self.total else 0
        self.page_label.setText(f'{first}–{min(self.offset+self.page_size,self.total)} / {self.total}')
        self.prev_button.setEnabled(self.offset>0)
        self.next_button.setEnabled(self.offset+self.page_size<self.total)

    def change_page(self,delta):
        offset = max(0,self.offset+delta*self.page_size)
        if offset>=self.total and self.total:
            return
        self.offset = offset
        self._checked.clear()
        self._candidate_key = ''
        self.refresh(force=True)

    def _check_changed(self,item):
        candidate_id = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState()==Qt.CheckState.Checked:
            self._checked.add(candidate_id)
        else:
            self._checked.discard(candidate_id)
        self.candidate_count.setText(f'全部线索  {self.total}  已选 {len(self._checked)}')

    def _select_page(self):
        self._checked.update(c['id'] for c in self.candidates)
        self._candidate_key = ''
        self._render_candidates()

    def _clear_checked(self):
        self._checked.clear()
        self._candidate_key = ''
        self._render_candidates()

    def _candidate_selected(self,current,previous):
        if current:
            self.load_detail(current.data(Qt.ItemDataRole.UserRole))

    def load_detail(self,candidate_id):
        self._save_reason_draft()
        changed = self.selected_id!=candidate_id
        self.selected_id = candidate_id
        self._detail_request += 1
        sequence,epoch = self._detail_request,self._run_epoch
        if changed:
            self.detail = None
            self._detail_key = ''
            self.tabs.setCurrentIndex(0)
            self._clear_detail()
            self.detail_title.setText('正在读取原始证据…')
        def work():
            client = self._client()
            detail = client.request('GET',f'/api/candidates/{object_id(candidate_id)}')
            try:
                history = client.request('GET',api_path('/api/reviews',run_id=detail['run_id']))
                detail['_history'] = [review for review in history.get('items',[]) if any(change.get('candidate_id')==candidate_id for change in review.get('changes',[]))]
            except ApiError as error:
                detail['_history_error'] = error_text(error)
            return detail
        def done(detail):
            if sequence==self._detail_request and epoch==self._run_epoch:
                self.detail = detail
                self._render_detail()
        def failed(error):
            if sequence==self._detail_request:
                self._error(error)
                self.detail_title.setText('无法读取证据，请重新选择或刷新。')
        self.executor.submit(work,done,failed)

    def _clear_detail(self):
        self.detail_stack.setCurrentIndex(0)
        self.reason_toggle.setChecked(False)
        self.detail_title.setText('选择一条线索，核对证据')
        self.detail_kicker.setText('原文与审核')
        self.facts_label.hide()
        self.source_button.setEnabled(False)
        for layout in (self.evidence_layout,self.analysis_layout,self.reviews_layout):
            clear_layout(layout)
        self.evidence_layout.addWidget(label('原始内容与来源链接会显示在这里。','muted'))
        self.evidence_layout.addStretch(1)
        self.reason.blockSignals(True)
        self.reason.setPlainText(self._draft_reasons.get(self.selected_id or '', ''))
        self.reason.blockSignals(False)
        self._set_review_enabled(False)

    def _render_detail(self):
        detail = self.detail
        if not detail:
            return
        key = json.dumps(detail,ensure_ascii=False,sort_keys=True)
        if key==self._detail_key:
            return
        scrolls = [scroll.verticalScrollBar().value() for scroll in (self.evidence_scroll,self.analysis_scroll,self.reviews_scroll)]
        text_scrolls = [edit.verticalScrollBar().value() for edit in self.evidence_widget.findChildren(QPlainTextEdit)]
        expanded = {section.header.text():section.expanded for section in self.tabs.findChildren(CollapsibleSection)}
        self._detail_key = key
        self.detail_stack.setCurrentIndex(1)
        self.platform_avatar.setText(PLATFORMS.get(detail.get('platform'),'源')[:1])
        self.detail_title.setText(candidate_title(detail))
        evidence_status = {'complete':'证据完整','partial':'证据部分可用','unverified':'尚未核实','demo':'合成演示证据'}.get(detail.get('evidence_status'),'待核实')
        self.detail_kicker.setText(f"{PLATFORMS.get(detail.get('platform'),detail.get('platform','来源'))} · {evidence_status}"+(' · 评论' if detail.get('kind')=='comment' else ''))
        known = [(name,value) for name,value in source_facts(detail,self.run) if value not in (None,'','待确认','身份待核')][:3]
        self.facts_label.setText(' / '.join(f'{name}：{display(value)}' for name,value in known))
        self.facts_label.setVisible(bool(known))
        self.source_button.setEnabled(bool(safe_source_url(detail.get('parent_source_url') or detail.get('source_url'))))
        for layout in (self.evidence_layout,self.analysis_layout,self.reviews_layout):
            clear_layout(layout)
        evidence = detail.get('evidence') or []
        for index,record in enumerate(evidence):
            kind = record.get('kind')
            title = {'post':'帖子原文','comment':'评论原文','website':'网页原文','import':'导入原文'}.get(kind,'来源原文')
            self.evidence_layout.addWidget(label(title+(f' {index+1}' if len(evidence)>1 else '')+'  '+date_text(record.get('posted_at')),'muted'))
            editor = plain(str(record.get('content') or record.get('quote') or '尚未取得原文。'))
            editor.setObjectName('sourceEvidence')
            editor.setAccessibleName(title)
            self.evidence_layout.addWidget(editor)
            metadata = record.get('metadata') or {}
            url = record.get('source_url')
            if kind=='comment' and metadata.get('comment_permalink_available') is False:
                url = detail.get('parent_source_url') or metadata.get('parent_source_url') or url
            button = QPushButton('打开父帖核查上下文' if kind=='comment' and metadata.get('comment_permalink_available') is False else '查看来源')
            button.setEnabled(bool(safe_source_url(url)))
            button.clicked.connect(lambda _checked=False,u=url:self.open_source(u))
            button.setProperty('variant','flat')
            if len(evidence)>1 or url!=(detail.get('parent_source_url') or detail.get('source_url')):
                self.evidence_layout.addWidget(button,0,Qt.AlignmentFlag.AlignLeft)
            else:
                button.deleteLater()
            if kind=='comment':
                comment = [f"评论者：{detail.get('author_handle') or '待核实'}"]
                if metadata.get('parent_title'):
                    comment.append('父帖标题：'+str(metadata['parent_title']))
                matches = metadata.get('matched_keywords') or metadata.get('keyword_matches') or []
                if matches:
                    comment.append('关键词命中：'+'、'.join(str(value) for value in matches)+'（原文匹配，非模型判断）')
                if metadata.get('comment_permalink_available') is False:
                    comment.append('平台未提供独立评论链接；已保留父帖 URL 和评论 ID。')
                self._add_section(self.evidence_layout,'评论上下文与关键词','\n'.join(comment),expanded)
            if record.get('quote') and record.get('quote')!=record.get('content'):
                self._add_section(self.evidence_layout,'模型引用的片段',str(record['quote']),expanded)
            technical = '\n'.join(f'{name}：{display(value)}' for name,value in source_facts(detail,self.run))
            technical += f"\n证据 ID：{record.get('id','')}\n候选版本：{detail.get('version','')}\n采集于：{date_text(record.get('retrieved_at'))}\n内容指纹：{record.get('content_sha256','')}"
            if detail.get('comment_id') or metadata.get('comment_id'):
                technical += '\n评论 ID：'+str(detail.get('comment_id') or metadata['comment_id'])
            self._add_section(self.evidence_layout,f'记录信息 {index+1}',technical,expanded)
        if not evidence:
            self.evidence_layout.addWidget(label('尚未取得原始证据，需要补查来源与原文。','muted'))
        self.evidence_layout.addStretch(1)
        self._render_analysis(detail,expanded)
        reviews = detail.get('_history') or detail.get('reviews') or []
        self.tabs.setTabText(2,f'记录 {len(reviews)}')
        if detail.get('_history_error'):
            self.reviews_layout.addWidget(label('审核历史读取失败：'+detail['_history_error'],'errorBanner'))
        for review in reversed(reviews):
            changes = [change for change in review.get('changes',[]) if change.get('candidate_id')==detail['id']] or ([review] if review.get('decision') else [])
            for change in changes:
                who = {'human':'人工审核','codex':'Codex 审核','dsh':'harness 审核'}.get(review.get('reviewer'),review.get('reviewer','审核'))
                status = '（已撤销）' if review.get('status')=='reverted' else ''
                self.reviews_layout.addWidget(label(f"{who} · {DECISIONS.get(change.get('decision'),'待确认')} {status}",'sectionTitle'))
                self.reviews_layout.addWidget(label(str(change.get('reason') or '未填写理由')))
                self.reviews_layout.addWidget(label(f"{date_text(review.get('created_at'))} · {review.get('id','')}",'muted'))
        if not reviews:
            self.reviews_layout.addWidget(label('还没有审核记录。核对原文后可在下方留下决定。','muted'))
        self.reviews_layout.addStretch(1)
        self.review_label.setText('人工审核  '+DECISIONS.get(detail.get('human_decision'),'未审核'))
        self.reason.blockSignals(True)
        self.reason.setPlainText(self._draft_reasons.get(detail['id'],''))
        self.reason.blockSignals(False)
        if self.reason.toPlainText():self.reason_toggle.setChecked(True)
        self._set_review_enabled(not self._action_busy)
        def restore():
            for scroll,value in zip((self.evidence_scroll,self.analysis_scroll,self.reviews_scroll),scrolls):
                scroll.verticalScrollBar().setValue(value)
            for editor,value in zip(self.evidence_widget.findChildren(QPlainTextEdit),text_scrolls):
                editor.verticalScrollBar().setValue(value)
        QTimer.singleShot(0,restore)

    def _add_section(self,layout,title,text,expanded):
        section = CollapsibleSection(title)
        section.content_layout.addWidget(label(text,'muted'))
        section.set_expanded(expanded.get(title,False))
        layout.addWidget(section)
        return section

    def _render_analysis(self,detail,expanded):
        analysis = detail.get('analysis') or {}
        status = analysis.get('analysis_status') or detail.get('analysis_status')
        if status=='not_requested':
            self.analysis_layout.addWidget(label('未使用本地模型','sectionTitle'))
            self.analysis_layout.addWidget(label('本轮只采集原文，待你或 Codex 审核；关键词仅属于原文匹配。','muted'))
        else:
            self.analysis_layout.addWidget(label('本地初筛 · '+DECISIONS.get(analysis.get('decision') or detail.get('local_decision'),'尚无结论'),'sectionTitle'))
            self.analysis_layout.addWidget(label(str(analysis.get('reason') or detail.get('local_reason') or '尚无分析结果。')))
            window = analysis.get('input_window') or {}
            if window.get('truncated'):
                self.analysis_layout.addWidget(label(f"模型只分析前 {window.get('analyzed_chars')} 字符；原文共 {window.get('original_chars')} 字符，未读部分需要补查。",'warning'))
            if analysis.get('target_match'):
                self._add_section(self.analysis_layout,'定向匹配',json.dumps(analysis['target_match'],ensure_ascii=False,indent=2),expanded)
            facts,unknown = analysis.get('facts') or {},analysis.get('unknown_fields') or []
            target = domestic_target(detail,self.run)
            if target.get('market')=='cn':
                allowed = {'budget','start_date','deadline','author_handle'}
                if any(word in target.get('industry','') for word in ('家装','装修','装饰','整装','全屋','房产')):
                    allowed.update({'property_type','area','renovation_stage'})
                unknown = [key for key in unknown if isinstance(key,str) and key in allowed]
            extracted = '\n'.join(f'{FACT_LABELS.get(key,key)}：{display(value)}' for key,value in facts.items())
            if unknown:
                extracted += '\n待核实：'+'、'.join(FACT_LABELS.get(key,key) for key in unknown)
            if extracted:
                self._add_section(self.analysis_layout,'提取信息与待核实项',extracted,expanded)
            metadata = {key:analysis.get(key) for key in ('model','protocol','prompt_version','analysis_status','citations')}
            self._add_section(self.analysis_layout,'模型运行信息',json.dumps(metadata,ensure_ascii=False,indent=2),expanded)
        for reviewer,title,key in [('codex','Codex 复核','codex_decision'),('human','人工决定','human_decision')]:
            history = next((review for review in (detail.get('_history') or []) if review.get('reviewer')==reviewer and review.get('status')!='reverted'),None)
            reason = next((change.get('reason') for change in (history or {}).get('changes',[]) if change.get('candidate_id')==detail['id']),None)
            self.analysis_layout.addWidget(label(title+' · '+DECISIONS.get(detail.get(key),'等待审核'),'sectionTitle'))
            self.analysis_layout.addWidget(label(str(reason or '核对原文后提交审核意见。'),'muted'))
        self.analysis_layout.addStretch(1)

    def _save_reason_draft(self):
        if self.selected_id:
            self._draft_reasons[self.selected_id] = self.reason.toPlainText()

    def _set_review_enabled(self,enabled):
        for button in self.review_buttons.values():
            button.setEnabled(enabled)
        self.reason.setEnabled(bool(self.detail) and not self._action_busy)
        self.trash_action.setEnabled(bool(self.run) and self.run.get('status') not in ACTIVE_STATUSES and not self._action_busy)

    def submit_review(self,decision):
        if not self.detail or self._action_busy or self._close_checking or self._closing_pending:
            return
        detail = self.detail
        reason = self.reason.toPlainText().strip() or f'人工标记为“{DECISIONS[decision]}”；未填写详细理由。'
        change = {'candidate_id':detail['id'],'base_version':detail['version'],'decision':decision,'reason':reason,'evidence_ids':[e['id'] for e in detail.get('evidence',[])],'tags':[]}
        self._post_review(detail['run_id'],[change],{detail['id']:self.reason.toPlainText()})

    def _post_review(self,run_id,changes,reasons):
        canonical = json.dumps([run_id,changes],ensure_ascii=False,sort_keys=True)
        if not self._pending_review or self._pending_review.get('_key')!=canonical:
            self._pending_review = {'run_id':run_id,'reviewer':'human','operation_id':str(uuid.uuid4()),'changes':changes,'_key':canonical}
        payload = {key:value for key,value in self._pending_review.items() if key!='_key'}
        self._action_busy = True
        self._set_review_enabled(False)
        def done(result):
            self._action_busy = False
            self._pending_review = None
            for candidate_id,text in reasons.items():
                if self._draft_reasons.get(candidate_id)==text:
                    self._draft_reasons.pop(candidate_id,None)
                if candidate_id==self.selected_id and self.reason.toPlainText()==text:
                    self.reason.blockSignals(True)
                    self.reason.clear()
                    self.reason.blockSignals(False)
            self.statusBar().showMessage('审核已保存：'+str(result.get('id','')),10000)
            self._detail_key = ''
            self.refresh(force=True)
            if self.selected_id:
                self.load_detail(self.selected_id)
        def failed(error):
            self._action_busy = False
            self._set_review_enabled(bool(self.detail))
            self._error(error,dialog=True)
            if getattr(error,'code','') in ('CONFLICT','VERSION_CONFLICT'):
                self._pending_review = None
                self.refresh(force=True)
                if self.selected_id:
                    self.load_detail(self.selected_id)
        self.executor.submit(lambda:self._client().request('POST','/api/reviews',payload),done,failed)

    def batch_review(self,decision):
        if self._action_busy or self._close_checking or self._closing_pending:
            return
        selected = [c for c in self.candidates if c['id'] in self._checked]
        if not selected or not self.run:
            QMessageBox.information(self,'选择线索','先勾选需要审核的线索。')
            return
        reason,ok = QInputDialog.getMultiLineText(self,'批量人工审核',f'将 {len(selected)} 条线索标记为“{DECISIONS[decision]}”。审核理由：')
        if not ok:
            return
        reason = reason.strip() or f'人工批量标记为“{DECISIONS[decision]}”；未填写详细理由。'
        run_id = self.run['id']
        self._action_busy = True
        def work():
            client = self._client()
            changes = []
            for candidate in selected:
                detail = client.request('GET',f"/api/candidates/{object_id(candidate['id'])}")
                if detail['version']!=candidate['version']:
                    raise ApiError('CONFLICT','候选已被修改，请刷新后重新审核。')
                changes.append({'candidate_id':detail['id'],'base_version':detail['version'],'decision':decision,'reason':reason,'evidence_ids':[e['id'] for e in detail.get('evidence',[])],'tags':[]})
            return changes
        def done(changes):
            self._action_busy = False
            self._post_review(run_id,changes,{})
        def failed(error):
            self._action_busy = False
            self._error(error,dialog=True)
            self.refresh(force=True)
        self.executor.submit(work,done,failed)

    def control_run(self):
        if not self.run or self._action_busy or self._close_checking or self._closing_pending:
            return
        action = self.control_button.property('runAction')
        if not action:
            return
        run_id = self.run['id']
        self._action_busy = True
        self.control_button.setEnabled(False)
        def done(result):
            self._action_busy = False
            if (self.run or {}).get('id')==run_id:
                self.run = result
                self._render_header()
            self.refresh(force=True)
        def failed(error):
            self._action_busy = False
            self._render_header()
            self._error(error,dialog=True)
        self.executor.submit(lambda:self._client().request('POST',f'/api/runs/{object_id(run_id)}/{action}',{}),done,failed)

    def _dialog(self,dialog):
        self._dialogs.append(dialog)
        try:
            result = dialog.exec()
        finally:
            self._dialogs.remove(dialog)
            if hasattr(dialog,'executor'):
                dialog.executor.shutdown(wait=False)
            dialog.deleteLater()
        self.refresh(force=True)
        return result

    def show_new(self,source='opencli',analysis_mode=None):
        if self._action_busy or self._close_checking or self._closing_pending:
            return
        from .native_dialogs import NewRunDialog
        dialog = NewRunDialog(self.client,self,source=source,analysis_mode=analysis_mode)
        if self._dialog(dialog)!=QDialog.DialogCode.Accepted:
            return
        payload,start_now = dialog.payload,dialog.start_now
        self._action_busy = True
        self.new_button.setEnabled(False)
        def work():
            client = self._client()
            run = client.request('POST','/api/runs',payload)
            start_error = None
            if start_now:
                try:
                    run = client.request('POST',f"/api/runs/{object_id(run['id'])}/start",{})
                except Exception as error:
                    start_error = error_text(error)
            return run,start_error
        def done(result):
            self._action_busy = False
            self.new_button.setEnabled(True)
            run,start_error = result
            self.runs.insert(0,run)
            self._run_list_key = ''
            self._render_runs()
            self.select_run(run['id'])
            if start_error:
                self._error(ValueError('任务已创建，但启动失败：'+start_error),dialog=True)
            else:
                self.statusBar().showMessage('任务已创建'+('并启动。' if start_now else '，可随时开始运行。'),10000)
        def failed(error):
            self._action_busy = False
            self.new_button.setEnabled(True)
            self._error(error,dialog=True)
        self.executor.submit(work,done,failed)

    def show_settings(self):
        if self._close_checking or self._closing_pending:
            return
        from .native_dialogs import SettingsDialog
        dialog = SettingsDialog(self.client,self)
        dialog.saved.connect(lambda data:self.statusBar().showMessage('设置已保存；不会启动或停止模型。',10000))
        self._dialog(dialog)

    def show_history(self):
        if not self.run or self._close_checking or self._closing_pending:
            return
        from .native_dialogs import HistoryDialog
        dialog = HistoryDialog(self.client,self.run['id'],self)
        dialog.changed.connect(self._reviews_changed)
        self._dialog(dialog)

    def show_review_import(self):
        if not self.run or self._action_busy or self._close_checking or self._closing_pending:
            return
        from .native_dialogs import CodexImportDialog
        dialog = CodexImportDialog(self.client,self.run['id'],self)
        if self._dialog(dialog)!=QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload
        self._action_busy = True
        def done(result):
            self._action_busy = False
            self.statusBar().showMessage('审核变更已导入：'+str(result.get('id','')),12000)
            self._reviews_changed()
        def failed(error):
            self._action_busy = False
            self._error(error,dialog=True)
            self._reviews_changed()
        self.executor.submit(lambda:self._client().request('POST','/api/reviews',payload),done,failed)

    def _reviews_changed(self):
        self._detail_key = ''
        self.refresh(force=True)
        if self.selected_id:
            self.load_detail(self.selected_id)

    def export_run(self,format):
        if not self.run:
            return
        run_id = self.run['id']
        filename,_ = QFileDialog.getSaveFileName(self,'导出本轮原文与审核',f'获客平台-{run_id}.{format}',f'{format.upper()} (*.{format})')
        if not filename:
            return
        def work():
            raw,_ = self._client().raw('GET',f'/api/runs/{object_id(run_id)}/export?format={format}')
            Path(filename).write_bytes(raw)
            return filename
        self.executor.submit(work,lambda path:self.statusBar().showMessage('已导出：'+path,12000),lambda error:self._error(error,dialog=True))

    def open_source(self,url):
        safe = safe_source_url(url)
        if not safe:
            self._error(ValueError('来源网址不是有效的 http:// 或 https:// 链接。'),dialog=True)
        elif not QDesktopServices.openUrl(safe):
            self._error(ValueError('系统未能打开来源链接，请检查默认浏览器。'),dialog=True)

    def open_detail_source(self):
        if self.detail:
            self.open_source(self.detail.get('parent_source_url') or self.detail.get('source_url'))

    def show_help(self):
        QMessageBox.information(self,'获客平台',
            '每个任务只运行一轮。新建任务第三步可设“本批最多采集”，默认20条、当前最多50条，所有平台合计。\n'
            '达到上限或本轮来源读完，就结束采集；不会一直等待凑满，也不会自动重复采集。\n'
            '选择本地AI后，会自动完成本批初筛。暂停需等待当前采集或推理步骤结束。\n\n'
            '在“任务管理 / 回收站”可多选整理任务。移入回收站可恢复；永久删除只在回收站确认后执行。\n\n'
            '菜单可导出资料交给Codex复核，原文与审核记录可分别查看。评论是所选帖子的有限样本。\n'
            '模型服务由你现有的控制器管理，本程序只负责连接。')

    def closeEvent(self,event):
        if self._can_close:
            self.refresh_timer.stop()
            self.close_timer.stop()
            self.executor.shutdown(wait=False)
            event.accept()
            return
        event.ignore()
        if self._action_busy:
            self.statusBar().showMessage('正在创建、保存或控制任务，请本次操作完成后再关闭。',12000)
            return
        if self._close_checking or self._closing_pending:
            return
        self._close_checking = True
        def done(data):
            self._close_checking = False
            runs = data.get('items',[]) if isinstance(data,dict) else data
            active = [run for run in runs if run.get('status') in ACTIVE_STATUSES]
            if not active:
                self._finish_close()
                return
            box = QMessageBox(self)
            box.setWindowTitle('任务仍在运行')
            box.setText(f'还有 {len(active)} 个任务正在运行。暂停完成后再关闭窗口。')
            pause = box.addButton('暂停后关闭',QMessageBox.ButtonRole.AcceptRole)
            box.addButton('取消',QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton()!=pause:
                return
            self._closing_pending = True
            self.refresh_timer.stop()
            self.centralWidget().setEnabled(False)
            self.statusBar().showMessage('正在请求暂停；等待当前采集 / 推理步骤结束，不会关闭模型。')
            ids = [run['id'] for run in active if run.get('status')=='running']
            def pause_all():
                client = self._client()
                for run_id in ids:
                    client.request('POST',f'/api/runs/{object_id(run_id)}/pause',{})
            def paused(_):
                self.close_timer.start()
                self._check_paused_close()
            def failed(error):
                self._closing_pending = False
                self.centralWidget().setEnabled(True)
                self.refresh_timer.start()
                self._error(error,dialog=True)
            self.executor.submit(pause_all,paused,failed)
        def failed(error):
            self._close_checking = False
            self._error(error,dialog=True)
        self.executor.submit(lambda:self._client().request('GET','/api/runs'),done,failed)

    def _check_paused_close(self):
        if self._close_checking:
            return
        self._close_checking = True
        def done(data):
            self._close_checking = False
            runs = data.get('items',[]) if isinstance(data,dict) else data
            if not any(run.get('status') in ACTIVE_STATUSES for run in runs):
                self._finish_close()
        def failed(error):
            self._close_checking = False
            self.close_timer.stop()
            self._closing_pending = False
            self.centralWidget().setEnabled(True)
            self.refresh_timer.start()
            self._error(error,dialog=True)
        self.executor.submit(lambda:self._client().request('GET','/api/runs'),done,failed)

    def _finish_close(self):
        if self._action_busy:
            self._close_checking = self._closing_pending = False
            self.close_timer.stop()
            self.centralWidget().setEnabled(True)
            self.refresh_timer.start()
            self.statusBar().showMessage('正在创建、保存或控制任务，请本次操作完成后再关闭。',12000)
            return
        self._can_close = True
        self.close()


def run_native(data_dir: Path, service_info: dict | None = None) -> int:
    """Reuse the launcher's QApplication and enter the native event loop."""
    app = QApplication.instance() or QApplication([])
    app.setApplicationName('获客平台')
    app.setOrganizationName('LeadWorkbench')
    apply_dark_theme(app)
    root = Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent.parent))
    icon_path = root/'static'/'app.ico'
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = NativeWindow(Path(data_dir),service_info)
    window.show()
    app._acquisition_window = window
    result = app.exec()
    window.executor.shutdown(wait=True)
    return result
