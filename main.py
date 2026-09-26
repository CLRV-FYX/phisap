import configparser
import json
import locale
import os
import re
import shutil
import subprocess
import sys
from tkinter import ttk, messagebox, Tk, W, X, EW, IntVar, StringVar, DoubleVar, filedialog, simpledialog
from typing import Iterator
from algo.algo_base import TouchEvent
from threading import Thread

from chart import Chart
from control import DeviceController, max_touch_points
from rpe import detect_kind, rpe_to_official_v3
from algo.algo_base import load_from_json, export_to_json, PLAN_CACHE_SUFFIX, first_note_ms, manual_start_plan

from rich.console import Console


# 规划算法(下拉列表中的顺序)
# algo3: 扫屏(4行, 只在drag/flick附近扫); algo3f: 坐标轴扫屏(横竖各1个, 全程扫)
ALGORITHMS = ('algo3', 'algo3f', 'algo1', 'algo2')


def agreement():
    if os.path.exists('./cache'):
        return
    if not messagebox.askyesno(title='用户协定', message='您因使用或修改本程序发生的一切后果将由您自己承担而与程序原作者无关。\n' '您是否同意？'):
        sys.exit(0)


# 从谱面文件名识别难度。兼容: Chart_AT.json / Chart_AT #4159.json / chart_at_4159.json /
# Chart_DT_4874a.json 等常见命名。取"chart"之后的第一个字母串，与已知难度表比对。
_KNOWN_DIFFICULTIES = ('SPB', 'INB', 'HDB', 'ATB', 'SP', 'IN', 'HD', 'AT', 'DT')
_CHART_TOKEN_RE = re.compile(r'chart[_\s\-#]?([a-z]+)', re.IGNORECASE)


def chart_difficulty(filename: str) -> str | None:
    """从谱面文件名中提取难度，无法识别时返回None"""
    base = os.path.basename(filename)
    if base.lower() == 'chart.json':
        # 游戏旧版本中Chart.json即SP谱
        return 'SP'
    m = _CHART_TOKEN_RE.search(base)
    if not m:
        return None
    token = m.group(1).upper()
    for d in _KNOWN_DIFFICULTIES:
        if token == d:
            return d
    # 容错: 难度字母后紧跟其他内容(如ATX)时取难度前缀
    for d in _KNOWN_DIFFICULTIES:
        if token.startswith(d) and len(token) <= len(d) + 2:
            return d
    return None


def guess_difficulty(filename: str) -> str | None:
    return chart_difficulty(filename)


def list_difficulties(songid: str) -> list[str]:
    try:
        files = os.listdir(os.path.join('./Assets/Tracks', songid))
    except FileNotFoundError:
        return []
    return sorted({
        diff for f in files
        if 'ans' not in f and (diff := chart_difficulty(f))
    })


def find_chart_path(songid: str, difficulty: str) -> str:
    """定位谱面文件：优先Chart_<难度>.json，其次任意命名但该难度的谱面(如手动放入的新版命名文件)"""
    tracks_dir = os.path.join('./Assets/Tracks', songid)
    direct = os.path.join(tracks_dir, f'Chart_{difficulty}.json')
    if os.path.exists(direct):
        return direct
    for f in sorted(os.listdir(tracks_dir)):
        if 'ans' not in f and chart_difficulty(f) == difficulty:
            return os.path.join(tracks_dir, f)
    raise FileNotFoundError(f'未找到谱面文件: {songid} / {difficulty}')


def has_ans_cache(songid: str, difficulty: str) -> bool:
    """该难度是否已有(当前版本的)规划缓存; 旧版本的 .ans.json 缓存会被忽略"""
    tracks_dir = os.path.join('./Assets/Tracks', songid)
    direct = os.path.join(tracks_dir, f'Chart_{difficulty}.json')
    if os.path.exists(direct + PLAN_CACHE_SUFFIX):
        return True
    try:
        return os.path.exists(find_chart_path(songid, difficulty) + PLAN_CACHE_SUFFIX)
    except FileNotFoundError:
        return False


