# iOS 方案: 用 pymobiledevice3 / USB HID 驱动 Phigros —— 可行性分析(延迟 / 准确度)

2026-10-02, 第二版(加入你的设备 iPad Pro M4 / iPadOS 18.7.7, 以及 USB HID 的新思路, 见第 10 节)。
**沙箱里没有 iPhone / iPad**, 所以每条结论都标了证据等级, 没测过的不会当成事实:

| 标记 | 含义 |
|---|---|
| ✅ | 这次在沙箱里读源码 / 跑判定模拟器 / 核对安装包得到, 命令见附录 A, 可复现 |
| 📚 | 别人的实测或文档(给了出处), 我没有复现 |
| ❓ | 没有任何公开数据, 只能上真机测。`tools/ios_probe.py` 就是为这些写的 |

---

## 1. 结论

**有条件可行: 值得做, 但要先花一天上真机测几个数字, 再决定写不写完整后端。主方案仍然是 pymobiledevice3 + WDA。
你想到的 USB HID 在 iPadOS 上给应用的触摸最多只有一个(第 10 节), 一个触点连 EZ 都只有约 46.1% 的 Perfect, 走不通。**

1. **pymobiledevice3 本身不能注入触控** ✅。读了 11.20.2 的源码, 没有任何 touch / tap / HID 服务(只有开关 AssistiveTouch 图标)。
   SideTap(Windows 上驱动 iPhone 的开源项目)的作者独立得出同样的结论: "pymobiledevice3 has no touch or HID service" 📚。
   它能做的是"把 WebDriverAgent(WDA)拉起来并连通": `developer wda --xctrunner`、`XCUITestService`、`usbmux forward`。
   真正的触控由 WDA 的 W3C `/actions` 发出。所以"用 pymobiledevice3"的思路成立, 但要补的是一个"WDA 触控客户端", 不是它的某个现成功能。
2. **延迟: 不能照搬 Android 的"电脑边播边发"** ✅📚。WDA 每个 `/actions` 都是一次同步手势, 固定开销几十到几百毫秒, 而且一次只处理一个请求。
   只能**把整首歌预排好, 一次交给设备按时间偏移回放**(WDA 里每个手指是一条独立的绝对时间线 ✅)。
   这样"绝对延迟"可以校准掉, 真正决定成败的是**回放内部的时间误差**(❓); 起点同步见第 5 节, 把"点开始键"放进同一条记录之后起点延迟的波动也不再进入误差。
3. **准确度(判定模拟器, ✅)**:
   - 每个事件的时间误差在 −60~+60ms 内, 全是 Perfect 且没有 Bad/Miss(iPad 的 11 触点, 60fps); 偏晚超过约 +70ms 或偏早超过约 −90ms 时 tap 变成 Good, 连击基本保住。
   - 把整体偏移校准好之后, 回放抖动 σ ≤ 15ms 几乎无损, σ=20ms 掉到 99.6%, σ=30ms 掉到 97.3%(iPad 11 触点, 60fps)。
   - 同时触点上限: iPad 约 11 个(📚, 对注入的触点是否同样生效 ❓)。algo3f 默认布局在 11 个触点上, 9 张谱面(共 11489 个音符)全是 100.0%, Bad+Miss 为 0 个;
     5 指 99.2%、3 指 96.6%、2 指 79.8%、**1 指 57.5%**。
   - 最短接触时间: 即使要求每次 tap 按住 125ms, 5 指时也只多掉约 1.5 个百分点。
   - 整首歌的请求体 0.5~1MB(WDA 默认上限 1GB ✅)。
   **算法这一侧不是障碍, 障碍在设备侧: 回放抖动、触点上限对注入是否生效、能不能一次吃下 1000 个输入源。**
4. **你的设备(iPad Pro M4, iPadOS 18.7.7)**:
   - 触点上限约 11(📚), 比 iPhone 的 5 宽松得多; 11 个触点连现有算法在 10 触点上的那点损失(Chart_AT 的 17 个 tap)都没有了。
   - ProMotion 120Hz: 判定模拟器在 120fps 下结论不变, 窗口整体晚约 4~8ms, 抖动容忍度略好(见 4.1)。游戏实际跑多少帧 ❓。
   - iPadOS 18.7.x 在 pymobiledevice3 的免管理员 USB 用户态隧道范围内(iOS ≥ 17.4)✅; 整条链路是否通 ❓。
   - WDA 在 iPad 原生全屏下坐标误差不超过 0.5 个点(Appium 的回归测试 📚); iPhone 兼容模式下以前会偏(已修, WDA 16.x 里有), Stage Manager 窗口 ❓, 都要避开(见第 9 节)。
5. **USB HID**(第 10 节): iPadOS 把 USB / 蓝牙的鼠标、触控板、摇杆合并成**一个** AssistiveTouch 指针, 点击被翻译成触摸; 多点触控数字化仪不被支持 📚。
   所以 HID 最多给游戏一个触点。判定模拟器里 1 个触点: EZ 46.1% (10822)、HD 50.7% (17153)、IN 58.1% (21759)(格内是 Perfect 占比 (Bad+Miss 个数)), EZ / HD 要 3 个触点才到 99.8%~99.9%(IN 99.4%)。**不建议作为主方案。**
6. **起点同步**(第 5 节): Android 的"计时器同步"是点一下屏幕再等固定的开始延迟; iOS 可以把"点开始键"和整首歌放进**同一条** WDA 记录
   (`build_actions(..., start_tap=..., start_delay_ms=...)`), 两者的间隔由记录内部的偏移保证, 不受"请求 → 第一个事件"延迟波动的影响。
