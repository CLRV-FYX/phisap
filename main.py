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
import threading
import time
import traceback
from threading import Thread

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt5.QtGui import QTextCursor, QFont
from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QMessageBox, QFileDialog, QStackedWidget, QTreeWidget, QTreeWidgetItem, QHeaderView

from qfluentwidgets import (
    MSFluentWindow, FluentIcon as FIF,
    SubtitleLabel, CaptionLabel, StrongBodyLabel,
    PrimaryPushButton, PushButton, ToolButton, SwitchButton,
    SearchLineEdit, PlainTextEdit,
    TransparentToolButton,
    ComboBox, DoubleSpinBox, CheckBox, LineEdit,
    SettingCardGroup, SettingCard, Pivot,
    InfoBar, InfoBarPosition,
    IndeterminateProgressBar, ProgressBar,
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
import apk_tools


ALGORITHMS = ('algo3', 'algo3f', 'algo1', 'algo2')

_KNOWN_DIFFICULTIES = ('SPB', 'INB', 'HDB', 'ATB', 'SP', 'IN', 'HD', 'AT', 'DT', 'EZ')
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


def load_chart_file(path: str):
    """从磁盘读一份谱面并转成 Chart。官谱(formatVersion 1/2/3)和 RPE 谱面都认。

    以前这里是裸的 Chart.from_dict(json.load(f))。用户选到 RPE 谱面时
    (RPE 没有 formatVersion, 靠 BPMList/META/judgeLineList 识别)直接
    KeyError: 'formatVersion' 崩掉, 而 import_songs 那条导入路本来就有 RPE 转换,
    规划这条路忘了 —— 于是"能导入却没法规划"。

    认不出来时抛带文件名的 ValueError, 比 KeyError 好排查得多。
    返回 (Chart, 警告列表), 警告由调用方决定怎么显示。
    """
    if is_plan_cache(path):
        raise ValueError(
            f'选中了规划缓存文件而不是谱面: {os.path.basename(path)}\n'
            f'*.ans.vN.json 是程序自己生成的规划结果, 不是谱面, 读不出 formatVersion。\n'
            f'请重新选择曲目和难度; 如果曲目列表里只剩下缓存文件, 说明这张谱面没被正确导入。')
    with open(path, 'r', encoding='utf-8-sig') as f:
        data = json.load(f)
    kind = detect_kind(data)
    warns: list[str] = []
    if kind == 'rpe':
        data, warns = rpe_to_official_v3(data)
    elif kind == 'unknown':
        raise ValueError(
            f'无法识别的谱面格式: {os.path.basename(path)}\n'
            f'既不是官谱(formatVersion 1/2/3), 也不是 RPE 谱面'
            f'(RPE 需要 BPMList / META / judgeLineList)。\n'
            f'如果你认为这个文件没问题, 请把文件发给作者。')
    return Chart.from_dict(data), warns


def first_note_ms_from_path(path: str | None) -> int:
    '''谱面第一个音符的判定时间(ms)。

    这个函数会被 _start_playback 在每次点"开始演奏"时调用, 而它的实现是
    "读盘 -> 识别格式(RPE要转换) -> 遍历所有判定线和音符取最小值"。
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
        ch, _ = load_chart_file(path)
        v = first_note_ms(ch)
        v = v if v else 0
    except Exception:
        v = 0
    if len(_FIRST_NOTE_CACHE) > 64:
        _FIRST_NOTE_CACHE.clear()
    _FIRST_NOTE_CACHE[key] = v
    return v


# 规划缓存文件的名字形如 Chart_AT.ans.v10.json。**必须按模式匹配所有版本**,
# 不能只排除当前版本的后缀: 缓存版本号一升(v9 -> v10), 上一版留下的旧缓存
# (.ans.v9.json)就不再被排除, 会被当成谱面选中去规划, 报"无法识别的谱面格式"。
# 用户实际踩过这个坑。
_PLAN_CACHE_RE = re.compile(r'\.ans\.v\d+\.json$', re.IGNORECASE)


def is_plan_cache(filename: str) -> bool:
    """是不是程序自己生成的规划缓存(*.ans.vN.json), 而不是谱面"""
    return bool(_PLAN_CACHE_RE.search(os.path.basename(filename)))


def find_chart_path(song_id: str, diff: str) -> str | None:
    folder = os.path.join('./Assets/Tracks', song_id)
    if not os.path.isdir(folder):
        return None
    for f in os.listdir(folder):
        if (chart_difficulty(f) == diff and f.endswith('.json')
                and not is_plan_cache(f)):
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
            # 用 load_chart_file 而不是裸的 Chart.from_dict: 用户选到 RPE 谱面时
            # 必须先转成官方v3结构, 否则 KeyError: 'formatVersion' 直接规划失败。
            chart, chart_warns = load_chart_file(self.chart_path)
            for w in chart_warns:
                cap.print(f'[yellow]RPE转换: {w}[/yellow]')
            mod = importlib.import_module(f'algo.{self.algo}')
            ans = mod.solve(chart, cap, self.max_pointers)
            with open(self.plan_path, 'w', encoding='utf-8') as fp:
                export_to_json(ans, fp)
            self.log_line.emit(buf.getvalue())
            self.finished_ok.emit(self.plan_path, ans)
        except Exception:
            self.failed.emit(traceback.format_exc())


# ============== APK 提取/解包线程 ==============
class ApkTaskThread(QThread):
    '''后台执行 apk_tools 的任务(adb 提取 APK/OBB、解包谱面)。

    任务函数形如 task(progress, cancel, log), 在这个线程里跑; 进度、日志、结果、失败原因
    全部通过信号回到GUI线程, 线程里绝不碰控件。progress 的第一个参数是完成比例
    (0~1, 总量未知时是 -1): 总量可能超过 2GB, 不能用 int 信号(C int 会溢出)。
    '''
    progress = pyqtSignal(float, str)
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(object)
    cancelled = pyqtSignal()
    failed = pyqtSignal(str)

    def __init__(self, task):
        super().__init__()
        self._task = task
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def _on_progress(self, done, total, text):
        self.progress.emit(done / total if total else -1.0, text)

    def run(self):
        try:
            result = self._task(self._on_progress, self.cancel_event, self.log_line.emit)
        except apk_tools.Cancelled:
            self.cancelled.emit()
        except apk_tools.ApkToolError as e:
            self.failed.emit(str(e))  # 已经是写给用户看的说明, 不用堆栈
        except Exception:
            self.failed.emit(traceback.format_exc())
        else:
            self.finished_ok.emit(result)


# 手动开始时, 打歌时钟比"现在"提前这么多(秒)。注入触控有延迟(经 adb 转发 +
# scrcpy-server 处理), 游戏是在收到点击之后才开始计时的; 这里留一点提前量,
# 让第一批事件(按下按钮那一戳)落在游戏开始之前而不是之后。
MANUAL_START_LEAD = 0.01

# "开始延迟"和"实时延迟"的可调范围(ms)。以前是±500, 用户反馈不够(不同设备/模拟器的
# 整体时序偏移能超过1秒); 后来放到±2秒, 用户又明确要求"两个都不设大小上限"。
# QDoubleSpinBox 必须有有限边界, 这里给一个远超任何实际需求的范围(±999秒),
# 对人工调节等于没有上限。注意别处不要再夹一次 —— 用户设多少就是多少,
# 夹一次他设的值就被悄悄改回去, 等于没有补偿手段。
DELAY_LIMIT_MS = 999999


class PointerTracker:
    """跟着发送进度实时维护"当前哪些触点正按在屏幕上"。

    以前的写法是拿"整份规划跑完之后还没抬起的触点"当作停止时要释放的集合 ——
    一份完整规划里所有触点最后都会被抬起, 于是那个集合基本永远为空,
    点停止时一个UP都发不出去。扫屏触点是"第一个音符之前按下、最后一个音符之后
    抬起", 正好永远不在这个集合里, 所以用户看到的就是"扫屏触点停不下来"。

    release_set() = 当前真正按下的(live) ∪ 这一轮用过的全部触点(all_pids)。
    后半部分是必须的: 用户用手指点过屏幕后, Android 会给应用发 ACTION_CANCEL,
    应用的触点被取消了, 但 scrcpy-server 内部的 PointersState 仍认为那些触点
    按着。只发live里的UP清不掉它的状态, 下一轮用同一个pointerId发DOWN会被当成
    "已经按下"而失效, 于是用户必须重启模拟器才能恢复。
    """
    __slots__ = ('live', 'all_pids')

    def __init__(self):
        self.live: set[int] = set()
        self.all_pids: set[int] = set()

    def update(self, events) -> None:
        for e in events:
            self.all_pids.add(e.pointer)
            if e.action is TouchAction.DOWN:
                self.live.add(e.pointer)
            elif e.action is TouchAction.UP:
                self.live.discard(e.pointer)

    def release_set(self) -> set[int]:
        return self.live | self.all_pids


LOG_FILE = './phisap.log'


def _log_to_file(msg: str) -> None:
    """每一行日志都追加写到 ./phisap.log。

    界面里的日志控件高度有限, 而且用户经常只截一小段图; 落盘之后
    完整过程随时可查(排查"停止不彻底/断连/延迟"这类问题时特别需要)。
    """
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(str(msg).rstrip('\n') + '\n')
    except Exception:
        pass


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
        # 播放期间的实时延迟(秒), 由"实时延迟"旋钮直接驱动。
        # run_player 每批都重新读 start_time, 所以改这个值下一批就生效 ——
        # 旧版tkinter的"微调"就是这么做的。
        self._fine_tune = 0.0
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

    def _enable_touch_scroll(self):
        '''让设置页面可以用手指直接拖动滚动(触屏电脑)。

        MainPage 是 ScrollArea, 而 QScrollArea 默认只认滚轮和滚动条 ——
        在触屏电脑上手指划页面没有反应, 用户看到的就是"我能用手指滑动页面,
        但程序不可以"。QScroller 加一个手势识别就够, 不用自己写触摸逻辑。

        只抓 TouchGesture, 故意不抓 LeftMouseButtonGesture: 后者会把左键按下
        事件延迟(QScrollerProperties::MousePressEventDelay 默认0.25秒),
        页面上所有按钮/下拉框/数字框的点击都会变肉。手指走真正的触摸事件就行。

        整段包 try: 某些 PyQt 构建或平台上没有 QScroller, 抓不到也不能让界面
        起不来。self 和 viewport() 都抓一次 —— 不同 PyQt 版本上手势要接在
        哪个对象上表现不一样, 哪个生效算哪个。
        '''
        try:
            from PyQt5.QtWidgets import QScroller
        except Exception:
            return
        for target in (self, self.viewport()):
            try:
                QScroller.grabGesture(target, QScroller.TouchGesture)
            except Exception:
                pass

    def _build(self):
        self.scroll_widget = QWidget()
        self.setWidget(self.scroll_widget)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 触屏电脑上用手指拖动页面滚动(见 _enable_touch_scroll)
        self._enable_touch_scroll()
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

        # ---- 标签页 ----
        # 以前曲目/下载/规划/设备/设置/演奏/日志全都堆在一个滚动页面里, 想改个设置要滚很久。
        # 现在按用途分成4页, 每页内部照样可以滚动(外层 ScrollArea 没动)。
        # Pivot 是 qfluentwidgets 的分段导航(官方 settings 演示就是这个用法),
        # 用 currentItemChanged 而不是每个 item 各自 connect 一个 lambda,
        # 免得闭包捕获循环变量踩坑。
        page_defs = [('song', '曲目'), ('plan', '规划与设备'), ('play', '演奏'), ('log', '日志')]
        self.tab_layouts = []
        self.tab_widgets = []
        for _ in page_defs:
            w = QWidget()
            # 立刻存进 self.tab_widgets, 不要等布局建好再存: 无父对象的 QWidget 只被
            # 循环里的局部变量引用时, Python 一轮回收就把 C++ 对象销毁了, 连挂在它上面
            # 的布局一起没。上一版就是只存了布局、靠 lay.parentWidget() 回头找控件,
            # 启动时直接 RuntimeError: wrapped C/C++ object of type QVBoxLayout has been
            # deleted, 整个程序起不来。创建出来马上有人背书, 就不存在能被回收的窗口。
            self.tab_widgets.append(w)
            lay = QVBoxLayout(w)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(16)
            self.tab_layouts.append(lay)
        self.stack = QStackedWidget()
        for w in self.tab_widgets:
            self.stack.addWidget(w)
        self.pivot = Pivot(self)
        for key, text in page_defs:
            self.pivot.addItem(routeKey=key, text=text)
        # 用字典查表而不是 list.index(): 万一哪天 Pivot 多回调一个未知 key,
        # list.index 会抛 ValueError 把界面搞崩, 查表最多少切一页。
        self._tab_index = {key: i for i, (key, _) in enumerate(page_defs)}
        self.pivot.currentItemChanged.connect(
            lambda k: self.stack.setCurrentIndex(self._tab_index.get(k, 0)))
        outer.addWidget(self.pivot)
        outer.addWidget(self.stack)
        p_song, p_plan, p_play, p_log = self.tab_layouts

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
        p_song.addWidget(song_group)

        # ---- 从游戏安装包提取谱面(adb 提取 APK/OBB + 解包, 都带进度条) ----
        apk_group = SettingCardGroup('从游戏安装包提取谱面')
        self.apk_card = SettingCard(
            FIF.PHONE, '从手机提取并解包',
            '用 adb 提取 Phigros 的 APK 和 OBB(Google Play 版的谱面在 OBB 里), 再解包进谱面库')
        self.apk_pkg_edit = LineEdit()
        self.apk_pkg_edit.setText(apk_tools.PHIGROS_PACKAGE)
        self.apk_pkg_edit.setPlaceholderText('Phigros 包名')
        self.apk_pkg_edit.setToolTip('一般不用改; 设备上找不到这个包名时, 会自动找名字里带 phigros 的应用')
        self.apk_pkg_edit.setFixedWidth(260)
        self.apk_card.hBoxLayout.addWidget(self.apk_pkg_edit, 0, Qt.AlignRight)
        self.apk_card.hBoxLayout.addSpacing(16)
        apk_group.addSettingCard(self.apk_card)

        self.apk_overwrite_cb = CheckBox('覆盖已有的同名谱面')
        self.apk_overwrite_cb.setChecked(True)
        self.apk_overwrite_cb.setToolTip('谱面内容变了(游戏更新了)时, 对应的规划缓存会自动作废')
        self.apk_delete_cb = CheckBox('解包成功后删除提取出来的 APK/OBB')
        self.apk_delete_cb.setToolTip('安装包有几个 GB, 删掉省空间; 之后想再解包就得重新提取')
        apk_opt_row = QHBoxLayout()
        apk_opt_row.setSpacing(24)
        apk_opt_row.addWidget(self.apk_overwrite_cb)
        apk_opt_row.addWidget(self.apk_delete_cb)
        apk_opt_row.addStretch(1)
        apk_group.vBoxLayout.addLayout(apk_opt_row)
        apk_group.vBoxLayout.addSpacing(6)

        apk_btn_row = QHBoxLayout()
        apk_btn_row.setSpacing(8)
        self.apk_all_btn = PrimaryPushButton(FIF.DOWNLOAD, '提取并解包')
        self.apk_all_btn.clicked.connect(self._apk_pull_and_extract)
        self.apk_pull_btn = PushButton(FIF.PHONE, '仅提取安装包')
        self.apk_pull_btn.clicked.connect(self._apk_pull_only)
        self.apk_pick_btn = PushButton(FIF.FOLDER, '选择 APK/OBB 解包…')
        self.apk_pick_btn.clicked.connect(self._apk_pick_and_extract)
        self.apk_local_btn = PushButton(FIF.SYNC, '解包已提取的文件')
        self.apk_local_btn.clicked.connect(self._apk_extract_local)
        self.apk_cancel_btn = PushButton(FIF.CANCEL, '取消')
        self.apk_cancel_btn.setEnabled(False)
        self.apk_cancel_btn.clicked.connect(self._apk_cancel)
        for b in (self.apk_all_btn, self.apk_pull_btn, self.apk_pick_btn, self.apk_local_btn):
            apk_btn_row.addWidget(b)
        apk_btn_row.addStretch(1)
        apk_btn_row.addWidget(self.apk_cancel_btn)
        apk_group.vBoxLayout.addLayout(apk_btn_row)
        apk_group.vBoxLayout.addSpacing(8)

        # 进度条按千分比走: 总量(安装包可能超过 2GB)不适合直接塞进 int
        self.apk_progress = ProgressBar()
        self.apk_progress.setRange(0, 1000)
        self.apk_progress.setValue(0)
        self.apk_progress.hide()
        apk_group.vBoxLayout.addWidget(self.apk_progress)
        self.apk_status = CaptionLabel('')
        self.apk_status.setWordWrap(True)
        self.apk_status.hide()
        apk_group.vBoxLayout.addWidget(self.apk_status)
        p_song.addWidget(apk_group)

        # ---- 规划组 ----
        plan_group = SettingCardGroup('规划')
        self.algo_card = SettingCard(FIF.ROBOT, '算法', '选择规划算法')
        self.algo_box = ComboBox()
        self.algo_box.addItems(ALGORITHMS)
        self.algo_box.setMinimumWidth(180)
        self.algo_card.hBoxLayout.addWidget(self.algo_box, 0, Qt.AlignRight)
        self.algo_card.hBoxLayout.addSpacing(16)
        plan_group.addSettingCard(self.algo_card)

        self.delay_card = SettingCard(FIF.STOP_WATCH, '开始延迟 (ms)',
                                      '正值延后,负值提前; 按下"开始演奏"时生效, 不设上限')
        self.delay_spin = DoubleSpinBox()
        # 不设大小上限(见 DELAY_LIMIT_MS): 用户要求两个延迟控件都不夹范围。
        self.delay_spin.setRange(-DELAY_LIMIT_MS, DELAY_LIMIT_MS)
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
        p_plan.addWidget(plan_group)

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
        p_plan.addWidget(dev_group)

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
        p_song.addWidget(dl_group)

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
        p_play.addWidget(setting_group)

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

        # ---- 实时延迟: 与"开始延迟"分开的第二个控件 ----
        # 用户要求把延迟拆成两个: "开始延迟"在播放前设好(就是原来那个偏移框),
        # "实时延迟"放在"开始演奏"按钮下面, 播放中随时改, 下一批事件就生效。
        # 两个都不夹范围。实时延迟**不**并回开始延迟: 并回去一来用户会搞混
        # "这次调的是哪一项", 二来以前夹在±2秒, 调过的值会被悄悄截断。
        live_row = QHBoxLayout()
        live_row.setSpacing(8)
        live_row.addWidget(StrongBodyLabel('实时延迟'))
        self.live_delay_spin = DoubleSpinBox()
        self.live_delay_spin.setRange(-DELAY_LIMIT_MS, DELAY_LIMIT_MS)
        self.live_delay_spin.setSingleStep(5)
        self.live_delay_spin.setDecimals(1)
        self.live_delay_spin.setValue(0)
        self.live_delay_spin.setFixedWidth(140)
        self.live_delay_spin.setToolTip('播放中调整, 下一批事件立即生效; 正值延后, 负值提前')
        self.live_delay_spin.valueChanged.connect(self._on_live_delay)
        live_row.addWidget(self.live_delay_spin)
        live_row.addWidget(CaptionLabel('播放中随时调, 下一批事件生效'))
        live_row.addStretch(1)
        play_group.vBoxLayout.addLayout(live_row)
        p_play.addWidget(play_group)

        # ---- 日志 ----
        log_head = QHBoxLayout()
        log_head.addWidget(StrongBodyLabel('日志'))
        log_head.addStretch(1)
        self.log_export_btn = TransparentToolButton(FIF.SAVE)
        self.log_export_btn.setToolTip('导出完整日志到文件')
        self.log_export_btn.clicked.connect(self._export_log)
        log_head.addWidget(self.log_export_btn)
        p_log.addLayout(log_head)
        self.log_view = PlainTextEdit()
        self.log_view.setReadOnly(True)
        # 高一点, 并且允许用户自己拖高: 以前只有160px, 一屏就只能看几行,
        # 排查问题时来回翻很痛苦。同时每行都写进了 ./phisap.log, 不怕丢。
        self.log_view.setMinimumHeight(260)
        p_log.addWidget(self.log_view)

        # 每页末尾各留一段伸缩, 让卡片贴顶、不会被拉高
        for lay in self.tab_layouts:
            lay.addStretch(1)

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
        _log_to_file(msg)
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

    # ---- 从游戏安装包提取谱面 (adb 提取 APK/OBB + 解包, 都带进度条) ----
    # 耗时操作都在 ApkTaskThread 里跑。任务函数(下面各处的 task)只能用从界面读出来的普通值
    # (包名/序列号/开关), 进度、日志、结果全部走信号回到GUI线程, 绝不在线程里碰控件。
    def _apk_busy(self) -> bool:
        t = getattr(self, '_apk_thread', None)
        return t is not None and t.isRunning()

    def _apk_playing(self) -> bool:
        '''正在演奏, 或者正在等视觉检测开始演奏'''
        return bool(self._running or getattr(self, '_vauto_waiting', False))

    def _apk_can_start(self) -> bool:
        '''能不能启动提取/解包。演奏期间不行: 拉文件占满 USB 带宽、纯 Python 解包又会和演奏线程抢
        GIL, 都会让触控延迟抖动; 而且任务结束时刷新曲目列表会重置难度/规划, 把"停止演奏"按钮禁用掉。'''
        if self._apk_busy():
            return False
        if self._apk_playing():
            InfoBar.warning('正在演奏', '演奏时不能提取/解包(会让触控延迟抖动), 请先停止演奏',
                            parent=self.window(), position=InfoBarPosition.TOP, duration=4000)
            return False
        return True

    def _apk_serial(self):
        '''设备下拉框里选中的序列号; 空的话交给 apk_tools 现查(只有一台设备时自动选它)'''
        return self.devices_box.currentText().strip() or None

    def _apk_package(self) -> str:
        return self.apk_pkg_edit.text().strip() or apk_tools.PHIGROS_PACKAGE

    def _apk_set_running(self, running: bool):
        for b in (self.apk_all_btn, self.apk_pull_btn, self.apk_pick_btn, self.apk_local_btn):
            b.setEnabled(not running)
        self.apk_pkg_edit.setEnabled(not running)
        self.apk_cancel_btn.setEnabled(running)

    def _apk_set_bar(self, frac: float, error: bool = False):
        self.apk_progress.setValue(int(max(0.0, min(frac, 1.0)) * 1000))
        try:
            self.apk_progress.setError(error)
        except AttributeError:  # 老版本 qfluentwidgets 没有错误态
            pass

    def _apk_begin(self, task, first_text: str):
        '''启动一个后台任务, 同时把进度条亮出来'''
        if self._apk_busy():
            return
        self._apk_set_running(True)
        self._apk_set_bar(0.0)
        self.apk_progress.show()
        self.apk_status.setText(first_text)
        self.apk_status.show()
        t = ApkTaskThread(task)
        t.progress.connect(self._apk_on_progress)
        t.log_line.connect(self.log)
        t.finished_ok.connect(self._apk_done)
        t.cancelled.connect(self._apk_cancelled)
        t.failed.connect(self._apk_failed)
        t.finished.connect(self._apk_finished)
        self._apk_thread = t
        t.start()

    def _apk_pull_and_extract(self):
        if not self._apk_can_start():
            return
        serial, package = self._apk_serial(), self._apk_package()
        overwrite = self.apk_overwrite_cb.isChecked()
        delete = self.apk_delete_cb.isChecked()

        def task(progress, cancel, log):
            adb = apk_tools.Adb(apk_tools.pick_serial(apk_tools.list_devices(), serial))
            return apk_tools.pull_and_extract(
                adb.serial, package, overwrite=overwrite, delete_archives=delete,
                progress=progress, cancel=cancel, log=log, adb=adb)

        self.log(f'开始: 从设备提取 {package} 并解包谱面')
        self._apk_begin(task, '正在连接设备…')

    def _apk_pull_only(self):
        if not self._apk_can_start():
            return
        serial, package = self._apk_serial(), self._apk_package()

        def task(progress, cancel, log):
            adb = apk_tools.Adb(apk_tools.pick_serial(apk_tools.list_devices(), serial))
            return apk_tools.pull_package(adb, package, progress=progress, cancel=cancel, log=log)

        self.log(f'开始: 从设备提取 {package} 的 APK/OBB')
        self._apk_begin(task, '正在连接设备…')

    def _apk_pick_and_extract(self):
        if not self._apk_can_start():
            return
        start = apk_tools.DEFAULT_APK_DIR if os.path.isdir(apk_tools.DEFAULT_APK_DIR) else '.'
        files, _ = QFileDialog.getOpenFileNames(
            self, '选择 Phigros 的 APK / OBB(Google Play 版请把 OBB 一起选上)', start,
            'Android 安装包 (*.apk *.obb);;所有文件 (*)')
        if files:
            self._apk_extract(files)

    def _apk_extract_local(self):
        if not self._apk_can_start():
            return
        files = apk_tools.find_local_archives()
        if not files:
            InfoBar.warning('没有找到已提取的安装包',
                            f'{apk_tools.DEFAULT_APK_DIR} 里没有 APK/OBB。请先点"仅提取安装包", 或者选择文件解包',
                            parent=self.window(), position=InfoBarPosition.TOP, duration=5000)
            return
        self._apk_extract(files)

    def _apk_extract(self, files):
        files = list(files)
        overwrite = self.apk_overwrite_cb.isChecked()

        def task(progress, cancel, log):
            return apk_tools.extract_charts(files, overwrite=overwrite, progress=progress, cancel=cancel, log=log)

        self.log('开始解包: ' + ', '.join(os.path.basename(f) for f in files))
        self._apk_begin(task, '正在读取 catalog…')

    def _apk_cancel(self):
        t = getattr(self, '_apk_thread', None)
        if t is not None and t.isRunning():
            t.cancel()
            self.apk_cancel_btn.setEnabled(False)
            self.apk_status.setText('正在取消…')

    def _apk_on_progress(self, frac: float, text: str):
        if frac >= 0:
            self.apk_progress.setValue(int(min(frac, 1.0) * 1000))
        self.apk_status.setText(text)

    def _apk_done(self, result):
        self._apk_set_bar(1.0)
        if isinstance(result, apk_tools.PullResult):
            folder = os.path.dirname(result.files[0]) if result.files else apk_tools.DEFAULT_APK_DIR
            msg = f'已提取 {len(result.files)} 个文件({apk_tools.format_size(result.total_bytes)}), 保存在 {folder}'
            self.apk_status.setText(msg)
            self.log(msg)
            InfoBar.success('安装包已提取', msg + '。接着点"解包已提取的文件"提取谱面',
                            parent=self.window(), position=InfoBarPosition.TOP, duration=6000)
            return
        ext = result.extract if isinstance(result, apk_tools.PipelineResult) else result
        summary = ext.summary()
        self.apk_status.setText('完成: ' + summary)
        cur_diff = self.diff_box.currentText()
        playing = self._apk_playing()
        if not playing:
            self.refresh_songs()
        else:
            self.log('正在演奏, 暂不刷新曲目列表; 演奏结束后点搜索栏旁边的刷新按钮即可看到新谱面')
        if not playing and self.song_box.currentText():
            # refresh_songs 只刷新曲目列表, 不会更新难度下拉框(谱库原来是空的话, 选中了第一首歌
            # 难度却是空的); 谱面被更新时界面里挂着的旧规划也已经作废。重新载入一遍当前曲目,
            # 并尽量保留用户原来选的难度。播放中不能动: 这会把"停止演奏"按钮禁用掉。
            self.song_selected(self.song_box.currentText())
            if cur_diff and self.diff_box.findText(cur_diff) >= 0 and self.diff_box.currentText() != cur_diff:
                self.diff_box.setCurrentText(cur_diff)
        if ext.overwritten:
            self.log(f'有 {ext.overwritten} 份谱面内容发生了变化, 它们的旧规划缓存已作废, 请重新规划')
        if ext.failed:
            InfoBar.warning('解包完成, 但有失败', summary + '(失败原因见日志)',
                            parent=self.window(), position=InfoBarPosition.TOP, duration=8000)
        else:
            InfoBar.success('解包完成', summary, parent=self.window(), position=InfoBarPosition.TOP, duration=6000)

    def _apk_cancelled(self):
        self.apk_status.setText('已取消')
        self.log('已取消')
        InfoBar.info('已取消', '已写入的谱面保留, 没传完的文件已清理',
                     parent=self.window(), position=InfoBarPosition.TOP, duration=3000)

    def _apk_failed(self, msg: str):
        first = msg.strip().splitlines()[0] if msg.strip() else '未知错误'
        self._apk_set_bar(self.apk_progress.value() / 1000, True)  # 条变红, 停在出错的位置
        self.apk_status.setText('失败: ' + first)
        self.log('提取/解包失败:\n' + msg)
        InfoBar.error('提取/解包失败', first[:200], parent=self.window(),
                      position=InfoBarPosition.TOP, duration=8000)

    def _apk_finished(self):
        self._apk_set_running(False)

    def _apk_shutdown(self):
        '''退出前让后台的提取/解包收尾(会停掉 adb、清掉没传完的半截文件)。
        QThread 在运行中被销毁会直接崩溃, 所以必须等它结束。'''
        t = getattr(self, '_apk_thread', None)
        if t is not None and t.isRunning():
            t.cancel()
            t.wait(5000)

    def import_songs(self):
        files, _ = QFileDialog.getOpenFileNames(self, '选择谱面 JSON', '', 'JSON Files (*.json)')
        if not files:
            return
        ok = 0
        for fp in files:
            try:
                if is_plan_cache(fp):
                    # 规划缓存也是合法JSON, 不拦的话会被存成一张空谱面,
                    # 曲目列表里多出一个莫名其妙的新歌。
                    ok += 1
                    self.log(f'跳过规划缓存文件(不是谱面): {os.path.basename(fp)}')
                    continue
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
        if offset:
            self.log(f'已应用偏移 {offset * 1000:+.1f} ms')
        self.log(f'同步完成,偏移 {offset*1000:.1f} ms')
        self._start_playback(manual=False)

    def run(self):
        if self._apk_busy():
            InfoBar.warning('正在提取/解包', '请等它完成, 或点"取消"后再开始演奏(传文件会让触控延迟抖动)',
                            parent=self.window(), duration=4000)
            return
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
        # 实时延迟从0开始: "开始延迟"负责播放前的整体偏移, "实时延迟"负责
        # 播放中的临场补偿, 两个控件互不覆盖。
        self.live_delay_spin.blockSignals(True)
        self.live_delay_spin.setValue(0)
        self.live_delay_spin.blockSignals(False)
        self._fine_tune = 0.0

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
                adapted, _unused = got
                offset = self.delay_spin.value() / 1000.0
                if manual:
                    fnm = first_note_ms_from_path(self._find_chart_path())
                    adapted = manual_start_plan(adapted, fnm)
                    # 偏移正负号与 sync_ms 一致: 正值=打歌时钟往后推=事件延后。
                    # 手动开始以前完全不读这个偏移, 用户调了没反应, 等于没有补偿手段。
                    self._start_time = (time.perf_counter() - adapted[0][0] / 1000
                                        - MANUAL_START_LEAD + offset)
                    if offset:
                        self.log(f'已应用偏移 {offset * 1000:+.1f} ms')
                elif prestarted:
                    # 视觉自动开始: 锚点不是"现在", 而是"检测到界面跳变的时刻"(_vauto_fire_time),
                    # 这样打歌时钟与Phigros内部时钟同源, 每首歌(不管第一音符在第几秒)都自动对齐。
                    fnm = first_note_ms_from_path(self._find_chart_path())
                    adapted = manual_start_plan(adapted, fnm)
                    self._start_time = self._vauto_fire_time - adapted[0][0] / 1000 + offset
                # ---- 触点状态实时跟踪 ----
                # 以前的写法是"把整份规划跑一遍, 看最后还有谁没抬起", 得到的是
                # **规划结束时**的状态。一份完整规划里所有触点最后都会被抬起, 于是这个
                # 集合基本永远是空的 —— 点停止时一个UP都发不出去。
                # 扫屏触点是"第一个音符前按下、最后一个音符后抬起", 正好永远不在这个
                # 集合里, 所以用户看到的就是"扫屏触点停不下来"。
                # 现在跟着发送进度实时维护: self._active_pids 就是这个set对象本身,
                # 停止时读到的就是"当下真正按在屏幕上的是哪几个"。
                # 开打前先把可能残留的触点状态清干净。
                # 用户用手指点过屏幕后, Android 会给应用发 ACTION_CANCEL, 应用的触点
                # 被取消了, 但 scrcpy-server 内部的 PointersState 仍然认为那些触点
                # 按着。不清掉的话, 下一轮用同一个 pointerId 发DOWN会被当成
                # "已经按下了"而失效 —— 用户看到的就是"必须重启模拟器和程序才能恢复"。
                stale = (set(getattr(self, '_all_pids', None) or ())
                         | {e.pointer for _, evs in adapted for e in evs})
                if stale:
                    try:
                        self.controller.release_pointers(sorted(stale))
                    except Exception:
                        pass

                tracker = PointerTracker()
                self._active_pids = tracker.live
                self._all_pids = tracker.all_pids
                if not adapted:
                    self.log('规划为空, 没有可发送的事件')
                    return
                first = adapted[0]
                rest = iter(adapted[1:])

                def send(events):
                    tracker.update(events)
                    self.controller.touch_many(events)

                if manual or prestarted:
                    stats = run_player(send, rest,
                                       lambda: self._start_time + self._fine_tune,
                                       lambda: self._running,
                                       first_event=first,
                                       should_continue=lambda: my_gen == self._playback_gen)
                else:
                    stats = run_player(send, iter(adapted),
                                       lambda: self._start_time + self._fine_tune,
                                       lambda: self._running,
                                       should_continue=lambda: my_gen == self._playback_gen)
                self.log('演奏结束')
                if stats is not None:
                    for line in stats.summary(0):
                        self.log(line)
            except Exception as e:
                # 触控通道断开时给出明确结论 —— 不要只说一句"演奏出错",
                # 那看起来像bug, 而实际是连接断了, 重新连设备就能好。
                if type(e).__name__ == 'ControlChannelDead':
                    self.log(f'触控通道已断开, 无法继续注入: {e}\n请重新连接设备后再演奏。')
                else:
                    self.log('演奏出错:\n' + traceback.format_exc())
            finally:
                # 播放结束(无论正常/异常/手动停止)都释放残余触点,避免手指卡在屏幕上
                self._release_all_active()
                # 通知GUI线程复位按钮。必须用信号: invokeMethod 找不到未注册的Python方法
                self.playback_finished.emit()

        self._player_thread = Thread(target=worker, daemon=True)
        self._player_thread.start()
    # ---- 实时延迟 ----
    def _on_live_delay(self, v):
        """拖动"实时延迟"旋钮: 平移打歌时钟, 下一批事件即生效。

        和"开始延迟"是两个独立控件: 开始延迟在播放前设好并缓存, 实时延迟只影响
        当前这一轮, 不并回开始延迟(并回去一来用户分不清调的是哪一项, 二来以前
        夹在±2秒, 调过的值会被悄悄截断)。
        """
        self._fine_tune = v / 1000.0
        if self._running:
            self.log(f'实时延迟 {v:+.1f} ms (下一批事件生效)')

    def _export_log(self):
        """把完整日志另存一份给用户(界面里看的 + ./phisap.log 里的都在)"""
        try:
            text = self.log_view.toPlainText()
            path, _ = QFileDialog.getSaveFileName(self, '导出日志', 'phisap-log.txt',
                                                  '文本文件 (*.txt)')
            if not path:
                return
            with io.open(path, 'w', encoding='utf-8') as f:
                f.write(text)
            # 顺便把落盘的那份也带上(它比界面里更完整)
            if os.path.exists(LOG_FILE):
                try:
                    with io.open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                        disk = f.read()
                    with io.open(path, 'a', encoding='utf-8') as f:
                        if not text.endswith('\n'):
                            f.write('\n')
                        f.write('\n===== ./phisap.log 完整记录 =====\n')
                        f.write(disk)
                except Exception:
                    pass
            self.log(f'日志已导出: {path}')
            InfoBar.success('已导出', path, parent=self.window(), duration=3000)
        except Exception as e:
            self.log(f'导出日志失败: {e}')

    def _release_all_active(self):
        """给所有仍处于按下状态的程序触点发送 UP 事件,防止手指卡住。

        释放范围 = 当前真正按下的(live) ∪ 这一轮规划用过的全部触点(all_pids)。
        后半部分是必须的: 用户用手指点过屏幕后, Android 会给应用发 ACTION_CANCEL,
        应用的触点被取消了, 但 scrcpy-server 内部的 PointersState 仍然认为那些
        触点按着 —— 只发live里的UP清不掉它的状态, 下一轮播放再用同一个pointerId
        发DOWN就会被当成"已经按下了"而失效, 于是必须重启模拟器才能恢复。
        """
        try:
            ctrl = self.controller
            if ctrl is None:
                return
            # 只看控制通道。视频流断不断跟能不能抬手指毫无关系, 以前这里判的是
            # collector_running(视频), 视频一抖就连UP都发不出去。
            if not getattr(ctrl, 'control_running', True):
                return
            pids = set(getattr(self, '_active_pids', None) or ())
            pids |= set(getattr(self, '_all_pids', None) or ())   # live ∪ all_pids
            if not pids:
                return
            # 抬起位置用屏幕中心即可: Android 按 pointerId 匹配, 坐标不影响抬起语义。
            # 具体怎么发(scrcpy 控制socket / MaaTouch 指令)由后端自己封装。
            ctrl.release_pointers(sorted(pids))
            self.log(f'已释放 {len(pids)} 个触点(当前按下 {len(self._active_pids or ())} 个)')
            if isinstance(self._active_pids, set):
                self._active_pids.clear()
        except Exception as e:
            self.log(f'释放残余触点失败(可忽略): {e}')

    def _stop(self):
        self._running = False
        self._vauto_waiting = False
        self._vauto_fire_time = 0.0
        # 代际+1: 让当前worker立刻停止发送新事件(不等它自己发现_running变了)
        self._playback_gen += 1
        # 立刻抓住当前worker线程对象。必须现在就抓 —— 用户随时可能再点开始,
        # 那时 self._player_thread 会被换成新线程, 旧的_stop_async就会join错人。
        t = self._player_thread
        self.log('正在停止...')
        # 立刻在GUI线程复位按钮, 不等播放线程——它可能卡在发送上永远出不来。
        self._reset_go()
        # "真正让手指离开屏幕"由 worker 自己的 finally 完成: 只有它能保证一定发生在
        # 自己最后一次写socket之后, 也不会误伤随后开始的新一轮播放(新一轮会覆盖
        # _active_pids, 而旧线程此刻才发UP就会把新播放的手指也抬起来)。
        # 这里只负责等它退出; 等不到才强制断开。
        Thread(target=self._stop_async, args=(t,), daemon=True).start()

    def _stop_async(self, t):
        '''(后台线程) 等播放线程真正退出。触点释放交给 worker 自己的 finally。'''
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
                if t.is_alive() and self._player_thread is t:
                    # 极端情况: finally都没跑到。兜底抬一下已知触点。
                    # 加 self._player_thread is t 判断, 避免误抬新一轮播放的手指。
                    self.log('播放线程仍未能退出, 触点可能残留, 建议重新连接设备')
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
        # 演奏结束(或手动停止)时把"实时延迟"归零。
        # 它是播放中的临时补偿: 用户边放边调, 调完这一轮就使命完成了。
        # 不清零的话下一轮会 silently 继承上次的补偿量, 表现为"这次没调却还是有延迟",
        # 而用户早就忘了自己上一轮拧过这个旋钮。开始演奏时也会归零一次(见_start_playback),
        # 但那一轮播放中拧的值必须在这里收尾, 否则停止/自然结束后仍然留着。
        # 注意 setValue(0) 在值本来就是0时不会发 valueChanged, 所以 _fine_tune 要显式清零。
        if abs(self._fine_tune) > 1e-9:
            self.log(f'实时延迟已归零(本轮播放中为 {self._fine_tune * 1000:+.1f} ms)')
        try:
            self.live_delay_spin.setValue(0)
        except Exception:
            pass
        self._fine_tune = 0.0
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
        self._apk_shutdown()
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

    def closeEvent(self, e):
        # MainPage 是子控件, 关主窗口时它自己的 closeEvent 不会被调用, 所以由主窗口转交。
        # 这里只收尾后台任务, 不碰 MainPage.closeEvent 里的 save_cache。
        try:
            self.main_page._apk_shutdown()
        except Exception:
            pass
        super().closeEvent(e)


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
    apk_tools.add_local_adb_to_path()  # phisap 目录下的 platform-tools/adb 让整个程序都能用上
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