def open_tracks_dir():
    """在文件管理器中打开谱面库目录(便于直接拖放谱面文件)"""
    tracks = './Assets/Tracks'
    os.makedirs(tracks, exist_ok=True)
    if sys.platform.startswith('win'):
        os.startfile(tracks)  # type: ignore[attr-defined]
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', tracks])
    else:
        subprocess.Popen(['xdg-open', tracks])


def import_charts(app: 'App') -> None:
    """导入谱面文件：支持各版本官谱(v1/v2/v3, 原样保存)与RPE谱面(转换为v3格式保存)"""
    paths = filedialog.askopenfilenames(
        title='选择要导入的谱面文件（可多选：各版本官谱或RPE谱面）',
        filetypes=[('谱面文件', '*.json'), ('所有文件', '*.*')])
    if not paths:
        return
    results: list[str] = []
    last_imported: tuple[str, str] | None = None
    last_songid = app.song_id.get()
    top = app.winfo_toplevel()

    for path in paths:
        name = os.path.basename(path)
        try:
            with open(path, 'r', encoding='utf-8-sig', newline='') as f:
                data = json.load(f)
        except Exception as e:
            results.append(f'✗ {name}: 读取失败({e.__class__.__name__}: {e})')
            continue
        kind = detect_kind(data)
        if kind == 'unknown':
            results.append(f'✗ {name}: 无法识别的格式(仅支持官谱v1/v2/v3与RPE谱面)')
            continue

        diff = guess_difficulty(name)
        if not diff:
            diff = simpledialog.askstring(
                '难度', f'无法从文件名 {name} 识别难度。\n请输入难度(如 SP/IN/HD/AT/DT):', parent=top)
            if not diff:
                results.append(f'✗ {name}: 已取消(未输入难度)')
                continue
            diff = diff.strip().upper()

        kind_desc = f'官谱v{data["formatVersion"]}' if kind == 'official' else 'RPE谱面'
        songid = simpledialog.askstring(
            '曲目ID',
            f'{name} ({kind_desc}, 难度{diff})\n它属于哪个曲目？\n请输入曲目ID(将作为谱面目录名):',
            initialvalue=last_songid, parent=top)
        if not songid or not songid.strip():
            results.append(f'✗ {name}: 已取消(未输入曲目ID)')
            continue
        songid = songid.strip()
        last_songid = songid

        warns: list[str] = []
        try:
            target_dir = os.path.join('./Assets/Tracks', songid)
            os.makedirs(target_dir, exist_ok=True)
            target = os.path.join(target_dir, f'Chart_{diff}.json')
            if os.path.exists(target) and not messagebox.askyesno(
                    '覆盖', f'已存在:\n{target}\n确定覆盖？', parent=top):
                results.append(f'✗ {name}: 已取消(目标文件已存在)')
                continue
            if kind == 'rpe':
                out, warns = rpe_to_official_v3(data)
                with open(target, 'w', encoding='utf-8') as f:
                    json.dump(out, f, ensure_ascii=False)
                note = 'RPE已转换为v3格式'
            else:
                shutil.copyfile(path, target)
                note = f'官谱v{data["formatVersion"]}原样保存'
            results.append(f'✓ {name} → {target} ({note})')
            last_imported = (songid, diff)
        except Exception as e:
            results.append(f'✗ {name}: 写入失败({e.__class__.__name__}: {e})')
            continue
        for w in warns:
            results.append(f'    [warn] {w}')

    app.refresh_songs()
    if last_imported:
        app.song_id.set(last_imported[0])
        app.difficulty.set(last_imported[1])
        app.refresh_difficulties()
    messagebox.showinfo('导入完成', '\n'.join(results), parent=top)


# 屏幕尺寸预设：常见手机分辨率（横屏/竖屏），用户也可以手动输入任意"宽×高"
SCREEN_PRESETS = [
    '自动',
    '1080×1920', '1080×2340', '1080×2400', '1260×2720', '1440×3120',
    '1920×1080', '2340×1080', '2400×1080', '2560×1440',
]

_SCREEN_SIZE_RE = re.compile(r'^\s*(\d+)\s*[×xX: ]*\s*(\d+)\s*$')