7. **这次做了什么**(都不依赖 iPhone / iPad, 没有改 `main.py`, 没有接进界面):
   - `algo/ios_actions.py`: 规划结果 → WDA 的 W3C actions 转换器(含轨迹精简、坐标适配、开始键点击、参考解释器)。
   - `tools/ios_sim.py`: 离线分析(本文第 4、10 节的表都由它生成, 新增 `demand` 和 `--fps`)。
   - `tools/ios_probe.py` + `tools/ios_probe.html`: 真机探针(用假设备验证了"从页面事件算结论"这部分)。
   - `docs/ios_sim_results.md`: 离线分析的原始表格。
8. **下一步**: 按第 8 节在 iPad 上跑 Phase 0 探针(约 3 分钟出结果), 对照第 7 节的门槛表决定走哪条路。USB HID 想验证的话有一个 5 分钟的小实验(10.5)。

---

## 2. pymobiledevice3 能做什么、不能做什么(✅ 读 11.20.2 源码)

| | 结论 |
|---|---|
| 触控 / HID 注入 | **没有**。`services/` 里没有任何相关服务; XCTest 的 `synthesizeEvent` 也没有暴露给 IDE 一侧 |
| 启动 WDA | **有**: `pymobiledevice3 developer wda <命令> --xctrunner <runner 的 bundle id>`, 底层是 `services/dvt/testmanaged/xcuitest.py` 的 `XCUITestService`(经 testmanagerd 跑 XCUITest), 不需要 Mac / Xcode |
| WDA 客户端 | `services/wda.py` 很精简: 开会话、按元素点击、`dragfromtoforduration`(单指拖动)、截图、按键。**没有 W3C `/actions`, 没有多指**, 所以触控要自己 POST |
| 端口转发 | `pymobiledevice3 usbmux forward 8100 8100`(本机 8100 → 设备 8100, WDA 的 HTTP 端口) |
| 常驻 | **CLI 是一次性的**: 每个 `developer wda …` 命令结束时都会取消 runner(`_cleanup_xctrunner`), WDA 随之退出。要常驻, 得在 Python 里自己保持 `XCUITestService.run()` 任务(见第 9 节的示例) |
| iOS 17+ 的隧道 | 开发者服务需要隧道。11.20.2 在 Windows/Linux 上**默认走免管理员的用户态隧道**(纯 Python 网络栈 `pmd-pytcp`); iOS 17.0~17.3 不支持用户态隧道, 需要管理员运行 `remote tunneld` |
| Python 版本 | iOS 18.2+ 去掉了 QUIC, RemotePairing 隧道改走 TCP, 需要 Python ≥ 3.13(更低版本由依赖 `sslpsk-pmd3` 提供 TLS-PSK)。USB 用户态隧道不走这条路, 但"Windows + Python 3.11 + 最新 iOS"整条链路是否通 ❓ 要真机确认 |
| 安装 | 纯 Python wheel; 需要编译的依赖(`sslpsk-pmd3`、`backports.zstd`)都有 cp311 / cp312 的 win_amd64 预编译包 ✅(用 `pip download --platform win_amd64` 核对过, 没有在真 Windows 上装过) |

另一条等价的启动路线是 go-ios(`ios runwda` + `ios forward`), SideTap 就是这么做的 📚; 它是 Go 程序, 需要多装一个二进制。pymobiledevice3 的好处是纯 Python、可以在同一个进程里管理 WDA 的生命周期。

---

## 3. 延迟分析

### 3.1 Android 现状(对照)

- 电脑按计划逐批 `sendall` 给 scrcpy-server / MaaTouch(`player.py`), 链路几毫秒, 日志里统计"晚于计划多少毫秒", 超过 `LATE_WARN_MS=20ms` 才报。
- 即便如此, 用户日志里仍出现过"最大延迟 75~82ms"的尖峰(commit 70e83f4 的说明), 而 Perfect 窗口是 ±80ms: **Android 本身也不是零抖动**, 所以第 4.1 节的容忍度对 Android 同样有参考意义。
- 起点同步: 视觉自动开始只有 5fps(`VIDEO_MAX_FPS=5`, 每帧 200ms)的帧体积突变检测, 再加手动微调; 手动模式靠人。**iOS 的起点同步不需要比 Android 现在更准, 但需要"可重复"。**

### 3.2 WDA 的延迟结构

| 环节 | 数据 |
|---|---|
| 电脑 → usbmux 转发 → WDA | 3~5ms(SideTap 记录的 `/status` 往返 3.8ms 是上界)📚 |
| WDA 路由 | 路由跑在主队列(`FBWebServer.m: setRouteQueue:dispatch_get_main_queue()`)✅; SideTap 实测"一次只处理一个请求"📚 |
| 解析前台应用 | `/actions` 先取 `session.activeApplication`, SideTap 测得 72~102ms📚 |
| 事件合成 | `synthesizeEvent:` 同步等整条记录放完(`FBRunLoopSpinner`, 无超时, 100ms 一次检查完成)✅ |
| 放完之后 | `animationCoolOffTimeout` 默认 2s、`waitForIdleTimeout` 默认 10s ✅ → Unity 游戏永远不"空闲", **必须把两项设成 0**(`POST /session/<id>/appium/settings`) |
| 别人测的单次手势 | W3C 点一下: iOS 15.4 上 <300ms, iOS 16.3 上 >500ms(Appium 论坛)📚; SideTap: 点一下的预算里"按住 80ms 约占 20%"📚 |
| 尾部风险 | 屏幕唤醒后的第一次手势会挂起约 16~20s(SideTap)📚 → 自动锁定要设成"永不" |

app 里 iOS 说明对话框写的"WDA 单指约 50ms 延迟"我没找到出处, 和上面的数据对不上; 但**以上所有数字测的都是"一次手势从发出到完成的总耗时", 没有一个测的是"一次回放内部每个事件的时间准不准"**, 而后者才是这个方案要的 ❓。

### 3.3 所以方案形态只能是"批量回放"

