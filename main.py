"""phisap - Phigros autoplay assistant
UI built with PyQt-Fluent-Widgets (Win11 Fluent Design).
"""
from __future__ import annotations

import configparser
import json
import locale
import os
import re
import subprocess
import sys
import time
import traceback
from threading import Thread

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QTextCursor, QFont
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QMessageBox, QFileDialog, QTreeWidget, QTreeWidgetItem, QHeaderView

from qfluentwidgets import (
    MSFluentWindow, FluentIcon as FIF,
    SubtitleLabel, CaptionLabel, StrongBodyLabel,
    PrimaryPushButton, PushButton, ToolButton, SwitchButton,
    SearchLineEdit, PlainTextEdit,
    ComboBox, DoubleSpinBox, CheckBox,
    SettingCardGroup, SettingCard,
    InfoBar, InfoBarPosition,
    IndeterminateProgressBar,
    ScrollArea,
    setTheme, Theme, setThemeColor,
)

from rich.console import Console

from algo.algo_base import (
    TouchEvent, TouchAction, load_from_json, export_to_json, PLAN_CACHE_SUFFIX,
    first_note_ms, manual_start_plan,
)
from chart import Chart
from control import DeviceController, max_touch_points, VIDEO_MAX_FPS
from player import run_player, raise_timer_resolution
from rpe import detect_kind, rpe_to_official_v3
from downloader import Downloader, SOURCES, DEFAULT_SOURCE


ALGORITHMS = ('algo3', 'algo3f', 'algo1', 'algo2')

_KNOWN_DIFFICULTIES = ('SPB', 'INB', 'HDB', 'ATB', 'SP', 'IN', 'HD', 'AT', 'DT')
_CHART_TOKEN_RE = re.compile(r'chart[_\s\-#]?([a-z]+)', re.IGNORECASE)


def chart_difficulty(filename: str) -> str | None:
    base = os.path.basename(filename)
    if base.lower() == 'chart.json':
        return 'SP'
    m = _CHART_TOKEN_RE.search(base)
    if not m:
        return None
    token = m.group(1).upper()
    if token in _KNOWN_DIFFICULTIES:
        return token
    if token[:-1] in ('AT', 'IN', 'HD', 'SP') and token.endswith('B'):
        return token[:-1]
    return None


_FIRST_NOTE_CACHE: dict[tuple, int] = {}


def first_note_ms_from_path(path: str | None) -> int:
    '''谱面第一个音符的判定时间(ms)。

    这个函数会被 _start_playback 在每次点"开始演奏"时调用, 而它的实现是
    "读盘 -> json.load -> Chart.from_dict -> 遍历所有判定线和音符取最小值"。
    一张2.5MB的谱面实测就要42ms, 大谱面(3000+判定线)轻松上几百毫秒,
    而且全部发生在GUI线程、发生在用户点完按钮之后 —— 表现为"点了开始要等
    一会儿才动"。所以这里按 (路径, mtime, 大小) 缓存, 同一张谱面只算一次。
    '''
    if not path or not os.path.exists(path):
        return 0
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return 0
    v = _FIRST_NOTE_CACHE.get(key)
    if v is not None:
        return v
    try:
        with open(path, 'r', encoding='utf-8-sig') as f:
            ch = Chart.from_dict(json.load(f))
        v = first_note_ms(ch)
        v = v if v else 0
    except Exception:
        v = 0
    if len(_FIRST_NOTE_CACHE) > 64:
        _FIRST_NOTE_CACHE.clear()
    _FIRST_NOTE_CACHE[key] = v
    return v


def find_chart_path(song_id: str, diff: str) -> str | None:
    folder = os.path.join('./Assets/Tracks', song_id)
    if not os.path.isdir(folder):
        return None
    for f in os.listdir(folder):
        if chart_difficulty(f) == diff and f.endswith('.json') and PLAN_CACHE_SUFFIX not in f:
            return os.path.join(folder, f)
    return None


def _report_crash(exctype, value, tb):
    err = ''.join(traceback.format_exception(exctype, value, tb))
    sys.stderr.write(err)
    try:
        with open('phisap_error.log', 'a', encoding='utf-8') as f:
            f.write('\n' + '=' * 60 + '\n' + err)
        QMessageBox.critical(None, f'{exctype.__name__}: {value}',
                             '程序发生了未处理的异常,详细信息已保存到 phisap_error.log')
    except Exception:
        pass


# ============== Plan Thread ==============
class PlanThread(QThread):
    finished_ok = pyqtSignal(str, dict)
    failed = pyqtSignal(str)
    log_line = pyqtSignal(str)

    def __init__(self, chart_path, algo, plan_path, max_pointers):
        super().__init__()
        self.chart_path = chart_path
        self.algo = algo
        self.plan_path = plan_path
        self.max_pointers = max_pointers

    def run(self):
        try:
            import importlib, io
            from rich.console import Console as RConsole
            buf = io.StringIO()
            cap = RConsole(file=buf, highlight=False, force_terminal=False)
            with open(self.chart_path, 'r', encoding='utf-8-sig') as f:
                chart = Chart.from_dict(json.load(f))
            mod = importlib.import_module(f'algo.{self.algo}')
            ans = mod.solve(chart, cap, self.max_pointers)
            with open(self.plan_path, 'w', encoding='utf-8') as fp:
                export_to_json(ans, fp)
            self.log_line.emit(buf.getvalue())
            self.finished_ok.emit(self.plan_path, ans)
        except Exception:
            self.failed.emit(traceback.format_exc())


# 手动开始时, 打歌时钟比"现在"提前这么多(秒)。注入触控有延迟(经 adb 转发 +
# scrcpy-server 处理), 游戏是在收到点击之后才开始计时的; 这里留一点提前量,
# 让第一批事件(按下按钮那一戳)落在游戏开始之前而不是之后。
MANUAL_START_LEAD = 0.01


def _active_pids_of(adapted) -> set[int]:
    '''从适配后的事件表算出"当前处于按下状态的触点集合", 停止时用来全部抬起。'''
    active: set[int] = set()
    for _, evs in adapted:
        for ev in evs:
            if ev.action is TouchAction.DOWN:
                active.add(ev.pointer)
            elif ev.action is TouchAction.UP:
                active.discard(ev.pointer)
    return active