class App(ttk.Frame):
    SYNC_MODE_DELAY = 0
    SYNC_MODE_MANUAL = 1

    cache: configparser.ConfigParser | None
    serials: list[str]
    running: bool
    start_time: float
    controller: DeviceController | None
    player_worker_thread: Thread | None
    console: Console

    def __init__(self, master: Tk):
        super().__init__(master)
        self.console = Console()
        self.controller = None
        self.player_worker_thread = None
        self.cache_path = None
        self.running = True
        self.start_time = 0.0
        self.cache = None
        self.pack(fill=X, expand=1)

        frm = ttk.Frame(self, padding=10)
        frm.pack(fill=X)
        frm.columnconfigure(0, weight=1)

        # ---- 工具栏 ----
        tb = ttk.Frame(frm)
        tb.grid(row=0, column=0, sticky=EW, pady=(0, 6))
        ttk.Button(tb, text='导入谱面', command=lambda: import_charts(self)).pack(side='left', padx=(0, 6))
        ttk.Button(tb, text='打开谱面目录', command=open_tracks_dir).pack(side='left')

        def section(row: int, title: str) -> ttk.Frame:
            lf = ttk.LabelFrame(frm, text=title, padding=8)
            lf.grid(row=row, column=0, sticky=EW, pady=4)
            lf.columnconfigure(1, weight=1)
            return lf

        # ---- 曲目 ----
        sec = section(1, '曲目')
        ttk.Label(sec, text='曲目ID：').grid(column=0, row=0, sticky=W)
        self.song_id = StringVar()
        self.songs_select = ttk.Combobox(sec, state='readonly', values=[], textvariable=self.song_id)
        self.songs_select.grid(column=1, row=0, sticky=EW, padx=4)
        self.songs_select.bind('<<ComboboxSelected>>', self.song_selected)
        ttk.Label(sec, text='难度：').grid(column=0, row=1, sticky=W)
        self.difficulty = StringVar()
        self.difficulties_select = ttk.Combobox(sec, state='readonly', values=[], textvariable=self.difficulty)
        self.difficulties_select.grid(column=1, row=1, sticky=EW, padx=4)
        self.difficulties_select.bind('<<ComboboxSelected>>', self.difficulty_selected)

        # ---- 规划 ----
        sec = section(2, '规划')
        ttk.Label(sec, text='规划算法：').grid(column=0, row=0, sticky=W)
        self.algo = StringVar()
        self.algo_select = ttk.Combobox(sec, state='readonly', values=[], textvariable=self.algo)
        self.algo_select.grid(column=1, row=0, sticky=EW, padx=4)

        # ---- 设备与屏幕 ----
        sec = section(3, '设备与屏幕')
        ttk.Label(sec, text='设备Serial: ').grid(column=0, row=0, sticky=W)
        self.serial = StringVar()
        self.serial_select = ttk.Combobox(sec, state='readonly', values=[], textvariable=self.serial)
        self.serial_select.grid(column=1, row=0, sticky=EW, padx=4)
        self.serial_select.bind('<<ComboboxSelected>>', self.adb_serial_selected)
        ttk.Button(sec, text='刷新', command=self.detect_adb_devices).grid(column=2, row=0, padx=(0, 10))
        ttk.Label(sec, text='屏幕尺寸: ').grid(column=0, row=1, sticky=W)
        self.screen_size = StringVar()
        self.screen_size.set('自动')
        self.screen_select = ttk.Combobox(sec, values=SCREEN_PRESETS, textvariable=self.screen_size, width=16)
        self.screen_select.grid(column=1, row=1, sticky=W, padx=4)
        ttk.Label(sec, text='（自动=设备实际尺寸；可手动输入 宽×高，如 1920×1080）').grid(column=2, row=1, sticky=W)

        # ---- 计时器同步 ----
        self.sync_mode = IntVar()
        self.sync_mode.set(self.SYNC_MODE_MANUAL)
        sec = section(4, '计时器同步')
        self.sync_mode2 = ttk.Radiobutton(
            sec, text='手动同步', variable=self.sync_mode, value=self.SYNC_MODE_MANUAL, command=self.sync_mode_changed)
        self.sync_mode1 = ttk.Radiobutton(
            sec, text='延时同步', variable=self.sync_mode, value=self.SYNC_MODE_DELAY, command=self.sync_mode_changed)
        self.sync_mode2.grid(column=0, row=0, sticky=W)
        self.sync_mode1.grid(column=1, row=0, sticky=W, padx=(16, 0))
        self.delay_lbl = ttk.Label(sec, text='延时时长：')
        self.delay_lbl.grid(column=0, row=1, sticky=W)
        self.delay = DoubleVar()
        self.delay_input = ttk.Spinbox(sec, increment=0.01, textvariable=self.delay, from_=-100, to=100)
        self.delay_input.grid(column=1, row=1, sticky=W, padx=4)
        self.delay_input['state'] = 'disabled'
        ttk.Label(sec, text='秒').grid(column=2, row=1, sticky=W)

        # ---- 开始按钮 ----
        self.go = ttk.Button(frm, text='开始!', command=self.run)
        self.go.grid(row=5, column=0, sticky=EW, pady=8, ipady=4)

        self.info_label = ttk.Label(frm, wraplength=560, justify='left')
        self.info_label.grid(row=6, column=0, sticky=EW)

        agreement()

    def sync_mode_changed(self):
        if self.sync_mode.get() == 0:  # delay
            self.info_label['text'] = 'Tip: 请开始游戏，再暂停游戏，然后再点击上面的开始按钮'
            self.delay_input['state'] = 'normal'
        else:  # tap
            self.info_label['text'] = ''
            self.delay_input['state'] = 'disabled'

    def refresh_songs(self):
        try:
            self.songs_select['values'] = sorted(os.listdir('./Assets/Tracks'))
        except FileNotFoundError:
            self.songs_select['values'] = []

    def load_songs(self):
        self.refresh_songs()
        if not len(self.songs_select['values']):
            messagebox.showinfo(
                '谱面库为空',
                'phisap需要依赖谱面文件才能工作，然而您当前的谱面库为空\n'
                '请点击左上角"导入谱面"按钮，选择谱面文件导入\n'
                '支持各版本官方谱面(v1/v2/v3, 如Chart_AT.json)与RPE谱面(JSON格式)\n'
                '导入的谱面保存在 ./Assets/Tracks/<曲目ID>/ 下，也可以直接把json文件放到该目录',
            )
        return self

    def load_cache(self, cache_path):
        self.cache_path = cache_path
        cache = configparser.ConfigParser()

        if os.path.exists(cache_path):
            try:
                cache.read(cache_path, encoding='utf-8')
            except (UnicodeDecodeError, configparser.Error):
                # 旧版本按系统默认编码(如GBK)写入的缓存，或已损坏的缓存
                try:
                    cache = configparser.ConfigParser()
                    cache.read(cache_path, encoding=locale.getpreferredencoding(False))
                except (UnicodeDecodeError, configparser.Error):
                    cache = configparser.ConfigParser()

        if not cache.has_section('cache'):
            cache.add_section('cache')

        for key in ('songid', 'difficulty', 'algo'):
            if not cache.has_option('cache', key):
                cache.set('cache', key, '')
        songid = cache.get('cache', 'songid')
        # 曲目可能已被删除：只恢复仍存在于谱面库中的曲目
        songs = [str(v) for v in (self.songs_select['values'] or ())]
        self.song_id.set(songid if songid in songs else '')
        self.difficulty.set(cache.get('cache', 'difficulty'))
        self.algo.set(cache.get('cache', 'algo'))
        self.refresh_difficulties()

        try:
            self.delay.set(cache.getfloat('cache', 'offset'))
        except configparser.NoOptionError:
            cache.set('cache', 'offset', '1.95')

        try:
            self.screen_size.set(cache.get('cache', 'screen'))
        except configparser.NoOptionError:
            cache.set('cache', 'screen', '自动')

        self.cache = cache

        return self

    def detect_adb_devices(self):
        self.serial_select['values'] = DeviceController.get_devices()
        return self

    def resolve_screen_size(self, device_width: int, device_height: int) -> tuple[int, int]:
        """返回实际设备像素尺寸。

        "自动"时取设备实际尺寸（scrcpy探测），否则按用户输入的"宽×高"解析。
        """
        text = (self.screen_size.get() or '').strip()
        if not text or text == '自动':
            if device_width and device_height:
                return int(device_width), int(device_height)
            # 设备尺寸尚未探测到时，退化为16:9标准画布
            return 1280, 720
        m = _SCREEN_SIZE_RE.match(text)
        if not m:
            raise ValueError(f'无法识别的屏幕尺寸"{text}"，请输入 宽×高 的形式，例如 1920×1080')
        return int(m.group(1)), int(m.group(2))

    def resolve_serial(self) -> str:
        """确定要连接的设备：优先使用界面中选择的设备；未选择时仅在只有一台设备时自动选中"""
        serial = (self.serial.get() or '').strip()
        devices = DeviceController.get_devices()
        self.serial_select['values'] = devices
        if serial:
            if serial not in devices:
                raise RuntimeError(f'所选设备 {serial} 当前未连接（已连接: {devices or "无"}）\n请检查连接后点"刷新"重新选择。')
            return serial
        if not devices:
            raise RuntimeError('未检测到任何设备，请连接手机（开启USB调试）或启动模拟器后点"刷新"。')
        if len(devices) > 1:
            raise RuntimeError(f'检测到多个设备: {", ".join(devices)}\n请先在"设备Serial"中选择要使用的设备。')
        self.serial.set(devices[0])
        return devices[0]

    def adb_serial_selected(self, event):
        serial = event.widget.get()
        print(serial)

    ALGO_CACHED = '不规划(使用缓存)'

    def refresh_difficulties(self):
        """根据当前曲目刷新难度列表；当前难度无效时自动选中第一个难度，并刷新算法列表"""
        songid = self.song_id.get()
        diffs = list_difficulties(songid) if songid else []
        self.difficulties_select['values'] = diffs
        if self.difficulty.get() not in diffs:
            self.difficulty.set(diffs[0] if diffs else '')
        self.refresh_algos()

    def refresh_algos(self):
        """根据当前曲目/难度刷新算法列表；当前算法无效时自动选择默认算法（有规划缓存时默认使用缓存）"""
        songid, diff = self.song_id.get(), self.difficulty.get()
        algos = list(ALGORITHMS)
        if songid and diff and has_ans_cache(songid, diff):
            algos.insert(0, self.ALGO_CACHED)
        self.algo_select['values'] = algos
        if self.algo.get() not in algos:
            self.algo.set(algos[0])

    def song_selected(self, event):
        self.refresh_difficulties()

    def difficulty_selected(self, event):
        self.refresh_algos()

    def run(self):
        try:
            import time

            # 先检查选项和设备再规划，避免规划完才发现问题
            if not self.song_id.get():
                raise RuntimeError('请先选择曲目（谱面库为空时请先点击"导入谱面"）')
            if not self.difficulty.get():
                raise RuntimeError('请先选择难度')
            if not self.algo.get():
                self.refresh_algos()
            serial = self.resolve_serial()

            chart_path = find_chart_path(self.song_id.get(), self.difficulty.get())

            with open(chart_path, encoding='utf-8-sig') as f:
                chart = Chart.from_dict(json.load(f))

            assert self.cache
            assert self.cache_path
            self.cache.set('cache', 'songid', self.song_id.get())
            self.cache.set('cache', 'difficulty', self.difficulty.get())
            self.cache.set('cache', 'offset', str(self.delay.get()))
            self.cache.set('cache', 'screen', self.screen_size.get())
            self.cache.set('cache', 'algo', self.algo.get())
            with open(self.cache_path, 'w', encoding='utf-8') as f:
                self.cache.write(f)

            algo_method = self.algo.get()
            ans: dict
            ans_file = chart_path + PLAN_CACHE_SUFFIX
            if algo_method == self.ALGO_CACHED:
                with open(ans_file, encoding='utf-8') as f:
                    ans = load_from_json(f)
            elif algo_method in ALGORITHMS:
                import importlib

                solver = importlib.import_module(f'algo.{algo_method}')
                ans = solver.solve(chart, self.console, max_touch_points())
                with open(ans_file, 'w', encoding='utf-8') as f:
                    export_to_json(ans, f)
            else:
                raise RuntimeError(f'未知的规划算法: "{algo_method}"，请在"规划算法"中重新选择')
            self.refresh_algos()

            if self.controller is not None and (self.controller.serial != serial or not self.controller.collector_running):
                # 用户切换了设备，或上次的连接已中断：断开旧连接后重连
                self.controller.close()
                self.controller = None

            if self.controller is None:
                print('[client]', f'正在连接设备: {serial}')
                self.controller = DeviceController(serial)

            # 视频尺寸由 scrcpy-server 在连接时直接告知，触控坐标以此为坐标系
            device_width = self.controller.device_width
            device_height = self.controller.device_height
            if device_height > device_width and not messagebox.askyesno(
                    '设备为竖屏',
                    f'设备当前为竖屏({device_width}x{device_height})。\n'
                    'Phigros 是横屏游戏，请先在设备上打开 Phigros 并进入选曲/游戏界面，再点"开始"，'
                    '否则触控坐标会错位。\n\n仍然继续吗？'):
                return

            width, height = self.resolve_screen_size(device_width, device_height)

            # Phigros逻辑画布为1280×720：按contain方式居中适配到实际屏幕，
            # 非16:9屏幕（如20:9的2400×1080）时留边而非拉伸
            scale_factor = min(width / 1280, height / 720)
            xoffset = (width - 1280 * scale_factor) / 2
            yoffset = (height - 720 * scale_factor) / 2
            print('[client]', f'屏幕尺寸: {width}x{height}, 缩放: {scale_factor:.4f}, 偏移: ({xoffset:.1f},{yoffset:.1f})')

            # 手动指定屏幕尺寸时，换算到 scrcpy 视频坐标系（两者一致时 sx=sy=1）
            sx, sy = device_width / width, device_height / height
            if (width, height) != (device_width, device_height):
                print('[client]', f'视频尺寸 {device_width}x{device_height} 与屏幕尺寸不同，坐标按 {sx:.4f}x{sy:.4f} 换算')
            adapted_ans = [
                (timestamp, [ev.map_to(xoffset * sx, yoffset * sy, scale_factor * sx, scale_factor * sy) for ev in ans[timestamp]])
                for timestamp in sorted(ans.keys())
            ]

            ans_iter = iter(adapted_ans)

            pre_info = self.info_label['text']
            pre_command = self.go['command']
            pre_text = self.go['text']
            pre_delay_lbl = self.delay_lbl['text']
            pre_delay_var = self.delay_input['textvariable']

            delay_offset = DoubleVar()
            delay_offset.set(0)

            if self.sync_mode.get() == self.SYNC_MODE_DELAY:
                self.controller.tap(device_width >> 1, device_height >> 1)
                offset = self.delay.get()

                self.info_label['text'] = '准备就绪'

                def stop():
                    self.running = False

                self.go['command'] = stop
                self.go['text'] = '取消'

                self.delay_lbl['text'] = '微调(正为延后，负为提前)：'
                self.delay_input['state'] = 'normal'
                self.delay_input['textvariable'] = delay_offset

                def incremented(_):
                    self.start_time += 0.01

                def decremented(_):
                    self.start_time -= 0.01

                self.delay_input.bind('<<Increment>>', incremented)
                self.delay_input.bind('<<Decrement>>', decremented)

                # perf_counter: Windows上Python 3.12的time.time()精度只有约15.6ms
                self.start_time = time.perf_counter() + offset

                begin = False
                self.running = True
                self.console.print('正在等待')

                timestamp, events = next(ans_iter)
                try:
                    while self.running:
                        self.update()
                        now = round((time.perf_counter() - self.start_time) * 1000)
                        if now >= timestamp:
                            if not begin:
                                self.info_label['text'] = '开始操作'
                                self.console.print('开始操作')
                                begin = True
                            for event in events:
                                self.controller.touch(*event.pos, event.action, pointer_id=event.pointer)
                            timestamp, events = next(ans_iter)
                except Exception:
                    pass
                finally:
                    self.console.print('操作结束')

                self.go['command'] = pre_command
                self.go['text'] = pre_text

                self.info_label['text'] = pre_info
                self.delay_lbl['text'] = pre_delay_lbl

                self.delay_input['textvariable'] = pre_delay_var

                self.delay_input.unbind('<<Increment>>')
                self.delay_input.unbind('<<Decrement>>')
            else:
                self.info_label['text'] = '准备就绪\nTip: 请在第一个音符快落到判定线时，再按下上面的按钮\n可以使用空格键触发'
                self.go['text'] = '开始操作'

                self.running = True

                def player_worker(ans_iter: Iterator[tuple[int, list[TouchEvent]]]) -> None:
                    """打歌线程"""
                    if self.controller:
                        timestamp, events = next(ans_iter)
                        self.start_time = time.perf_counter() - timestamp / 1000 - 0.01  # 0.01 for the delay time

                        try:
                            while self.running:
                                now = round((time.perf_counter() - self.start_time) * 1000)
                                if now >= timestamp:
                                    for event in events:
                                        self.controller.touch(*event.pos, event.action, pointer_id=event.pointer)
                                    timestamp, events = next(ans_iter)
                        except StopIteration:
                            pass
                        finally:
                            self.console.print('操作结束')
                    else:
                        self.console.print('self.controller == None')

                    self.go['command'] = pre_command
                    self.go['text'] = pre_text
                    self.info_label['text'] = pre_info
                    self.delay_lbl['text'] = pre_delay_lbl
                    self.delay_input['textvariable'] = pre_delay_var
                    self.delay_input['state'] = 'disabled'

                    self.delay_input.unbind('<<Increment>>')
                    self.delay_input.unbind('<<Decrement>>')

                # 手动开始: 按下按钮的时刻对齐第一个音符(扫屏触点会在第一个音符之前就按下, 不能以第一个事件对齐)
                manual_ans = manual_start_plan(adapted_ans, first_note_ms(chart))
                self.player_worker_thread = Thread(target=player_worker, args=(iter(manual_ans),), daemon=True)

                def go_now():
                    def stop():
                        self.running = False

                    if self.player_worker_thread is None:
                        return

                    self.player_worker_thread.start()
                    self.info_label['text'] = '正在操作'
                    self.go['command'] = stop
                    self.go['text'] = '停止操作'

                    self.delay_lbl['text'] = '微调(正为延后，负为提前)：'
                    self.delay_input['state'] = 'normal'
                    self.delay_input['textvariable'] = delay_offset

                    def incremented(_):
                        self.start_time += 0.01

                    def decremented(_):
                        self.start_time -= 0.01

                    self.delay_input.bind('<<Increment>>', incremented)
                    self.delay_input.bind('<<Decrement>>', decremented)

                self.go['command'] = go_now
                self.update()
        except Exception as e:
            self.console.print_exception()
            messagebox.showerror('phisap', f'{e.__class__.__name__}: {e}')


def _report_crash(exc_type, exc_value, exc_tb) -> None:
    '''未捕获异常处理：写入 phisap_error.log 并弹窗提示
    （使用 pythonw 启动时没有控制台，否则程序会无声无息地退出）'''
    import traceback

    text = ''.join(traceback.format_exception(exc_type, exc_value, exc_tb))
    try:
        sys.__stderr__ and sys.__stderr__.write(text)
    except Exception:
        pass
    log_path = os.path.abspath('phisap_error.log')
    try:
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(text)
    except OSError:
        log_path = '(日志写入失败)'
    try:
        messagebox.showerror('phisap 出错了', f'{exc_type.__name__}: {exc_value}\n\n完整错误信息已保存到:\n{log_path}')
    except Exception:
        pass


if __name__ == '__main__':
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    sys.excepthook = _report_crash
    try:
        tk = Tk()
        tk.title('phisap')
        # Tk 回调（按钮等）中的异常默认只打印到 stderr，pythonw 下不可见
        tk.report_callback_exception = _report_crash
        App(tk).load_songs().load_cache('./cache').detect_adb_devices().mainloop()
    except SystemExit:
        raise
    except BaseException:
        _report_crash(*sys.exc_info())
        sys.exit(1)