- **逐事件下发不可行** ✅📚: 一首歌 5~7 万个事件(表见 `docs/ios_sim_results.md`), 峰值每秒数百个; WDA 每次手势至少上百毫秒、串行, 根本跟不上。
- **批量回放**: 一次请求里每个输入源(手指)是一条独立时间线, 偏移 = 此前所有条目的 `duration` 之和, 不做跨源对齐 ✅(`FBW3CActionsSynthesizer.m`)。
  `algo/ios_actions.py` 把"每次按下~抬起"拆成一个输入源: `[pause(按下时刻), pointerMove(0), pointerDown, …, pointerUp]`。
- 这时"延迟"拆成三项:
  1. **起点延迟的均值**(请求发出 → 第一个事件): 可以校准掉(整体提前就行)。
  2. **起点延迟的波动 σ_L**: 决定每次开打的同步误差。❓
  3. **回放内部误差 σ_j**(每个事件相对计划的随机偏差)。❓
- 代价: 失去"在线微调"和"随时停止"。WDA 没有取消一次在播记录的接口 ✅(`spinUntilCompletion` 没有取消); `DELETE /session/<id>` 会让 HTTP 层立刻回应该会话挂起的请求(`abandonPendingRequestsForSessionID`)✅,
  但设备端会不会停止放剩下的事件 ❓。保险的做法是**分段**(每段 ≤10~15 秒, 段与段之间所有手指抬起), 代价是段边界附近有一小段覆盖缺口 ❓。

---

## 4. 准确度分析(判定模拟器, ✅)

条件: `tools/judge_sim.py` 的 Phigros 规则, 60fps, 9 张谱面(4 张 AT + 5 张 IN, 共 11489 个音符)。
**这个模拟器照搬的是 Phira, 不是 Phigros 本体, 没有在真机上校准过**(见 QA.md), 下面的数字是"相对趋势", 不是承诺。原始表格见 `docs/ios_sim_results.md`。

### 4.1 时间

起点偏差(整首歌一个常数)和回放抖动(每个事件各自的随机误差)分开看。algo3f, 10 触点:

| 每个事件的误差 | Perfect 占比 (Bad+Miss) |
|---|---|
| −100ms | 31.5% (21) |
| −80ms | 99.6% (18) |
| −60 ~ +20ms | 99.7% ~ 99.9% (17~18) |
| +40ms | 99.8% (18) |
| +60ms | 99.8% (18) |
| +80ms | 31.6% (23) |
| +100ms | 31.4% (33) |

- 判定是按帧采样的, 事件要等到下一帧才被看到, 所以窗口整体偏早约 8ms: **计划整体提前约 10ms 最稳**。
- 超出窗口之后 tap 变成 Good, Perfect 占比掉到 31%, 但 Bad+Miss 仍只有 20~30 个(约 0.2%), 连击基本保住。
- 抖动叠加在偏移上: 偏移 +40ms 时, ±30ms 的均匀抖动就掉到 97.6%, ±40ms 掉到 85.8%(最晚的事件已经超过窗口); 偏移 0ms 时 ±40ms 的抖动仍是 99.9%。

校准好整体偏移(−10ms)之后, 每个事件独立延迟 N(0, σ):

| σ | 10 触点(algo3f) | 5 触点(扫屏 1 + 滑键 0) |
|---|---|---|
| 0~10ms | 99.8% (17) | 98.5% (171~174) |
| 15ms | 99.7% (21) | 98.0% (196) |
| 20ms | 99.4% (34) | 97.4% (228) |
| 30ms | 97.1% (144) | 95.4% (345) |
| 40ms | 92.9% (309) | 91.1% (569) |

**对设备端的要求**: 回放内部 σ_j ≲ 15ms; 起点延迟的 σ_L 因为是整首歌的共同偏移, 窗口半宽约 70ms, σ_L ≲ 25ms 时绝大多数次开打是全 Perfect(σ_L=30ms 时约 2% 的次数落在窗口外, 40ms 时约 8%)。

### 4.1b iPad: 11 触点、60 / 120fps

algo3f 默认布局, 11 个触点(iPad 的上限), 9 张谱面。格内: Perfect 占比 (Bad+Miss 个数)。

| 每个事件的误差 | 60fps | 120fps | 60fps, ±20ms 抖动 | 120fps, ±20ms 抖动 |
|---|---|---|---|---|
| −80ms | 99.7% (0) | 98.9% (0) | 99.3% (0) | 98.7% (0) |
| −60 ~ +40ms | 100.0% (0) | 100.0% (0~1) | 100.0% (0) | 100.0% (0~1) |
| +60ms | 100.0% (0) | 100.0% (1) | 76.5% (2) | 90.8% (1) |
| +80ms | 31.6% (5) | 32.4% (5) | 32.6% (8) | 34.0% (8) |

| 回放抖动 σ(偏移已校准为 −10ms) | 60fps | 120fps |
|---|---|---|
| 0~10ms | 100.0% (0) / 100.0% (0) / 100.0% (1) | 100.0% (0) / 100.0% (0) / 100.0% (0) |
| 15ms | 100.0% (3) | 100.0% (2) |
| 20ms | 99.6% (15) | 99.8% (7) |
| 30ms | 97.3% (129) | 97.6% (104) |
| 40ms | 93.4% (281) | 94.2% (238) |

- 全 Perfect 且 Bad+Miss 为 0 的误差窗口约 −60~+60ms(10 触点时窗口内也有 17~18 个 Bad+Miss, 那是 Chart_AT 从 72 秒起丢的 tap, 11 触点补上了)。
- 120fps 时每帧只有 8.3ms, 事件被"看到"的延迟更小, **窗口整体向晚的一侧移动约 4ms**: +60ms 且有 ±20ms 抖动时从 76.5% (2) 回到 90.8% (1), 而 −80ms 一侧略差。
  目标偏移 60fps 取约 −10ms、120fps 取约 −5ms。Phigros 在 iPad Pro 上实际跑 60 还是 120 帧 ❓(探针测不出来, 需要看游戏设置或抓帧)。
- iPad Pro 的 ProMotion 触摸采样率 120Hz(Apple Pencil 240Hz)📚 只影响真手指, 对 WDA 注入的事件没有意义。