# ============== 主页面 ==============
class MainPage(ScrollArea):
    # ---- 跨线程通知GUI线程的信号 ----
    # 播放线程/视觉等待线程都是普通 threading.Thread, 不能直接碰界面。
    # 之前用 QMetaObject.invokeMethod(self, '_xxx', QueuedConnection) 是错的:
    # 普通Python方法没注册进元对象, invokeMethod 找不到, 直接抛
    # "No such method MainPage::_reset_go()", 导致播放结束后按钮永远停在"停止演奏"、
    # _running 永远为True, 用户再也停不下来。pyqtSignal 才是跨线程调GUI的正确方式。
    playback_finished = pyqtSignal()   # 播放结束/异常 -> _reset_go
    vauto_abort = pyqtSignal()         # 视觉等待被取消 -> _vauto_abort
    vauto_launch = pyqtSignal()        # 视觉检测到跳变 -> _vauto_launch
    log_line = pyqtSignal(str)         # 任意线程写日志 -> 回GUI线程刷新PlainTextEdit

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('mainPage')
        # 信号连接必须在 _build() 之前或之中做好(_build里创建的控件由下面这些槽操作)
        self.log_line.connect(self._append_log)
        self.console = Console(highlight=False)
        self.downloader = Downloader(source=self._read_cached_source() or 'jsdelivr')
        self._dl_index_loaded = False
        self.controller: DeviceController | None = None
        self.plan_path: str | None = None
        self._raw_ans: dict | None = None
        self._all_song_values: list[str] = []
        self._running = False
        self._player_thread: Thread | None = None
        self._start_time: float = 0.0
        # 坐标适配结果缓存: (key, adapted, active)。适配只依赖(规划, 屏幕尺寸),
        # 所以可以在后台提前算好, 点"开始演奏"时直接取, 不在GUI线程里卡几百毫秒。
        self._adapt_cache: tuple | None = None
        self._adapt_gen = 0
        # 播放"代际": 每启动一次播放就+1。worker 记住自己属于哪一代,
        # 发现代际变了就立刻停止发送——防止上一次没退干净的线程继续往设备上戳。
        self._playback_gen = 0

        # Windows 上把计时器精度提到1ms: 否则 time.sleep(0.001) 实际睡15.6ms,
        # 打歌循环每批事件都会被睡过头, 系统性晚发最多15ms(见 player.raise_timer_resolution)
        if not raise_timer_resolution():
            self._timer_res_warned = True
        self._build()
        self._load_font_scale()
        self.detect_adb_devices()
        self.refresh_songs()
        self.load_cache('./cache')
        # 启动时尝试读磁盘缓存(不联网),更新状态文字
        try:
            self.downloader.load_index(force=False, use_cache=True)
            if self.downloader.song_index:
                self._dl_index_loaded = True
        except Exception:
            pass
        self._dl_refresh_status_text()

    def _build(self):
        self.scroll_widget = QWidget()
        self.setWidget(self.scroll_widget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer = QVBoxLayout(self.scroll_widget)
        outer.setContentsMargins(32, 24, 32, 24)
        outer.setSpacing(16)

        # 标题
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        t = SubtitleLabel('Phisap')
        title_box.addWidget(t)
        s = CaptionLabel('Phigros 自动演奏助手')
        title_box.addWidget(s)
        outer.addLayout(title_box)

        # ---- 曲目组 ----
        song_group = SettingCardGroup('曲目')

        self.search_card = SettingCard(FIF.SEARCH, '搜索曲目', '在本地谱面库中按 ID 过滤')
        self.search_edit = SearchLineEdit()
        self.search_edit.setPlaceholderText('输入关键字实时过滤...')
        self.search_edit.setFixedWidth(280)
        self.search_edit.textChanged.connect(self._apply_search_filter)
        self.refresh_btn = ToolButton(FIF.SYNC)
        self.refresh_btn.clicked.connect(self.refresh_songs)
        self.local_case_btn = PushButton('Aa')
        self.local_case_btn.setCheckable(True)
        self.local_case_btn.setToolTip('区分大小写')
        self.local_case_btn.setFixedWidth(40)
        self.local_case_btn.clicked.connect(self._apply_search_filter)
        lay = QHBoxLayout()
        lay.setSpacing(6)
        lay.addWidget(self.search_edit)
        lay.addWidget(self.local_case_btn)
        lay.addWidget(self.refresh_btn)
        w = QWidget(); w.setLayout(lay)
        self.search_card.hBoxLayout.addWidget(w, 0, Qt.AlignRight)
        self.search_card.hBoxLayout.addSpacing(16)
        song_group.addSettingCard(self.search_card)

        self.song_card = SettingCard(FIF.MUSIC_FOLDER, '曲目 ID', '选择歌曲')
        self.song_box = ComboBox()
        self.song_box.setMinimumWidth(320)
        self.song_box.currentTextChanged.connect(self.song_selected)
        self.song_card.hBoxLayout.addWidget(self.song_box, 0, Qt.AlignRight)
        self.song_card.hBoxLayout.addSpacing(16)
        song_group.addSettingCard(self.song_card)

        self.diff_card = SettingCard(FIF.TILES, '难度', '选择难度')
        self.diff_box = ComboBox()
        self.diff_box.setMinimumWidth(160)
        self.diff_box.currentTextChanged.connect(self.difficulty_selected)
        self.diff_card.hBoxLayout.addWidget(self.diff_box, 0, Qt.AlignRight)
        self.diff_card.hBoxLayout.addSpacing(16)
        song_group.addSettingCard(self.diff_card)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_import = PushButton(FIF.DOWNLOAD, '导入谱面')
        btn_import.clicked.connect(self.import_songs)
        btn_row.addWidget(btn_import)
        btn_dl = PushButton(FIF.GLOBE, '跳转到在线下载')
        btn_dl.clicked.connect(self._scroll_to_download)
        btn_row.addWidget(btn_dl)
        btn_row.addStretch(1)
        song_group.vBoxLayout.addLayout(btn_row)
        outer.addWidget(song_group)

        # ---- 规划组 ----
        plan_group = SettingCardGroup('规划')
        self.algo_card = SettingCard(FIF.ROBOT, '算法', '选择规划算法')
        self.algo_box = ComboBox()
        self.algo_box.addItems(ALGORITHMS)
        self.algo_box.setMinimumWidth(180)
        self.algo_card.hBoxLayout.addWidget(self.algo_box, 0, Qt.AlignRight)
        self.algo_card.hBoxLayout.addSpacing(16)
        plan_group.addSettingCard(self.algo_card)

        self.delay_card = SettingCard(FIF.STOP_WATCH, '偏移 (ms)', '正值延后,负值提前')
        self.delay_spin = DoubleSpinBox()
        self.delay_spin.setRange(-500, 500)
        self.delay_spin.setSingleStep(5)
        self.delay_spin.setDecimals(1)
        self.delay_spin.setValue(0)
        self.delay_spin.setFixedWidth(140)
        self.delay_card.hBoxLayout.addWidget(self.delay_spin, 0, Qt.AlignRight)
        self.delay_card.hBoxLayout.addSpacing(16)
        plan_group.addSettingCard(self.delay_card)

        self.auto_card = SettingCard(FIF.UPDATE, '自动开始', '第一个音符自动触发,无需手动点击')
        self.auto_switch = SwitchButton()
        self.auto_switch.setChecked(False)   # 默认关闭: 需要先把偏移调准
        self.auto_switch.setOffText('关')
        self.auto_switch.setOnText('开')
        self.auto_card.hBoxLayout.addWidget(self.auto_switch, 0, Qt.AlignRight)
        self.auto_card.hBoxLayout.addSpacing(16)
        plan_group.addSettingCard(self.auto_card)

        # 视觉自动开始: 程序自己盯着屏幕, 检测Phigros从准备界面跳进演奏界面的瞬间,
        # 完全不需要你对齐第一拍, 每首歌(不管第一个音符在第几秒)都全自动
        self.vauto_card = SettingCard(FIF.VIEW, '视觉自动开始 (实验性, 默认关)',
                                      '靠屏幕帧体积突变猜进入演奏的时机, 不保证可靠, 默认关闭')
        self.vauto_switch = SwitchButton()
        self.vauto_switch.setChecked(False)   # 默认关闭: 纯启发式检测, 未经验证
        self.vauto_switch.setOffText('关')
        self.vauto_switch.setOnText('开')
        self.vauto_switch.checkedChanged.connect(self._on_vauto_changed)
        self.vauto_card.hBoxLayout.addWidget(self.vauto_switch, 0, Qt.AlignRight)
        self.vauto_card.hBoxLayout.addSpacing(16)
        plan_group.addSettingCard(self.vauto_card)
        self._vauto_event = None
        self._vauto_fire_time = 0.0
        self._vauto_waiting = False

        plan_btn_row = QHBoxLayout()
        plan_btn_row.setSpacing(8)
        self.plan_btn = PrimaryPushButton(FIF.PLAY_SOLID, '生成规划')
        self.plan_btn.clicked.connect(self.start_planning)
        plan_btn_row.addWidget(self.plan_btn)
        self.export_btn = PushButton(FIF.SAVE_AS, '导出 JSON')
        self.export_btn.clicked.connect(self.export_plan)
        self.export_btn.setEnabled(False)
        plan_btn_row.addWidget(self.export_btn)
        plan_btn_row.addStretch(1)
        plan_group.vBoxLayout.addLayout(plan_btn_row)
        outer.addWidget(plan_group)

        # ---- 设备组 ----
        dev_group = SettingCardGroup('设备')
        self.dev_card = SettingCard(FIF.PHONE, 'ADB 设备', '选择设备')
        self.devices_box = ComboBox()
        self.devices_box.setMinimumWidth(320)
        self.dev_card.hBoxLayout.addWidget(self.devices_box, 0, Qt.AlignRight)
        self.dev_card.hBoxLayout.addSpacing(16)
        dev_group.addSettingCard(self.dev_card)

        # ---- 触控后端: scrcpy / MaaTouch ----
        # 人手点模拟器屏幕会打断自动演奏(InputDispatcher 发 ACTION_CANCEL),
        # 两个后端在不同模拟器上表现不一样, 所以让用户自己选。
        self.backend_box = ComboBox()
        self.backend_box.addItems(['scrcpy (16触点, 支持视觉自动开始)',
                                   'MaaTouch (10触点, 模拟器上可能不被打断)'])
        self.backend_box.setMinimumWidth(320)
        saved_backend = self._cache_get('touch_backend', 'scrcpy')
        self.backend_box.setCurrentIndex(1 if saved_backend == 'maatouch' else 0)
        self.backend_box.currentIndexChanged.connect(self._on_backend_changed)
        self.backend_card = SettingCard(FIF.TILES, '触控后端', '切换后需重新连接设备')
        self.backend_card.hBoxLayout.addWidget(self.backend_box, 0, Qt.AlignRight)
        self.backend_card.hBoxLayout.addSpacing(16)
        dev_group.addSettingCard(self.backend_card)

        self.dev_refresh_btn = PushButton(FIF.SYNC, '刷新设备')
        self.dev_refresh_btn.clicked.connect(self.detect_adb_devices)
        dev_row = QHBoxLayout()
        dev_row.addWidget(self.dev_refresh_btn)
        dev_row.addStretch(1)
        self.dev_badge = CaptionLabel('未连接')
        dev_row.addWidget(self.dev_badge)
        dev_group.vBoxLayout.addLayout(dev_row)
        outer.addWidget(dev_group)

        # ---- 在线下载组 ----
        dl_group = SettingCardGroup('在线谱面下载')
        dl_search_card = SettingCard(FIF.SEARCH, '搜索', '输入曲名/曲师/ID,回车搜索')
        self.dl_search = SearchLineEdit()
        self.dl_search.setPlaceholderText('搜索在线谱面...')
        self.dl_search.returnPressed.connect(self._dl_do_search)
        self.dl_diff = ComboBox()
        self.dl_diff.addItems(['EZ', 'HD', 'IN', 'AT', 'SP'])
        self.dl_diff.setCurrentText('AT')
        self.dl_diff.setFixedWidth(90)
        self.dl_case_btn = PushButton('Aa')
        self.dl_case_btn.setCheckable(True)
        self.dl_case_btn.setToolTip('区分大小写(默认不区分)')
        self.dl_case_btn.setFixedWidth(40)
        dl_row = QHBoxLayout()
        dl_row.setSpacing(6)
        dl_row.addWidget(self.dl_search, stretch=1)
        dl_row.addWidget(self.dl_diff)
        dl_row.addWidget(self.dl_case_btn)
        dl_row.setAlignment(Qt.AlignRight)
        dl_search_card.hBoxLayout.addLayout(dl_row, 1)
        dl_search_card.hBoxLayout.addSpacing(16)
        dl_group.addSettingCard(dl_search_card)

        # 搜索结果用 PushSettingCard 列表不现实,用 BodyLabel + 自定义可滚动列表
        self.dl_status = CaptionLabel('点击搜索开始加载索引')
        dl_status_row = QHBoxLayout()
        dl_status_row.addWidget(self.dl_status, stretch=1)
        self.dl_update_btn = PushButton(FIF.SYNC, '更新索引')
        self.dl_update_btn.clicked.connect(self._dl_update_index)
        dl_status_row.addWidget(self.dl_update_btn)
        self.dl_clear_btn = PushButton(FIF.CANCEL, '清除缓存')
        self.dl_clear_btn.clicked.connect(self._dl_clear_cache)
        dl_status_row.addWidget(self.dl_clear_btn)
        dl_group.vBoxLayout.addLayout(dl_status_row)
        self.dl_progress = IndeterminateProgressBar()
        self.dl_progress.hide()
        dl_group.vBoxLayout.addWidget(self.dl_progress)
        self.dl_list = QTreeWidget()
        self.dl_list.setObjectName('dlResultList')  # 供QSS精确匹配
        self.dl_list.setColumnCount(3)
        self.dl_list.setHeaderLabels(['曲名', '曲师', '难度'])
        self.dl_list.setAlternatingRowColors(True)
        self.dl_list.setRootIsDecorated(False)
        self.dl_list.setFixedHeight(200)
        self.dl_list.setSelectionBehavior(QTreeWidget.SelectRows)
        self.dl_list.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.dl_list.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.dl_list.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        dl_group.vBoxLayout.addWidget(self.dl_list)
        dl_btn_row = QHBoxLayout()
        dl_btn_row.setSpacing(8)
        self.dl_btn = PrimaryPushButton(FIF.DOWNLOAD, '下载选中')
        self.dl_btn.clicked.connect(self._dl_do_download)
        self.dl_btn.setEnabled(False)
        dl_btn_row.addWidget(self.dl_btn)
        dl_btn_row.addStretch(1)
        dl_group.vBoxLayout.addLayout(dl_btn_row)
        self.dl_list.itemSelectionChanged.connect(lambda: self.dl_btn.setEnabled(bool(self.dl_list.selectedItems())))
        self.dl_list.itemDoubleClicked.connect(lambda *_: self._dl_do_download())
        outer.addWidget(dl_group)

        # ---- 设置组 ----
        setting_group = SettingCardGroup('设置')
        self.font_card = SettingCard(FIF.FONT, '字体缩放', '调整界面字体大小(重启后生效)')
        self.font_box = ComboBox()
        self.font_box.addItems(['小 (90%)', '标准 (100%)', '大 (115%)', '特大 (130%)'])
        self.font_box.setCurrentIndex(1)
        self.font_box.currentIndexChanged.connect(self._on_font_changed)
        self.font_card.hBoxLayout.addWidget(self.font_box, 0, Qt.AlignRight)
        self.font_card.hBoxLayout.addSpacing(16)
        setting_group.addSettingCard(self.font_card)

        # 下载源
        self.source_card = SettingCard(FIF.GLOBE, '下载镜像源', '国内无法连接GitHub时切换到jsDelivr/GHProxy')
        self.source_box = ComboBox()
        self._source_keys = list(SOURCES.keys())
        for key in self._source_keys:
            self.source_box.addItem(SOURCES[key]['label'])
        saved_src = self._read_cached_source() or DEFAULT_SOURCE
        try:
            idx = self._source_keys.index(saved_src) if saved_src in self._source_keys else 1
        except ValueError:
            idx = 1
        self.source_box.setCurrentIndex(idx)
        self.source_box.currentIndexChanged.connect(self._on_source_changed)
        self.source_box.setMinimumWidth(240)
        self.source_card.hBoxLayout.addWidget(self.source_box, 0, Qt.AlignRight)
        self.source_card.hBoxLayout.addSpacing(16)
        setting_group.addSettingCard(self.source_card)

        self.help_card = SettingCard(FIF.HELP, '使用说明', '自动开始原理 / iOS 支持说明')
        help_lay = QHBoxLayout()
        help_lay.setSpacing(6)
        btn_auto = PushButton(FIF.PLAY, '自动开始说明')
        btn_auto.clicked.connect(self._help_auto_start)
        help_lay.addWidget(btn_auto)
        btn_ios = PushButton(FIF.PHONE, 'iOS 版说明')
        btn_ios.clicked.connect(self._help_ios)
        help_lay.addWidget(btn_ios)
        help_lay.addStretch(1)
        self.help_card.hBoxLayout.addLayout(help_lay, 1)
        self.help_card.hBoxLayout.addSpacing(16)
        setting_group.addSettingCard(self.help_card)

        self.about_card = SettingCard(FIF.INFO, '关于', 'Phisap by FYX')
        setting_group.addSettingCard(self.about_card)
        outer.addWidget(setting_group)

        # ---- 同步 & 播放 ----
        play_group = SettingCardGroup('同步与演奏')
        self.sync_btn = PushButton(FIF.SYNC, '计时器同步 (第一拍按下)')
        self.sync_btn.setFixedHeight(40)
        self.sync_btn.clicked.connect(self.sync_ms)
        play_group.vBoxLayout.addWidget(self.sync_btn)

        self.go_btn = PrimaryPushButton(FIF.PLAY, '开始演奏')
        self.go_btn.setFixedHeight(52)
        self.go_btn.clicked.connect(self.run)
        self.go_btn.setEnabled(False)
        play_group.vBoxLayout.addWidget(self.go_btn)
        outer.addWidget(play_group)

        # ---- 日志 ----
        outer.addWidget(StrongBodyLabel('日志'))
        self.log_view = PlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFixedHeight(160)
        outer.addWidget(self.log_view)

        outer.addStretch(1)

        # ---- 跨线程信号 -> 槽 ----
        self.playback_finished.connect(self._reset_go)
        self.vauto_abort.connect(self._vauto_abort)
        self.vauto_launch.connect(self._vauto_launch)

    # --- 辅助:给SettingCard右侧放自定义控件 ---
    # (直接操作 card.hBoxLayout,无需封装)
    def log(self, msg, level=None):
        '''写日志。可能被播放线程等非GUI线程调用, 所以走信号回到GUI线程再刷新控件,
        否则就是跨线程直接操作QPlainTextEdit(会触发 QTextCursor/QTextBlock
        相关的 Qt 警告, 且严格来说是不安全的)。'''
        self.log_line.emit(str(msg))

    def _append_log(self, msg):
        '''(GUI线程) 真正刷新日志控件'''
        self.log_view.appendPlainText(msg)
        self.log_view.moveCursor(QTextCursor.End)

    # --- 曲目/难度 ---
    def refresh_songs(self):
        try:
            self._all_song_values = sorted(os.listdir('./Assets/Tracks'))
        except FileNotFoundError:
            self._all_song_values = []
            os.makedirs('./Assets/Tracks', exist_ok=True)
        if not self._all_song_values:
            InfoBar.warning('谱面库为空', '请导入谱面或使用在线下载',
                            parent=self.window(), position=InfoBarPosition.TOP, duration=4000)
        self._apply_search_filter()

    def _apply_search_filter(self):
        kw = self.search_edit.text().strip()
        case = self.local_case_btn.isChecked()
        if not kw:
            vals = list(self._all_song_values)
        elif case:
            vals = [s for s in self._all_song_values if kw in s]
        else:
            kw_l = kw.lower()
            vals = [s for s in self._all_song_values if kw_l in s.lower()]
        cur = self.song_box.currentText()
        self.song_box.blockSignals(True)
        self.song_box.clear()
        self.song_box.addItems(vals)
        self.song_box.blockSignals(False)
        if cur in vals:
            self.song_box.setCurrentText(cur)
        else:
            self.diff_box.clear()

    def song_selected(self, sid):
        self.diff_box.clear()
        if not sid:
            return
        folder = os.path.join('./Assets/Tracks', sid)
        diffs = []
        if not os.path.isdir(folder):
            return
        for f in os.listdir(folder):
            d = chart_difficulty(f)
            if d and d not in diffs:
                diffs.append(d)
        order = ['AT', 'IN', 'HD', 'EZ', 'SP', 'DT', 'SPB', 'INB', 'HDB', 'ATB']
        diffs.sort(key=lambda d: order.index(d) if d in order else 99)
        self.diff_box.addItems(diffs)
        if diffs:
            pref = next((d for d in ('AT', 'IN') if d in diffs), diffs[0])
            self.diff_box.setCurrentText(pref)
        self.difficulty_selected(self.diff_box.currentText())

    def difficulty_selected(self, diff):
        self.plan_path = None
        self._raw_ans = None
        self.export_btn.setEnabled(False)
        self.go_btn.setEnabled(False)
        sid = self.song_box.currentText()
        if not sid or not diff:
            return
        folder = os.path.join('./Assets/Tracks', sid)
        cand = os.path.join(folder, f'Chart_{diff}{PLAN_CACHE_SUFFIX}')
        if (not os.path.exists(cand)) and diff == 'SP':
            cand = os.path.join(folder, f'Chart{PLAN_CACHE_SUFFIX}')
        if os.path.exists(cand):
            self.plan_path = cand
            try:
                with open(cand, 'r', encoding='utf-8') as f:
                    self._raw_ans = load_from_json(json.load(f))
            except Exception:
                self._raw_ans = None
            self.export_btn.setEnabled(True)
            self.go_btn.setEnabled(self.controller is not None)
            self.log(f'已载入缓存规划: {os.path.basename(cand)}')
            # 提前在后台把坐标适配算好(要100多ms), 用户点"开始演奏"时直接取
            self._refresh_adapted()

    def _find_chart_path(self):
        return find_chart_path(self.song_box.currentText(), self.diff_box.currentText())

    # ---- 坐标适配(重活, 不能放在GUI线程里) ----
    def _adapt_key(self):
        '''适配结果的缓存键: 规划对象 + 屏幕尺寸。任一变化缓存即失效。'''
        c = self.controller
        if c is None or not self._raw_ans:
            return None
        return (id(self._raw_ans), c.device_width, c.device_height)

    def _build_adapted(self):
        '''把规划坐标适配到当前屏幕 + PID偏移, 同时算出"当前按下的触点集合"(停止时全部UP用)。

        一张谱面有5万多个时间点, 逐个 map_to/_replace 实测要138ms。
        这段以前直接在GUI线程的 _start_playback 里跑, 用户点完"开始演奏"界面要卡
        几百毫秒才动 —— 这就是之前一直被当成"延迟1秒"的那个卡顿, 和视频参数无关。
        '''
        ans = self._raw_ans
        dw, dh = self.controller.device_width, self.controller.device_height
        w, h = dw, dh
        s = min(w / 1280, h / 720)
        xo = (w - 1280 * s) / 2
        yo = (h - 720 * s) / 2
        sx, sy = dw / w, dh / h
        DOWN, UP = TouchAction.DOWN, TouchAction.UP
        adapted = []
        active: set[int] = set()
        for ts in sorted(ans.keys()):
            batch = []
            for ev in ans[ts]:
                mapped = ev.map_to(xo * sx, yo * sy, s * sx, s * sy)
                new_pid = mapped.pointer + self.PID_OFFSET
                nev = mapped._replace(pointer=new_pid)
                batch.append(nev)
                if nev.action is DOWN:
                    active.add(new_pid)
                elif nev.action is UP:
                    active.discard(new_pid)
            adapted.append((ts, batch))
        return adapted, active

    def _refresh_adapted(self):
        '''后台预计算适配结果。选歌/生成规划/连接设备后调用, 等用户点开始时早就备好了。'''
        key = self._adapt_key()
        if key is None:
            self._adapt_cache = None
            return
        self._adapt_gen += 1
        my_gen = self._adapt_gen

        def work():
            try:
                res = self._build_adapted()
            except Exception:
                return          # 算失败就留着旧缓存, 播放时会同步重算
            if my_gen == self._adapt_gen:
                self._adapt_cache = (key,) + res

        Thread(target=work, daemon=True).start()

    def _adapted_now(self):
        '''取适配结果。缓存没算好就当场算(慢但保证正确), 绝不在GUI线程里干等。'''
        key = self._adapt_key()
        if key is None:
            return None
        c = self._adapt_cache
        if c is not None and c[0] == key:
            return c[1], c[2]
        res = self._build_adapted()
        self._adapt_cache = (key,) + res
        return res

    def _scroll_to_download(self):
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    # --- 在线下载 ---
    def _dl_set_status(self, msg, level=None):
        self.dl_status.setText(msg)

    def _dl_refresh_status_text(self):
        n = len(self.downloader.song_index)
        cached = self.downloader.has_cached_index()
        if n > 0:
            picked = self.downloader.src['label']
            used = SOURCES.get(self.downloader.source_used, {}).get('label', picked)
            # 实际来源和用户选的不一致时明确说出来, 避免"我明明选了jsDelivr却还是走github"
            note = '' if self.downloader.source_used == self.downloader.source else f'（实际走: {used}）'
            self._dl_set_status(f'索引就绪,共 {n} 首（所选源: {picked}{note}）')
        elif cached:
            self._dl_set_status('发现本地缓存,点击"更新索引"可重新下载')
        else:
            self._dl_set_status('无本地索引,请点"更新索引"按钮下载(国内请先切到jsDelivr)')

    def _dl_ensure_index(self, callback):
        if self._dl_index_loaded and self.downloader.song_index:
            callback()
            return
        # 优先读磁盘缓存(不联网)
        try:
            self.downloader.load_index(force=False, use_cache=True)
        except Exception:
            pass
        if self.downloader.song_index:
            self._dl_index_loaded = True
            self._dl_refresh_status_text()
            callback()
            return
        # 没有缓存,提示用户手动点更新
        self._dl_refresh_status_text()
        InfoBar.warning('无索引缓存', '请先点"更新索引"按钮下载曲目索引（国内请先在设置切换到jsDelivr源）',
                        parent=self.window(), duration=4000, position=InfoBarPosition.TOP)

    def _dl_update_index(self):
        """手动点按钮:强制联网拉索引"""
        self.dl_progress.show()
        self.dl_update_btn.setEnabled(False)
        self._dl_set_status(f'正在从 {self.downloader.src["label"]} 拉取索引...')

        class T(QThread):
            done = pyqtSignal(bool, str)
            def __init__(self, dl): super().__init__(); self.dl = dl
            def run(self):
                try:
                    self.dl.load_index(force=True, use_cache=False)
                    self.done.emit(True, f'索引就绪,共 {len(self.dl.song_index)} 首（源: {self.dl.src["label"]}）')
                except Exception as e:
                    self.done.emit(False, str(e))

        self._dl_updatet = T(self.downloader)
        def after(ok, msg):
            self.dl_progress.hide()
            self.dl_update_btn.setEnabled(True)
            self._dl_set_status(msg)
            if ok:
                self._dl_index_loaded = True
                InfoBar.success('索引已更新', msg, parent=self.window(), duration=2500, position=InfoBarPosition.TOP)
            else:
                InfoBar.error('索引下载失败', msg, parent=self.window(), duration=5000, position=InfoBarPosition.TOP)
        self._dl_updatet.done.connect(after)
        self._dl_updatet.start()

    def _dl_clear_cache(self):
        removed = self.downloader.clear_index_cache()
        self._dl_index_loaded = False
        self.dl_list.clear()
        self._dl_refresh_status_text()
        if removed:
            InfoBar.success('已清除', '本地索引缓存已删除', parent=self.window(), duration=2000, position=InfoBarPosition.TOP)
        else:
            InfoBar.info('无缓存', '本地没有缓存文件', parent=self.window(), duration=2000, position=InfoBarPosition.TOP)

    def _help_auto_start(self):
        QMessageBox.information(self, '自动开始说明',
            '【方式一: 视觉自动开始(实验性, 默认关闭, 不保证可靠)】\n'
            '打开"视觉自动开始"开关 -> 点"开始演奏" -> 程序会帮你在屏幕中心点一下, \n'
            '然后以20fps盯着屏幕帧数据。当检测到Phigros从准备界面跳进演奏界面\n'
            '(音符开始下落, 画面帧体积突变)的瞬间, 立刻按规划发送事件。\n'
            '原理: 只看每帧H.264数据的大小, 不解码画面内容, CPU占用极低。\n'
            '局限(所以默认关): 这是纯启发式猜测, 不是真的"看懂"画面。\n'
            '  - 帧体积受码率/画面复杂度/模拟器性能影响, 阈值1.8倍是我拍的, \n'
            '    不同设备/不同曲目可能不跳变或误触发;\n'
            '  - 20fps意味着最多滞后50ms, 加上我回退的25ms, 误差可能到几十毫秒;\n'
            '  - 检测到的是"进入演奏界面", 不是"第一个音符落到判定线", \n'
            '    前奏长的歌会整体偏早。\n'
            '如果试了不准, 请改用方式二(计时器同步)或方式三。\n\n'
            '【方式二: 计时器同步(第一拍按下)】\n'
            '点"计时器同步(第一拍按下)"按钮——程序会在屏幕中心点一下(帮您点开始),\n'
            '并把那一时刻记为 t=0+offset; 之后自动开始按规划发送事件。\n'
            '您只需在第一拍(第一个音符下落命中判定线)的瞬间点这个按钮,\n'
            '程序会根据"偏移(ms)"微调: 正值延后, 负值提前。\n\n'
            '【方式三: 自动开始开关】\n'
            '开启"自动开始"后, 点"开始演奏"时: 程序会自动点屏幕中心触发Phigros开始,\n'
            '并立刻开始按规划发送事件, 无需你对齐第一拍。\n'
            '这要求您的"偏移(ms)"设置得非常准——建议先用计时器同步模式测几次,\n'
            '记录每局恰好全Perfect时的偏移值, 填到"偏移"里再开自动开始。\n'
            '原理: manual_start_plan() 会在第一个音符之前插入扫屏触点的DOWN事件,\n'
            '按下"开始"按钮时直接把时钟设为 第一个事件时刻 - 10ms 开始播放,\n'
            '实现"按下即开始"的效果, 不用等您对齐节拍。\n\n'
            '【手动模式】\n'
            '三个开关都关时, 点"开始演奏"即认为 按下时刻 = 第一拍。\n'
            '适合你已经很熟、能稳定对齐的情况。\n\n'
            '【微调】\n'
            '打歌过程中/刚结束前, 用方向键/鼠标调节偏移是老版本特性;\n'
            '当前版本推荐: 一局打完看统计信息里的"最大延迟", 据此调整偏移ms。')

    def _help_ios(self):
        QMessageBox.information(self, 'iOS 支持说明',
            'iOS 无越狱触控注入的现状:\n\n'
            '【旧方案(需越狱)】\n'
            '  IOS13-SimulateTouch / ZXTouch / PTFakeTouch\n'
            '  系统级多点触控注入,延迟很低(5ms内),但需要 iOS 11-14 越狱;\n'
            '  iOS 15+ 上 palera1n / Taurine 等越狱也能装类似 tweak。\n\n'
            '【无越狱方案(2025 最新)】\n'
            '  SideTap (github.com/ucsandman/sidetap) — MIT 开源,\n'
            '  无需 Mac、无需越狱,只要 USB 数据线 + 免费 Apple ID:\n'
            '    1) 安装 go-ios + Python 客户端(Windows 一键安装脚本)\n'
            '    2) iPhone 开启开发者模式(iOS 17+),信任此电脑\n'
            '    3) 用 Sideloadly 免费签名把 WebDriverAgent.ipa 装到手机\n'
            '    4) 通过 go-ios USB 隧道访问 WDA,HTTP 接口下发触控事件\n'
            '  缺点: WDA 单指约 50ms 延迟,多指 MOVE 每批约 50ms,\n'
            '  对音游(要求 5ms 内释放)延迟偏大,能跑 EZ/HD 或非纵连曲目。\n'
            '  (免费 Apple ID 签名每 7 天需重签一次,重连即恢复)\n\n'
            '【iOS 模拟器方案(tapflow)】\n'
            '  github.com/jo-duchan/tapflow 可在 Mac 上驱动 iOS 模拟器\n'
            '  并原生 XCTest 注入触控(无 WDA 延迟),但需要 Mac + Xcode 且非真机。\n\n'
            '【phisap 当前支持情况】\n'
            '  目前 DeviceController 是为 Android adb + scrcpy 协议写的;\n'
            '  iOS 需要实现同样的 tap / device_width / device_height 接口才能接入。\n'
            '  如确需 WDA backend 支持请提 issue。\n\n'
            '【推荐替代】\n'
            '  最稳方案仍是 Android 备用机或 PC 模拟器(MuMu/雷电/BlueStocks),\n'
            '  adb 直连,phisap 开箱即用。')

    def _dl_do_search(self):
        def do_search():
            kw = self.dl_search.text().strip()
            case = self.dl_case_btn.isChecked()
            results = self.downloader.search(kw, case_sensitive=case)
            self.dl_list.clear()
            for sid, title, comp, diffs in results[:300]:
                item = QTreeWidgetItem([title, comp, ' / '.join(diffs)])
                item.setData(0, Qt.UserRole, sid)
                self.dl_list.addTopLevelItem(item)
            self._dl_set_status(f'找到 {len(results)} 首,显示前300首(双击下载)' + (' (区分大小写)' if case else ''))
        self._dl_ensure_index(do_search)

    def _dl_do_download(self):
        items = self.dl_list.selectedItems()
        if not items:
            return
        sid = items[0].data(0, Qt.UserRole)
        diff = self.dl_diff.currentText()
        self.dl_btn.setEnabled(False)
        self.dl_progress.show()
        self._dl_set_status(f'下载 {items[0].text(0)} [{diff}]...')

        class T(QThread):
            done = pyqtSignal(bool, str, str)
            prog = pyqtSignal(str)
            def __init__(self, dl, s, d): super().__init__(); self.dl=dl; self.s=s; self.d=d
            def run(self):
                try:
                    self.dl.download_chart(self.s, self.d,
                                           on_progress=lambda m: self.prog.emit(m),
                                           on_done=lambda ok,p,e: self.done.emit(ok,p,e or ''))
                except Exception as e:
                    self.done.emit(False, '', str(e))

        self._dl_dlt = T(self.downloader, sid, diff)
        self._dl_dlt.prog.connect(self._dl_set_status)
        def after(ok, path, err):
            self.dl_progress.hide()
            self.dl_btn.setEnabled(True)
            if ok:
                self._dl_set_status(f'已保存: {path}')
                self.refresh_songs()
                InfoBar.success('下载成功', os.path.basename(path),
                                parent=self.window(), position=InfoBarPosition.TOP, duration=2500)
            else:
                self._dl_set_status(f'失败: {err}')
                InfoBar.error('下载失败', err, parent=self.window(), position=InfoBarPosition.TOP, duration=4000)
        self._dl_dlt.done.connect(after)
        self._dl_dlt.start()

    def import_songs(self):
        files, _ = QFileDialog.getOpenFileNames(self, '选择谱面 JSON', '', 'JSON Files (*.json)')
        if not files:
            return
        ok = 0
        for fp in files:
            try:
                with open(fp, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if detect_kind(data) == 'rpe':
                    data = rpe_to_official_v3(data)
                sid = data.get('META', {}).get('id')
                if not sid:
                    base = os.path.splitext(os.path.basename(fp))[0]
                    sid = re.sub(r'[Cc]hart[_A-Za-z]*\s*[#\-_]?', '', base).strip() or base
                diff = chart_difficulty(os.path.basename(fp)) or 'SP'
                dst = os.path.join('./Assets/Tracks', sid)
                os.makedirs(dst, exist_ok=True)
                fn = f'Chart_{diff}.json' if diff != 'SP' else 'Chart.json'
                with open(os.path.join(dst, fn), 'w', encoding='utf-8') as out:
                    json.dump(data, out, ensure_ascii=False)
                ok += 1
            except Exception as e:
                self.log(f'导入失败 {os.path.basename(fp)}: {e}')
        self.log(f'成功导入 {ok} 个谱面')
        self.refresh_songs()
        InfoBar.success('导入完成', f'成功导入 {ok} 个谱面', parent=self.window(), duration=2500)

    def _on_backend_changed(self, idx: int):
        key = 'maatouch' if idx == 1 else 'scrcpy'
        self._cache_set('touch_backend', key)
        name = 'MaaTouch' if key == 'maatouch' else 'scrcpy'
        if key == 'maatouch':
            InfoBar.info('触控后端已切换',
                         'MaaTouch 最多同时 10 个触点, 且不支持视觉自动开始。\n'
                         '请点"刷新设备"重新连接后生效。',
                         parent=self.window(), duration=5000, position=InfoBarPosition.TOP)
        else:
            InfoBar.success('触控后端已切换',
                            'scrcpy: 最多 16 触点, 支持视觉自动开始。请点"刷新设备"重新连接。',
                            parent=self.window(), duration=4000, position=InfoBarPosition.TOP)
        self.log(f'触控后端已切换为 {name} (需重新连接设备)')

    def _make_controller(self, serial: str | None):
        '''按用户选择创建触控后端。'''
        if self._cache_get('touch_backend', 'scrcpy') == 'maatouch':
            from maatouch import MaaTouchController
            self.log(f'使用 MaaTouch 后端连接 {serial or "默认设备"} ...')
            return MaaTouchController(serial, server_dir='.')
        return DeviceController(serial)

    def detect_adb_devices(self):
        self.controller = None
        self.devices_box.clear()
        try:
            out = subprocess.check_output(['adb', 'devices'], text=True, stderr=subprocess.STDOUT, timeout=5)
            devs = []
            for line in out.splitlines()[1:]:
                line = line.strip()
                if not line: continue
                parts = line.split('\t')
                if len(parts) == 2 and parts[1] == 'device':
                    devs.append(parts[0])
            if not devs:
                self.dev_badge.setText('未连接')
                InfoBar.warning('未检测到设备', '请连接手机并开启 USB 调试',
                                parent=self.window(), position=InfoBarPosition.TOP, duration=3000)
                return
            self.devices_box.addItems(devs)
            self._adapt_cache = None
            self.controller = self._make_controller(devs[0])
            self.dev_badge.setText(f'已连接 {devs[0]}')
            self.log(f'设备已连接: {devs[0]} ({self.controller.device_width}x{self.controller.device_height})'
                     f', 触控后端: {"MaaTouch" if self._cache_get("touch_backend","scrcpy")=="maatouch" else "scrcpy"}')
            if self._raw_ans is not None:
                self.go_btn.setEnabled(True)
                # 屏幕尺寸以连接时为准, 变了他适配结果也要重算
                self._refresh_adapted()
        except FileNotFoundError:
            self.dev_badge.setText('ADB 未找到')
            InfoBar.error('ADB 未找到', '请把 adb 加入 PATH', parent=self.window(), duration=4000)
        except Exception as e:
            self.log(f'检测设备失败: {e}')

    # --- 规划 ---
    def start_planning(self):
        path = self._find_chart_path()
        if not path:
            InfoBar.warning('提示', '请先选择曲目和难度', parent=self.window(), duration=2500)
            return
        algo = self.algo_box.currentText()
        plan_path = path.replace('.json', PLAN_CACHE_SUFFIX)
        self.plan_btn.setEnabled(False)
        self.go_btn.setEnabled(False)
        self.log(f'开始生成规划 [{algo}] ...')
        # 触点数上限取决于当前后端: scrcpy补丁版16, MaaTouch只有10。
        # 之前固定按16算, 用MaaTouch时多出来的手指会被服务端静默丢弃。
        if self.controller is not None:
            cap = getattr(self.controller, 'max_pointers', None) or max_touch_points()
        else:
            cap = max_touch_points()
        self._plan_t = PlanThread(path, algo, plan_path, cap)
        self._plan_t.finished_ok.connect(self._plan_ok)
        self._plan_t.failed.connect(self._plan_fail)
        self._plan_t.log_line.connect(lambda s: self.log(s.rstrip()))
        self._plan_t.start()

    def _plan_ok(self, plan_path, ans):
        self.plan_btn.setEnabled(True)
        self.plan_path = plan_path
        self._raw_ans = ans
        self.export_btn.setEnabled(True)
        if self.controller:
            self.go_btn.setEnabled(True)
        self.log(f'规划完成: {os.path.basename(plan_path)} ({len(ans)} 个时间点)')
        # 同上: 后台预计算坐标适配, 别让用户点开始时再等
        self._refresh_adapted()
        InfoBar.success('规划完成', f'{len(ans)} 个时间点', parent=self.window(), duration=2000)

    def _plan_fail(self, tb):
        self.plan_btn.setEnabled(True)
        self.log('规划失败:\n' + tb)
        InfoBar.error('规划失败', tb.splitlines()[-1] if tb else '未知错误',
                      parent=self.window(), duration=4000)

    def export_plan(self):
        if not self.plan_path:
            return
        fn, _ = QFileDialog.getSaveFileName(self, '导出规划 JSON', self.plan_path, 'JSON (*.json)')
        if fn:
            import shutil
            shutil.copy(self.plan_path, fn)
            InfoBar.success('已导出', fn, parent=self.window(), duration=2000)

    # --- 播放 ---
    def _on_vauto_changed(self, on: bool):
        '''视觉自动开始开关: 开时排斥"自动开始(计时器)"开关, 由程序自己盯屏幕。'''
        self._vauto_event = None
        self._vauto_fire_time = 0.0
        self._vauto_waiting = False
        if on and self.auto_switch.isChecked():
            self.auto_switch.setChecked(False)
            self.log('已从"计时器同步"切换到"视觉自动开始"')
        self.log(f'视觉自动开始: {"开" if on else "关"}')

    def sync_ms(self):
        if not self.controller:
            InfoBar.warning('无设备', '请先连接 ADB 设备', parent=self.window(), duration=2500); return
        if not self._raw_ans:
            InfoBar.warning('提示', '请先生成规划', parent=self.window(), duration=2500); return
        w, h = self.controller.device_width, self.controller.device_height
        self.controller.tap(w >> 1, h >> 1)
        offset = self.delay_spin.value() / 1000.0
        self._start_time = time.perf_counter() + offset
        self.log(f'同步完成,偏移 {offset*1000:.1f} ms')
        self._start_playback(manual=False)

    def run(self):
        if not self._raw_ans:
            if self.plan_path and os.path.exists(self.plan_path):
                with open(self.plan_path, 'r', encoding='utf-8') as f:
                    self._raw_ans = load_from_json(json.load(f))
            else:
                InfoBar.warning('提示', '请先生成规划', parent=self.window(), duration=2500); return
        if not self.controller:
            InfoBar.warning('无设备', '请先连接 ADB 设备', parent=self.window(), duration=2500); return
        dw, dh = self.controller.device_width, self.controller.device_height
        if dh > dw:
            if QMessageBox.question(self, '竖屏警告',
                f'设备当前为竖屏 ({dw}x{dh})。Phigros 是横屏游戏,请先横屏打开游戏。\n仍然继续吗？'
            ) != QMessageBox.Yes:
                return
        if self.auto_switch.isChecked():
            self.sync_ms()
        elif self.vauto_switch.isChecked():
            self.log('视觉自动开始: 等待检测进入演奏界面(请先点Phigros开始按钮)')
            self._start_visual_wait()
        else:
            self.log('手动开始:按下按钮时对齐第一个音符')
            self._start_playback(manual=True)

    def _start_visual_wait(self):
        '''视觉自动开始: 点一下屏幕中心帮用户触发开始, 然后盯屏幕帧体积突变,
        检测到 Phigros 从准备界面跳进演奏界面的瞬间, 才真正按规划发送事件。
        这样不同歌曲(第一个音符在第几秒)都不需要你手动对齐。'''
        if not self.controller:
            InfoBar.warning('无设备', '请先连接 ADB 设备', parent=self.window(), duration=2500); return
        if not getattr(self.controller, 'supports_visual_watch', True):
            InfoBar.warning('当前后端不支持',
                            'MaaTouch 没有视频流, 无法做视觉自动开始, 已回退到计时器同步。',
                            parent=self.window(), duration=4000, position=InfoBarPosition.TOP)
            self.log('视觉自动开始不可用(MaaTouch无视频流), 回退到计时器同步')
            self.sync_ms()
            return
        w, h = self.controller.device_width, self.controller.device_height
        try:
            self.controller.tap(w >> 1, h >> 1)
        except Exception:
            pass
        try:
            ev = self.controller.start_activity_watch(cooldown=2.0)
        except Exception as e:
            self.log(f'视觉自动开始启动失败(回退到计时器同步): {e}')
            self.controller.stop_activity_watch()
            self.sync_ms()
            return
        self._vauto_event = ev
        self._vauto_waiting = True
        self.log(f'已开启屏幕监测(约{VIDEO_MAX_FPS}fps), 请在手机上点开始...')
        InfoBar.info('等待开始', '请在设备上点开始, 程序检测到进入演奏后自动开打',
                     parent=self.window(), duration=6000, position=InfoBarPosition.TOP)
        self.go_btn.setText('取消等待')
        self.go_btn.setIcon(FIF.CANCEL)
        try:
            self.go_btn.clicked.disconnect()
        except Exception:
            pass
        self.go_btn.clicked.connect(self._cancel_visual_wait)
        self.sync_btn.setEnabled(False)

        def waiter():
            fired = ev.wait(timeout=120.0)
            self.controller.stop_activity_watch()
            if not self._vauto_waiting:
                self.vauto_abort.emit()
                return
            if not fired:
                self.log('视觉自动开始: 等待超时(120秒)仍未检测到界面跳变')
            # 记录"检测到跳变"的绝对时刻作为打歌时钟锚点;
            # 真实跳变发生在 wait() 返回前的某一帧, 这里回退半帧(约25ms @20fps)补偿检测延迟
            self._vauto_fire_time = time.perf_counter() - 0.025
            self._start_time = self._vauto_fire_time
            # 必须在GUI线程里启动播放(要改按钮文字/重连信号), 用信号切回主线程
            self.vauto_launch.emit()

        Thread(target=waiter, daemon=True).start()

    def _vauto_launch(self):
        '''(GUI线程) 视觉自动开始检测到跳变后真正开始播放'''
        if not self._vauto_waiting:
            return
        self._vauto_waiting = False
        self.log(f'视觉触发: 已检测到进入演奏, 锚点回退25ms')
        self._start_playback(manual=False, prestarted=True)

    def _cancel_visual_wait(self):
        '''视觉自动开始等待期间用户点按钮取消'''
        if not self._vauto_waiting:
            return
        self._vauto_waiting = False
        if self.controller:
            self.controller.stop_activity_watch()
        if self._vauto_event:
            self._vauto_event.set()   # 唤醒waiter, 它会走abort分支
        self.log('已取消视觉自动开始等待')

    def _vauto_abort(self):
        '''(GUI线程) 视觉等待被取消/异常结束时恢复界面'''
        self._vauto_waiting = False
        self.go_btn.setText('开始演奏')
        self.go_btn.setIcon(FIF.PLAY)
        try:
            self.go_btn.clicked.disconnect()
        except Exception:
            pass
        self.go_btn.clicked.connect(self.run)
        self.go_btn.setEnabled(True)
        self.sync_btn.setEnabled(True)
        self._vauto_event = None
        self._vauto_fire_time = 0.0

    # 注意: 这个偏移**对应用实际看到的 pointerId 没有任何作用**。
    # scrcpy-server 和 MaaTouch 都会在注入前把我们给的 id 重映射成 0~N 的 localId
    # (见 scrcpy PointersState.update / MaaTouch PointersState.update: props[i].id = localId),
    # 所以想靠"错开PID"来避开人手触摸是行不通的——人手一点屏幕, InputDispatcher 依然会
    # 给应用发 ACTION_CANCEL, 把程序按住的触点全部取消。要避免被打断只能换触控后端试。
    # 这里保留偏移只是为了在日志/调试时能区分程序触点, 不影响实际行为。
    PID_OFFSET = 20000


    def _start_playback(self, manual: bool, prestarted: bool = False):
        '''manual:      True=按下按钮的时刻即第一拍(插入扫屏DOWN触点)
        prestarted: True=视觉自动开始已触发, 不再自己点屏幕, 直接播放'''
        assert self._raw_ans and self.controller
        # 新的一轮播放: 代际+1, 让任何还没退干净的上一个worker立即停止发送
        self._playback_gen += 1
        dw, dh = self.controller.device_width, self.controller.device_height
        self.log(f'屏幕 {dw}x{dh}')
        self._running = True
        self.go_btn.setText('停止演奏')
        self.go_btn.setIcon(FIF.CANCEL)
        try:
            self.go_btn.clicked.disconnect()
        except Exception:
            pass
        self.go_btn.clicked.connect(self._stop)
        self.sync_btn.setEnabled(False)

        my_gen = self._playback_gen

        def worker():
            # 代际过期(用户停止过/又重新开始过)就什么都不做
            if my_gen != self._playback_gen:
                return
            try:
                # 重活全搬到这里, 不在GUI线程做:
                #   5万多次坐标适配(实测138ms) + 读盘解析整张谱面取首音符(实测42ms起)
                # 以前这两段跑在GUI线程、跑在用户点完"开始演奏"之后, 界面要卡几百毫秒
                # 才动 —— 那就是一直被当成"延迟1秒左右"的卡顿, 跟视频参数无关。
                got = self._adapted_now()
                if got is None:
                    return
                adapted, active = got
                if manual:
                    fnm = first_note_ms_from_path(self._find_chart_path())
                    adapted = manual_start_plan(adapted, fnm)
                    active = _active_pids_of(adapted)
                    self._start_time = time.perf_counter() - adapted[0][0] / 1000 - MANUAL_START_LEAD
                elif prestarted:
                    # 视觉自动开始: 锚点不是"现在", 而是"检测到界面跳变的时刻"(_vauto_fire_time),
                    # 这样打歌时钟与Phigros内部时钟同源, 每首歌(不管第一音符在第几秒)都自动对齐。
                    fnm = first_note_ms_from_path(self._find_chart_path())
                    adapted = manual_start_plan(adapted, fnm)
                    active = _active_pids_of(adapted)
                    self._start_time = self._vauto_fire_time - adapted[0][0] / 1000
                self._active_pids = active
                if not adapted:
                    self.log('规划为空, 没有可发送的事件')
                    return
                first = adapted[0]
                rest = iter(adapted[1:])
                if manual or prestarted:
                    stats = run_player(self.controller.touch_many, rest,
                                       lambda: self._start_time, lambda: self._running,
                                       first_event=first,
                                       should_continue=lambda: my_gen == self._playback_gen)
                else:
                    stats = run_player(self.controller.touch_many, iter(adapted),
                                       lambda: self._start_time, lambda: self._running,
                                       should_continue=lambda: my_gen == self._playback_gen)
                self.log('演奏结束')
                if stats is not None:
                    for line in stats.summary(0):
                        self.log(line)
            except Exception:
                self.log('演奏出错:\n' + traceback.format_exc())
            finally:
                # 播放结束(无论正常/异常/手动停止)都释放残余触点,避免手指卡在屏幕上
                self._release_all_active()
                # 通知GUI线程复位按钮。必须用信号: invokeMethod 找不到未注册的Python方法
                self.playback_finished.emit()

        self._player_thread = Thread(target=worker, daemon=True)
        self._player_thread.start()
    def _release_all_active(self):
        """给所有仍处于按下状态的程序触点发送 UP 事件,防止手指卡住"""
        try:
            ctrl = self.controller
            if ctrl is None or not ctrl.collector_running:
                return
            active = getattr(self, '_active_pids', set())
            if not active:
                return
            # 抬起位置用屏幕中心即可: Android 按 pointerId 匹配, 坐标不影响抬起语义。
            # 具体怎么发(scrcpy 控制socket / MaaTouch 指令)由后端自己封装。
            ctrl.release_pointers(list(active))
            self.log(f'已释放 {len(active)} 个残余触点')
            self._active_pids = set()
        except Exception as e:
            self.log(f'释放残余触点失败(可忽略): {e}')

    def _stop(self):
        self._running = False
        self._vauto_waiting = False
        self._vauto_fire_time = 0.0
        # 代际+1: 让当前worker立刻停止发送新事件(不等它自己发现_running变了)
        self._playback_gen += 1
        self.log('正在停止...')
        # 立刻在GUI线程复位按钮, 不等播放线程——它可能卡在发送上永远出不来。
        self._reset_go()
        # 但"真正让手指离开屏幕"要另外做: 必须等播放线程不再写socket之后再发UP,
        # 否则两个线程同时写控制socket会把触控包撕碎(见 DeviceController._send_lock),
        # 表现为"界面显示停止了, 手指还按在屏幕上"。
        Thread(target=self._stop_async, daemon=True).start()

    def _stop_async(self):
        '''(后台线程) 等播放线程退出, 然后释放所有触点。'''
        t = self._player_thread
        if t is not None and t.is_alive():
            t.join(timeout=0.5)
            if t.is_alive():
                # 0.5秒还没退出, 基本是阻塞在 socket/管道写入上(设备端不读了)。
                # 强行断开传输让那个阻塞的write抛异常, 线程才能走到finally。
                self.log('播放线程未在0.5秒内退出, 强制断开触控通道')
                try:
                    if self.controller is not None:
                        self.controller.abort()
                except Exception as e:
                    self.log(f'强制断开失败(可忽略): {e}')
                t.join(timeout=0.5)
                if t.is_alive():
                    self.log('播放线程仍未能退出, 触点可能残留, 建议重新连接设备')
        # 线程已经不再写socket了, 现在发UP才是安全的
        self._release_all_active()

    def _reset_go(self):
        '''复位演奏按钮。可能被调用多次(手动停止一次 + 播放线程结束信号一次),
        所以先用按钮文字判断是否已复位, 否则 clicked 会被连上两份 run, 点一下开始两次。
        注意: 早退时不能顺手 setEnabled(True) —— 没生成规划时按钮本来就该是禁用的,
        那样会被错误启用。'''
        already = self.go_btn.text() == '开始演奏'
        self._vauto_waiting = False
        if not already:
            self.go_btn.setText('开始演奏')
            self.go_btn.setIcon(FIF.PLAY)
            try:
                self.go_btn.clicked.disconnect()
            except Exception:
                pass
            self.go_btn.clicked.connect(self.run)
            self.go_btn.setEnabled(True)
        self.sync_btn.setEnabled(True)

    # --- 缓存 ---
    def load_cache(self, path):
        self.cache_path = path
        cache = configparser.ConfigParser()
        if os.path.exists(path):
            try:
                cache.read(path, encoding='utf-8')
            except Exception:
                try:
                    cache.read(path, encoding=locale.getpreferredencoding(False))
                except Exception:
                    cache = configparser.ConfigParser()
        if not cache.has_section('cache'):
            cache.add_section('cache')
        for k in ('songid', 'difficulty', 'algo'):
            if not cache.has_option('cache', k):
                cache.set('cache', k, '')
        sid = cache.get('cache', 'songid')
        diff = cache.get('cache', 'difficulty')
        algo = cache.get('cache', 'algo')
        if sid and self.song_box.findText(sid) >= 0:
            self.song_box.setCurrentText(sid)
        if diff and self.diff_box.findText(diff) >= 0:
            self.diff_box.setCurrentText(diff)
        if algo and self.algo_box.findText(algo) >= 0:
            self.algo_box.setCurrentText(algo)
        return self

    def save_cache(self):
        cache = configparser.ConfigParser()
        cache.add_section('cache')
        cache.set('cache', 'songid', self.song_box.currentText())
        cache.set('cache', 'difficulty', self.diff_box.currentText())
        cache.set('cache', 'algo', self.algo_box.currentText())
        with open(self.cache_path, 'w', encoding='utf-8') as f:
            cache.write(f)

    # ---- 字体缩放 ----
    FONT_SCALES = [0.9, 1.0, 1.15, 1.30]

    def _cache_path(self) -> str:
        return getattr(self, 'cache_path', './cache')

    def _cache_get(self, key: str, default: str = '') -> str:
        cfg = configparser.ConfigParser()
        p = self._cache_path()
        if os.path.exists(p):
            try:
                cfg.read(p, encoding='utf-8')
                if cfg.has_section('cache') and cfg.has_option('cache', key):
                    return cfg.get('cache', key)
            except Exception:
                pass
        return default

    def _cache_set(self, key: str, value: str):
        cfg = configparser.ConfigParser()
        p = self._cache_path()
        if os.path.exists(p):
            try:
                cfg.read(p, encoding='utf-8')
            except Exception:
                pass
        if not cfg.has_section('cache'):
            cfg.add_section('cache')
        cfg.set('cache', key, value)
        try:
            with open(p, 'w', encoding='utf-8') as f:
                cfg.write(f)
        except Exception:
            pass

    def _read_cached_source(self) -> str:
        return self._cache_get('source', '')

    def _apply_font_scale(self, scale: float):
        app = QApplication.instance()
        base = app.font()
        f = QFont(base)
        f.setPointSizeF((base.pointSizeF() or 9) * scale)
        app.setFont(f)

    def _on_font_changed(self, idx: int):
        idx = max(0, min(idx, len(self.FONT_SCALES) - 1))
        scale = self.FONT_SCALES[idx]
        self._apply_font_scale(scale)
        self._cache_set('font_scale', str(scale))
        InfoBar.success('字体已调整', f'缩放到 {int(scale*100)}%',
                        parent=self.window(), duration=2000)

    def _on_source_changed(self, idx: int):
        if idx < 0 or idx >= len(self._source_keys):
            return
        key = self._source_keys[idx]
        self.downloader.set_source(key)
        self._cache_set('source', key)
        self._dl_index_loaded = False
        InfoBar.success('下载源已切换', f'当前: {self.source_box.currentText()}',
                        parent=self.window(), duration=2000)

    def _load_font_scale(self):
        try:
            v = self._cache_get('font_scale')
            if v:
                scale = float(v)
                if scale in self.FONT_SCALES:
                    idx = self.FONT_SCALES.index(scale)
                    self.font_box.setCurrentIndex(idx)
                    self._apply_font_scale(scale)
        except Exception:
            pass

    def closeEvent(self, ev):
        try:
            self.save_cache()
        except Exception:
            pass
        super().closeEvent(ev)


# ============== Fluent 主窗口 ==============
class Window(MSFluentWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Phisap')
        self.resize(820, 820)
        self.setMinimumSize(680, 680)

        self.main_page = MainPage(self)
        self.addSubInterface(self.main_page, FIF.APPLICATION, '主界面')


def agreement(parent=None) -> bool:
    if os.path.exists('./cache'):
        return True
    btn = QMessageBox.question(parent, '用户协定',
        '您因使用或修改本程序发生的一切后果由您自己承担而与程序作者无关。\n是否同意？',
        QMessageBox.Yes | QMessageBox.No)
    if btn == QMessageBox.Yes:
        os.makedirs('./cache', exist_ok=True)
        return True
    return False


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.excepthook = _report_crash

    app = QApplication(sys.argv)
    app.setApplicationName('Phisap')

    # 强制深色主题,写入配置
    from qfluentwidgets.common.config import qconfig
    qconfig.set(qconfig.themeMode, Theme.DARK, save=True)
    setThemeColor('#F26192', save=True)
    setTheme(Theme.DARK, save=True)

    # 全局深色背景+白色文字兜底;
    # Fluent 的自绘控件(PushButton/SwitchButton/ProgressBar等)会paintEvent自己覆盖颜色,
    # 不受这里的 background 影响;而透明/未被自定义绘制的容器会继承这里的深色
    app.setStyleSheet('''
        * { color: #f0f0f4; }
        QWidget, QFrame, QDialog, QScrollArea, QStackedWidget, QPlainTextEdit,
        QLabel, QToolTip, QSizeGrip {
            background-color: #202029;
            color: #f0f0f4;
        }
        QMainWindow, #mainPage { background-color: #202029; }
        /* 输入框/下拉: Fluent的LineEdit/ComboBox自己绘,这里只给原生兜底 */
        QLineEdit, QTextEdit, QPlainTextEdit {
            background-color: #292934;
            border: 1px solid #3f3f52;
            border-radius: 6px;
            color: #f0f0f4;
            padding: 6px 10px;
            selection-background-color: #F26192;
        }
        QLineEdit:focus, QTextEdit:focus { border-color: #F26192; }
        QComboBox {
            background-color: #292934;
            border: 1px solid #3f3f52;
            border-radius: 6px;
            padding: 5px 10px;
            color: #f0f0f4;
        }
        QComboBox:hover { border-color: #F26192; }
        QComboBox QAbstractItemView {
            background-color: #2c2c38; color: #f0f0f4;
            selection-background-color: #F26192; selection-color: #fff;
            border: 1px solid #3f3f52; outline: 0;
        }
        QComboBox::drop-down { border: none; width: 24px; }
        QComboBox::down-arrow { width:0; }
        QSpinBox, QDoubleSpinBox {
            background-color: #292934; border: 1px solid #3f3f52;
            border-radius: 6px; padding: 4px 8px; color: #f0f0f4;
        }
        /* 树/列表: 下载结果 */
        QTreeWidget, QListWidget {
            background-color: #292934; border: 1px solid #3f3f52;
            border-radius: 6px; color: #f0f0f4; alternate-background-color: #2e2e3b;
            selection-background-color: #F26192; selection-color: #fff;
            outline: none; padding: 4px;
        }
        QTreeWidget::item, QListWidget::item {
            min-height: 28px; padding: 4px 8px; border-radius: 4px;
        }
        QTreeWidget::item:hover, QListWidget::item:hover { background-color: #3a3a48; }
        QTreeWidget::item:selected, QListWidget::item:selected {
            background-color: #F26192; color: #fff;
        }
        QHeaderView::section {
            background-color: #202029; color: #9a9aa8;
            padding: 6px 8px; border: none; font-weight: 600;
        }
        QScrollBar:vertical { background: transparent; width: 8px; margin: 4px; }
        QScrollBar::handle:vertical { background: #505063; border-radius: 4px; min-height: 30px; }
        QScrollBar::handle:vertical:hover { background: #F26192; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        QScrollBar:horizontal { background: transparent; height: 8px; margin: 4px; }
        QScrollBar::handle:horizontal { background: #505063; border-radius: 4px; min-width: 30px; }
        QScrollBar::handle:horizontal:hover { background: #F26192; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
        QToolTip {
            background-color: #2c2c38; color: #f0f0f4;
            border: 1px solid #3f3f52; padding: 4px 8px; border-radius: 4px;
        }
        QMenu {
            background-color: #2c2c38; color: #f0f0f4;
            border: 1px solid #3f3f52; border-radius: 6px; padding: 4px;
        }
        QMenu::item { padding: 6px 20px; border-radius: 4px; }
        QMenu::item:selected { background-color: #F26192; color: #fff; }
        QMenu::separator { height: 1px; background: #3f3f52; margin: 4px 8px; }
        /* 对话框按钮 */
        QDialogButtonBox > QPushButton, QMessageBox QPushButton {
            background-color: #2c2c38; color: #f0f0f4;
            border: 1px solid #3f3f52; border-radius: 6px;
            padding: 6px 22px; min-width: 80px;
        }
        QDialogButtonBox > QPushButton:hover, QMessageBox QPushButton:hover {
            border-color: #F26192; color: #F26192;
        }
        QDialogButtonBox > QPushButton:default, QMessageBox QPushButton:default {
            background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #F26192, stop:1 #6E80FF);
            color: #fff; border: none;
        }
    ''')

    os.makedirs('./Assets/Tracks', exist_ok=True)

    w = Window()
    if not agreement(w):
        sys.exit(0)
    w.show()
    try:
        sys.exit(app.exec_())
    except SystemExit:
        raise
    except BaseException:
        _report_crash(*sys.exc_info())
        sys.exit(1)