### 4.2 触点上限

iPhone 对真手指是 5 个、iPad 约 11 个(Apple 社区 / StackOverflow 的说法 📚); WDA 注入的多指是"多条 `XCPointerEventPath` 时间上重叠"(Appium 文档 📚), 文档没提上限, SideTap 作者也说多指"unproven on WDA"📚 ❓。

对每个上限搜索最合适的布局(扫屏触点数 k、滑键触点数 n, 其余处理 tap/hold; 11 个及以上直接用 algo3f 的默认布局), 9 张谱面(4 张 AT + 5 张 IN)合计:

| 触点上限 | 1 | 2 | 3 | 4 | 5 | 6 | 8 | 10 | 11(iPad) | 16 |
|---|---|---|---|---|---|---|---|---|---|---|
| Perfect 占比 (Bad+Miss) | 57.5% (4580) | 79.8% (2019) | 96.6% (352) | 98.6% (154) | 99.2% (95) | 99.5% (51) | 100.0% (5) | 100.0% (5) | 100.0% (0) | 100.0% (0) |

(8 / 10 指那 5 个是 Rrharil AT 的红键雨, 搜索时滑键触点限制在 ≤3 个; 用 algo3f 的默认布局时 10 指是 99.8%(17 个, 都是 Chart_AT 从 72 秒起丢的 tap), 11 指就没有了。)

最难的两张: Entrance AT 5 指 94.8%、6 指 96.5%、8 指 100%; Rrharil AT 5 指 97.6%、6 指 98.6%。其余 7 张在 5 指上就是 100%。
EZ / HD / IN 全部 49 首歌(`--quick`)在 2 / 3 / 5 指上的合计:

| 难度(音符数) | 1 | 2 | 3 | 5 |
|---|---|---|---|---|
| EZ(20096) | 46.1% (10822) | 92.6% (1487) | 99.9% (17) | 100.0% (0) |
| HD(34850) | 50.7% (17153) | 89.3% (3708) | 99.8% (65) | 100.0% (1) |
| IN(53243) | 58.1% (21759) | 84.7% (7612) | 99.4% (309) | 100.0% (24) |

**结论: 只要注入的触点 ≥3 个就能保住 96% 以上(EZ/HD/IN 在 3 指上 ≥99.4%), ≥5 个基本无损, 11 个(iPad)完全无损; 悬崖在 2 个以下。**
但 5 触点以下需要新增一个"按上限自适应选布局"的算法(现有 algo3f 在 10/16 触点上调好, 5 触点以下的默认布局很差), `tools/ios_sim.py` 里的搜索逻辑可以直接转正。

### 4.3 最短接触时间

SideTap 的配置里提到"过短的接触可能被 iOS 丢弃, 漏点一次比慢一点更糟"📚(具体阈值没有数据 ❓); Appium 文档里点一下的例子是按住 0.125s📚。
Android 现在 tap 只按 5ms。如果 iOS 要求每次 tap 按住 T 毫秒, 触点占用变长, 密集处更不够用。5 张谱面, 最优布局:

| T | 5 指 | 10 指 |
|---|---|---|
| 5ms(现在) | 98.2% (95) | 99.9% (5) |
| 40ms | 97.8% (116) | 99.9% (5) |
| 80ms | 97.9% (101) | 99.9% (5) |
| 125ms | 96.7% (149) | 99.9% (5) |

影响很小(5 指时 125ms 也只掉约 1.5 个百分点, 10 指几乎没有)。`build_actions(min_contact_ms=...)` 已经支持。

### 4.4 请求体规模

| | 范围(9 张谱面, algo3f 10 触点) |
|---|---|
| 输入源(= 按下次数) | 385 ~ 1120 |
| actions 条目 | 6656 ~ 14226(精简前的点 3.4~6.7 万) |
| JSON | 0.47 ~ 0.98MB |
| 转换耗时(纯 Python) | 1.1 ~ 3.4s |

WDA 自带的 HTTP 服务器请求体上限默认 1GB(环境变量 `MAX_HTTP_REQUEST_BODY_SIZE` 可改)✅, 所以 HTTP 层不是问题。
一个记录里放 ~1000 个输入源、XCTest / testmanagerd 能不能吃下、会不会拖慢起点 ❓ —— 探针的 `start`(大请求体)实验测这个。

转换器按 WDA 的语义做了两处容易做错的事(都有测试):
- **采样保持**: 规划里的 MOVE 是"手指停在原地, 到点才跳过去"(判定线瞬移时长条就是这样停一阵再跳)。事件间隔 > 8ms 的当"停住, 最后 1ms 跳过去"; 若当成线性滑动, 手指会在 300ms 里缓慢漂向新位置, 旧位置的判定区早就丢了。
- **每次按下新开一个输入源**: WDA 里每个 `XCPointerEventPath` 只能按下抬起一次(Appium 文档 📚); 同一个源里"抬起后再 `pointerMove` 再按下"的写法在 WDA 源码里有一处可疑的路径(`pointerMove` 会作用在已抬起的旧路径上), 干脆绕开。探针的 `repeat` 实验测这个。

### 4.5 坐标、安全区、系统手势

- WDA 的坐标是**点(pt), 不是像素**, 随方向变化(横屏宽 > 高)。`fit_mapping` 按 `main.py` 的 Android 适配做等比例缩放居中。
  Phigros 在刘海屏 iPhone 上的实际画面区域、暂停键位置 ❓: 刚做的"暂停键保护"用的是 1280×720 虚拟坐标里的 160×160 方框(`PAUSE_BUTTON_BOX`), iPhone 上要重新量。
- WDA 的触点到不了 Home 指示条区域(SideTap 实测)📚; 屏幕边缘的系统手势区同理 ❓。音符落在这些位置时取不到。
- iOS 17+ 的 WDA 需要 Developer Mode、挂载开发者镜像(DDI)、用有效签名安装(免费 Apple ID 每 7 天过期 📚)。

---

## 5. 起点同步(最大的不确定性)

批量回放意味着开打的那一刻就决定了整首歌的偏移, 之后改不了(Android 现在可以边打边用"实时延迟"微调)。

先看 Android 现在是怎么做的(读 `main.py`, ✅): **计时器同步** `sync_ms` = 点一下屏幕中心(触发 Phigros 开始), 然后 `开始延迟`(用户校准的固定毫秒数)之后把规划的时刻 0 对上; 视觉自动开始是 5fps 的帧体积突变检测, 精度 ±100ms 量级, 再加手动微调。

| 方式 | 做法 | 预期误差 | 证据 |
|---|---|---|---|
| **开始键 + 整首歌放进同一条记录(推荐)** | 记录的零点先点一下开始键, 整首歌从偏移"开始延迟"起放: `build_actions(ans, mapping, start_tap=(640, 360), start_delay_ms=D)`。点开始键和第一个音符之间的间隔完全由记录内部的偏移决定 | 回放内部误差 σ_j + **游戏自己"点开始 → 音乐开始"的时长是否稳定** | ✅ 转换器; 游戏侧 ❓ |
| 手动 | 用户看着第一个音符点"开始"(和 Android 手动模式一样) | 人手 ±50~100ms, 无法在线微调 | 类比 Android |
| 校准常数 | 用探针测出"请求 → 第一个触点"的均值, 之后每次提前这么多 | 取决于 σ_L | ❓ |
| 视觉 | WDA 的 MJPEG 流(端口 9100, SideTap 的 viewer 约 34fps 📚)做"帧体积突变"检测, 复用 `control.py` 的 `_activity_feed` 思路; 检测到起点后再下发"从 t=已过时间 开始"的记录 | 帧间隔 ~30ms + 管线延迟(未知) | ❓ |

"同一条记录"的好处: 请求发出到第一个事件之间的延迟 σ_L(包括 WDA 解析、查前台应用等, 几十到几百毫秒且会波动)不再进入同步误差, 因为开始键和音符是同一条时间线上的两个点。
剩下的不确定性和 Android 的计时器同步一样, 都来自游戏本身(点击后加载 / 转场的时长每次是否一样), 所以你在 Android 上的体验可以直接参考: 同一首歌每次用同一个"开始延迟"都能对上, iOS 上也能。
注意这个办法要求开始键的位置固定(Android 点的是屏幕中心), 且点击会让游戏进入"开始"流程; 设备上的 Phigros 流程需要用户确认 ❓。

---

## 6. 风险与开放问题

| # | 问题 | 现状 | 探针实验 |
|---|---|---|---|
| R1 | 注入的同时触点上限 | ❓ | `touches` |
| R2 | 最短接触时间 | ❓(模拟显示影响很小) | `contact` |
| R3 | 回放内部时间误差 σ_j | ❓ | `timing` |
| R4 | 起点延迟 σ_L(小请求 / 大请求) | ❓ | `start` |
| R5 | `pointerMove(duration)` 内系统是否插值 | 很可能(WDA 的滑动靠它), ❓未验证 | `move` |
| R6 | 同一毫秒"一个抬起、一个按下"的接力 | ❓ | `handoff` |
| R7 | 同一个输入源按两次 | ❓(转换器已绕开) | `repeat` |
| R8 | 坐标精度、安全区 | ❓ | `coords` |
| R9 | 如何中止在播的记录 | ❓(见 3.3) | `longhold`(需要手动配合, 见第 8 节) |
| R10 | Windows + Python 3.11 + 最新 iOS 整条启动链路 | ❓ | 第 9 节 |
| R11 | 判定模拟器和真实 Phigros 的差距 | 已知存在, 见 QA.md | 真机跑一首低难度谱面对照 |
| R12 | Phigros 是否原生全屏、画面区域怎么摆(iPad 是 4:3 屏, 16:9 的玩法区可能上下留黑边)、暂停键锚定在屏幕角还是玩法区角 | ❓ | 看 WDA `window/size` + 截图 |
| R13 | iPad 的 Stage Manager / 分屏 / iPhone 兼容模式会让 WDA 坐标偏移 | WDA 在原生全屏下零误差 📚, 其余 ❓ | `coords`(在 Safari 里测, 游戏里要另外确认) |

---

## 7. 决策门槛(Phase 0 的判据)

| 门 | 探针指标 | 通过 | 不通过时 |
|---|---|---|---|
| G1 触点 | `touches`: 页面看到的同时触点峰值 | iPad 预期 11(≥5 基本无损, ≥3 也能做, 见 4.2) | ≤2: 连 EZ/HD 都只有约 90%, 不建议投入 |
| G2 接触 | `contact`: 最短可靠接触 | ≤125ms 时送达率 ≥99% | >125ms: 改用更长的 `min_contact_ms`, 重新用 `ios_sim floor` 评估 |
| G3 回放 | `timing`: 事件间隔误差标准差 / p99 | σ_j ≤15ms 且 p99 ≤40ms | 见备选路线 A |
| G4 起点 | `start`: 延迟标准差(小请求、大请求) | σ_L ≤25ms, 且大小请求的均值相差 ≤20ms(**用"开始键放进同一条记录"时不是硬门槛**, 此时只看 G3) | 起点要靠视觉同步补, 或走备选路线 A |
| G5 大记录 | `start`(大请求体) 成功、`move` 里的插值存在 | 1000 个输入源的记录被接受 | 改成分段下发 |

G1、G3 全过(并且用同一条记录做起点同步, 或 G4 也过)→ 做 Phase 1; G3 不过 → 备选路线。

---

## 8. 路线图

**Phase 0 — 真机探针(约 1 天, 需要 iPhone / iPad)**
1. 按第 9 节装好并启动 WDA、转发 8100。
2. `python tools/ios_probe.py run --wda http://127.0.0.1:8100`, 在 iPhone 的 Safari 里打开它打印的地址, 保持前台。约 3 分钟出报告, 原始数据存 `probe_result.json`。
3. 测"如何中止": `python tools/ios_probe.py run --wda http://127.0.0.1:8100 --experiments longhold`, 它会让一个手指按住 10 秒并打印会话 id;
   按住期间在另一个终端执行 `curl -X DELETE http://127.0.0.1:8100/session/<id>`, 报告里会写出手指实际按了多久、请求提前返回了没有。
4. 对照第 7 节填表, 把结果发回来, 再决定后面。

**Phase 1 — 最小后端(G1/G3/G4 通过后)**
- `ios_backend.py`: 后台线程里保持 `XCUITestService.run()`、`usbmux forward`、WDA 会话与设置; 接口对齐 `DeviceController`/`MaaTouchController` 里 `main.py` 用到的那部分(`max_pointers`、`device_width/height`、`supports_visual_watch=False`)。
- `main.py` 增加"批量回放后端"分支: 播放时不走 `run_player` 的逐批发送, 而是 `build_actions(...)` + 一次(或分段)POST。
- 起点同步: `build_actions(..., start_tap=开始键位置, start_delay_ms=开始延迟)` 把点开始键和整首歌放进同一条记录(对应 Android 的计时器同步); 手动开始作为备选。
- 新增"按触点上限自适应选布局"的算法(`tools/ios_sim.py` 的 `best_layout` 转正)。

**Phase 2**: 校准向导、MJPEG 视觉同步、分段与停止、`PAUSE_BUTTON_BOX` 按安全区修正。

**备选路线(探针不过时)**
- A. 自建 XCUITest runner(Swift/ObjC, 需要一台 Mac 或 GitHub Actions 的 macOS runner 构建一次, 再用 Sideloadly 签名): 设备端接收电脑发来的事件并用单调时钟精确调度。仍然走 XCTest 合成, 但绕开 WDA 的 HTTP 与路由开销。
- B. 越狱 + ZXTouch / IOS13-SimulateTouch: 系统级多点注入, 延迟很低(项目里的说明写 5ms 内, 我没验证), 官方支持 iOS 11~14 📚。
- C. Android 备用机 / 电脑模拟器(现在的路线, 已经跑通)。

---

## 9. 环境准备(Windows)与启动示例

```
pip install -U pymobiledevice3
pymobiledevice3 usbmux list                         # 能看到手机(电脑要装 Apple Devices 或 iTunes 才有 USB 驱动 📚)
pymobiledevice3 amfi enable-developer-mode          # iOS 16+; 手机会重启, 重启后在设置里确认
pymobiledevice3 mounter auto-mount                  # 挂载开发者镜像
pymobiledevice3 usbmux forward 8100 8100            # 另开一个终端, 保持运行
```

- WDA: 用 Sideloadly 把 appium/WebDriverAgent 的 `WebDriverAgentRunner-Runner.ipa` 签名装到手机(免费 Apple ID 每 7 天过期, 之后要重签 📚)。最新版本 16.13.6(2026-09-30)。
- iPad / iPhone 设置: 自动锁定 = 永不; 保持亮屏、探针页在 Safari 前台。
- iPad 额外要做的(建议, ❓ 没有在真机上验证过): 关掉 Stage Manager 和分屏 / Slide Over(设置 → 多任务与手势), Phigros 要全屏、横屏、原生 iPad 模式(不是 iPhone 兼容模式), 否则 WDA 的坐标会偏; 关掉 AssistiveTouch(如果试过 USB 鼠标, 它的悬浮按钮会盖在画面上); 玩之前用 WDA 的 `GET /session/<id>/window/size` 看一下游戏前台时的尺寸。
- 电脑和 iPhone 在同一个局域网(探针页通过局域网回传事件; 触点本身走 USB)。

pymobiledevice3 的 CLI 命令是一次性的, 要让 WDA 常驻, 需要自己保持 runner 任务。下面是照着它自己的 CLI 代码(`cli/developer/wda.py`、`cli/cli_common.py`)写的示意, **没有在真机上跑过 ❓**, 库的接口是 async 的, 不同版本有差异:

```python
import asyncio
from pymobiledevice3.cli.developer.wda import wait_for_xctest_app
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote import userspace_tunnel

XCTRUNNER = 'com.你的签名后的id.WebDriverAgentRunner.xctrunner'    # pymobiledevice3 apps list 可以查

async def main():
    lockdown = await create_using_usbmux()
    if int(lockdown.product_version.split('.')[0]) < 17:
        provider = lockdown                                    # iOS <17: 直接用 lockdown
    else:                                                      # iOS 17.4+: 免管理员的用户态隧道(17.0~17.3 要用 tunneld)
        provider = await userspace_tunnel.establish_userspace_rsd(serial=lockdown.udid, remotepairing_fallback=False)
    task = await wait_for_xctest_app(provider, XCTRUNNER)     # 等到设备 8100 端口可连, 返回常驻的 runner 任务
    print('WDA 已启动, Ctrl+C 退出')
    await task                                                  # 保持 runner 活着

asyncio.run(main())
```

如果这一步卡住, 先用 go-ios(`ios runwda`)或 Mac 上的 Xcode 把 WDA 跑起来, 探针只需要 `http://127.0.0.1:8100` 能通, 与 WDA 是谁启动的无关。

---

## 10. USB HID 路线(你提出的新思路)

### 10.1 想法与硬件

电脑本身不能当 USB 设备(普通 PC 的 USB 口都是主机口), 所以需要一块能扮演 USB HID 设备的小板子(树莓派 Pico、ESP32-S3、Arduino Leonardo 之类, 几十块钱)插在 iPad 的 USB-C 口上;
电脑通过串口把"什么时刻做什么"交给它, 板子用本地定时器发 HID 报告。好处是 iPad 上什么都不用装(不开开发者模式、不用签名、不用隧道),
而且定时在硬件上做: USB 全速 HID 最快每 1ms 一份报告, 板子自己的定时抖动可以做到 ≲1ms(这是规格上的推理, 没有测过 ❓)。

### 10.2 iPadOS 怎么处理 HID 输入(决定性的一点)

| 外接的 HID 设备 | iPadOS 的行为 | 证据 |
|---|---|---|
| 鼠标 / 触控板 / 摇杆(USB 或蓝牙) | 变成**一个** AssistiveTouch 指针, 点击被翻译成触摸, 效果"和手指触摸完全一样"(单指) | 📚 Apple 支持文档(AssistiveTouch → 指针设备)、WWDC20 "Handle trackpad and mouse input"、Roblox 工程师("iOS treats any pointer devices as touch assistant devices first, so clicks are translated as touch events") |
| 触摸屏 / 多点触控数字化仪 | **不支持**: "iPadOS 不支持外接触摸屏显示器, 触摸不会传给 iPad"; 只有把它的固件切到"鼠标模式"才会被当成指针 | 📚 Apple 社区的多个帖子 |
| 蓝牙 BLE HID 鼠标(ESP32) | 可以, 同样走 AssistiveTouch; 普通 BLE 手柄不行(不是 MFi) | 📚 asterics/esp32_mouse_keyboard 在 iPad Pro 上验证过; ESP32-BLE-Controller 的说明 |
| 键盘 / 手柄 | 只对读键盘 / GameController 的 app 有效, 我没查到 Phigros 支持 | ❓ |

AssistiveTouch 菜单里有"2 / 3 / 4 / 5 指手势"(Apple 支持文档), 但那是在屏幕上弹出一组固定排布的虚拟手指、靠菜单一步步选的, 没法按毫秒编程。
多个鼠标会合并成同一个光标(📚 说法一致, 我没有实测 ❓)。**所以 iPadOS 上 HID 能给应用的触摸最多是一个。**

### 10.3 一个触点够不够打 Phigros(判定模拟器 ✅)

用现有规划器(只有 1 个触点, 逐个处理 tap / 长条, 没有扫屏触点, 没有时分复用)。EZ / HD / IN 各 49 首歌的合计, 格内: Perfect 占比 (Bad+Miss 个数):

| 难度(音符数) | 1 个触点(USB 鼠标) | 2 个 | 3 个 | 5 个 |
|---|---|---|---|---|
| EZ(20096) | **46.1% (10822)** | 92.6% (1487) | 99.9% (17) | 100.0% (0) |
| HD(34850) | **50.7% (17153)** | 89.3% (3708) | 99.8% (65) | 100.0% (1) |
| IN(53243) | **58.1% (21759)** | 84.7% (7612) | 99.4% (309) | 100.0% (24) |
| 9 张 AT/IN(11489) | **57.5% (4580)** | 79.8% (2019) | 96.6% (352) | 99.2% (95) |

为什么一个触点不够(`python tools/ios_sim.py demand <谱面>...`):
- 黄键 + 红键占很大比例: EZ 53%、HD 46%、IN 35%。它们要靠一个"一直在动 / 一直按着"的触点去碰, 和点蓝键的触点冲突。
- tap / 长条头到来时手指同时被占着(≥2 个)的占比: EZ 中位数 28%(范围 0~57%)、HD 34%(2~60%)、IN 39%(13~73%)。
  同一时刻的多押理论上可以在 ±80ms 的窗口里排队(时分复用), 但游戏每帧才读一次触点, 一帧里多半只认一次新按下 ❓, 60fps 时一帧 16.7ms。
- 在长条按住期间到达的音符: EZ 中位数 7%、HD 9%、IN 11%(单张谱面最多的是 HD 里的一张, 52%)。
  按住长条的触点被钉在原地, 要兼顾别处的音符只能"抬起-点一下-按回来"(长条允许断开 50ms), 位置还得足够准。
- EZ / HD 最多只需要 2 个 tap / 长条触点(同时被占着的峰值), 再加 1 个扫屏触点, EZ / HD 用 3 个触点就到 99.8%~99.9%(IN 是 99.4%, 要 5 个触点才到 100.0%)。

更聪明的单指针规划(时分复用、用一直按着的触点去碰黄键)能比上面的数字好, 但上限未知 ❓; 它不可能赶上 3 个触点的结果。

### 10.4 其余问题

- **Phigros(Unity)认不认 AssistiveTouch 指针的点击** ❓: 系统层面点击被翻译成触摸(WWDC20: app 没声明 `UIApplicationSupportsIndirectInputEvents` 时, 点击就是和手指一样的 `TouchType.direct`),
  但有 Unity 里"触控板轻点只算鼠标按下、没有抬起"的报告(StackOverflow, 2019)。游戏不读指针就什么都点不了。
- **位置精度** ❓: 相对鼠标没有位置反馈, 还有指针加速度, 得靠"先甩到角落归零, 再按标定好的曲线移动"; USB 绝对指针(数位板式)能不能被 iPadOS 当指针没有明确资料
  (触摸屏切到鼠标模式能被当指针 📚, 说明绝对坐标可能可行, 未验证)。
- **AssistiveTouch 要一直开着**: 它的悬浮按钮会盖在游戏画面上。
- **BLE 鼠标**(无线)的连接间隔至少十几毫秒, 比 USB 差, 不适合做定时。

### 10.5 对比与建议

| | WDA(XCTest 合成) | USB HID 鼠标(单片机) |
|---|---|---|
| 同时触点 | iPad 约 11(对注入是否生效 ❓) | **1** |
| 判定模拟器 | 11 指 100.0% (0 个 Bad+Miss) | 1 指 IN 58.1% (21759)、EZ 46.1% (10822) |
| 位置 | 绝对, 点级精度; iPad 原生全屏误差 ≤0.5 点 📚 | 相对 + 加速度; 绝对是否可行 ❓ |
| 定时 | 回放内部误差 ❓(要求 σ ≲ 15ms); 起点用"同一条记录"解决 | 板子 ≲1ms(推理 ❓) + iPadOS 处理延迟 ❓ |
| iPad 上要做的设置 | 开发者模式、WDA 签名(免费 7 天)、隧道 | 开 AssistiveTouch(指针设备) |
| 额外硬件 | 无 | 一块 HID 板子 |

- **不建议把 USB HID 作为主方案**: 触点数不够是结构性的, 不是调一调就能补上的。我们的算法要的定时精度只是 σ ≲ 15ms, 并不需要硬件级的 1ms(Android 本身的发送延迟在你的日志里也出现过 75~82ms 的尖峰)。
- 想验证上面的判断, 5 分钟的小实验: 用 USB-C 转接头接一个普通 USB 鼠标, 设置 → 辅助功能 → 触控 → AssistiveTouch → 开启, 在 Phigros 里用鼠标点音符、按长条。
  能点 = Phigros 认指针; 再试着同时接两个鼠标, 看是不是只有一个光标。结果发给我就行。
- 如果你想的不是"单片机扮演鼠标"而是别的 HID 方案(比如某个能给 iPad 多点触控的硬件), 告诉我具体是什么, 我再查。

---

## 附录 A: 复现

```
python tools/ios_sim.py payload <谱面.json>... --pointers 10
python tools/ios_sim.py caps    <谱面.json>... --jobs 2 --quick --caps 2,3,4,5,6,8,10,16
python tools/ios_sim.py timing  <谱面.json>... --jobs 2 --pointers 10
python tools/ios_sim.py jitter  <谱面.json>... --jobs 2 --pointers 10 --offset=-10
python tools/ios_sim.py floor   <谱面.json>... --jobs 2 --quick --caps 5,10
python tools/ios_sim.py demand  <谱面.json>... --jobs 2
python tools/ios_sim.py caps    <谱面.json>... --jobs 2 --caps 1              # 单指针(USB 鼠标), 不加 --quick
python tools/ios_sim.py jitter  <谱面.json>... --jobs 2 --pointers 11 --offset=-10 --fps 120   # iPad
python tools/ios_probe.py run --wda http://127.0.0.1:8100
```
谱面可以是官方格式或 RPE 格式; 8 张谱面来自 PhiResources(app 的下载页就是它), 只有 `Chart_AT.json` 在仓库里。

## 附录 B: 文件

| 文件 | 作用 |
|---|---|
| `algo/ios_actions.py` | 规划 → W3C actions: `split_lives`、`build_actions`、`fit_mapping`、`simplify`、`replay_actions`(按 WDA 语义还原事件的参考解释器) |
| `tools/ios_sim.py` | 离线分析: `timing` / `jitter` / `caps` / `floor` / `payload` / `demand`, 都支持 `--fps` |
| `tools/ios_probe.py`、`tools/ios_probe.html` | 真机探针: 9 个实验(`longhold` 要人配合)+ 报告 |
| `tests/test_ios_actions.py`、`tests/test_ios_probe.py`、`tests/test_ios_sim.py` | 62 个测试, 不需要 iPhone / iPad |
| `docs/ios_sim_results.md` | 离线分析的原始表格 |

## 附录 C: 来源

- pymobiledevice3 11.20.2 源码(PyPI)、文档 https://doronz88.github.io/pymobiledevice3/installation/
- appium/WebDriverAgent 16.13.6 源码: `FBW3CActionsSynthesizer.m`、`FBXCTestDaemonsProxy.m`、`FBRunLoopSpinner.m`、`FBHTTPServer.m`、`FBConfiguration.m`、`XCUIApplication+FBTouchAction.m`
- SideTap(WDA + go-ios, Windows)https://github.com/ucsandman/SideTap : `docs/ERRORS.md`("A latency pass")、`src/phone_harness/config.py`、`README.md`、`CLAUDE.md`
- Appium XCUITest 驱动文档 "About iOS Input Events" https://appium.github.io/appium-xcuitest-driver/4.16/actions/
- Appium 论坛 "Performing Tap using W3c Sequence … >500 milliseconds in iPhone with OS 16.3" https://discuss.appium.io/t/performing-tap-using-w3c-sequence-and-it-is-taking-500-milliseconds-in-iphone-with-os-16-3/38737
- iPhone 同时触点上限: https://stackoverflow.com/questions/26098394/ 、https://discussions.apple.com/thread/251029385 、https://discussions.apple.com/thread/254843211
- IOS13-SimulateTouch(越狱, iOS 11~14)https://github.com/xuan32546/IOS13-SimulateTouch
- AssistiveTouch(指针设备、多指手势)https://support.apple.com/en-us/111794 、https://support.apple.com/en-ph/guide/ipad/ipad9a2466d3/ipados
- WWDC20 "Handle trackpad and mouse input" https://developer.apple.com/videos/play/wwdc2020/10094/
- iPadOS 不支持外接触摸屏: https://discussions.apple.com/thread/252363006 、https://discussions.apple.com/thread/255122887
- Roblox 开发者论坛(指针被当成触摸)https://devforum.roblox.com/t/ipados-mouse-support-is-very-bad/4635011
- ESP32 BLE 鼠标在 iPad 上可用 https://github.com/asterics/esp32_mouse_keyboard ; ESP32-BLE-Controller 对 iOS 的说明 https://registry.platformio.org/libraries/black.ghost.off/ESP32-BLE-Controller
- Unity 里触控板轻点只算鼠标按下 https://stackoverflow.com/questions/54953207/
- iPad 11 个同时触点 https://toucharcade.com/community/threads/ipad-multitouch-limitations.51012/ ; iPad Pro 触摸采样率 https://discussions.apple.com/thread/256297961
- appium/WebDriverAgent PR #1249(iPad 兼容模式下手势坐标缩放; 原生全屏零误差)https://github.com/appium/WebDriverAgent/pull/1249
