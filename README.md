# phisap

`phisap` 是 Phigros 谱面解析、触控规划与播放工具。桌面端用 Python/PyQt；Android 提供一个**单 APK、需要 root/SU** 的口袋版。项目不要求安装 LSPosed、Xposed、Zygisk 或额外模块。

## 主要部分

- `main.py`：桌面端界面和工作流；`start.cmd` 是 Windows 启动器。
- `algo/`：谱面触控规划算法与 iOS 事件转换。`algo1`、`algo2`、`algo3`、`algo3f`、`algored` 可在界面中选择；规划效果取决于谱面、设备、触点数与同步偏移。`algored` 仍有用户报告的漏音情况，尚未完成针对性复测，不能视为已解决。
- `extract.py`、`catalog.py`、`apk_tools.py`：谱面资源解析、下载及 Android/ADB 辅助工具。
- `device_plan.py`：桌面端输出与 Android 外部播放共用的 JSON 事件计划。
- `tools/native/`、`tools/app_dex.py`、`tools/build_apk.py`：ARM64 原生组件、手写 DEX 和单 APK 构建实现。
- `android/app/`：Android 原型源码与资源；当前发布 APK 的构建入口是 `tools/build_apk.py`，并非 Gradle 的 `assemble` 任务。
- `tests/`、`QA.md`、`docs/`：自动化回归测试、技术说明与验证记录。

## Android 播放模式

1. **进程内自动打歌**：APK 获得 root 后，将本项目的原生 hook 注入正在运行的 Phigros 进程；hook 观察游戏判定线和音符状态，并通过 root 触控组件发送事件。实现参考了 PhisSkin 自动打歌的功能思路，不依赖外部注入框架。
2. **进程外计划播放**：桌面端先解析谱面并生成 JSON；通过 ADB 将文件传到设备，再由 root 安装到 APK 私有目录。APK 的 `app_process` 播放器按设备端计划时间驱动 uinput 触控，避免在播放过程中逐事件经 USB/ADB 往返。

两条路径都需要 root。进程外计划使用 1280×720 的逻辑坐标，并在设备端适配屏幕；“按计划时间发送”不代表真实设备上绝对零延迟。桌面端也保留 ADB/MaaTouch 等播放路径。具体实现和设备验证步骤见 [`docs/android_inprocess_loader.md`](docs/android_inprocess_loader.md) 与 [`QA.md`](QA.md)。

## 运行桌面端

需要 64 位 Python 3.11 或更新版本。安装依赖后运行：

```sh
python -m pip install -r requirements.txt
python main.py
```

Windows 用户也可以运行 `start.cmd`，它会检查 Python、安装依赖并准备 scrcpy-server。ADB/platform-tools 对部分设备操作很有用；详见 `QA.md`。

## 构建 Android APK

```sh
python -m pip install -r requirements.txt
python tools/build_apk.py
```

构建器会生成/覆盖 `android/phisap-pocket.apk`，并执行 DEX、清单、原生载荷和签名检查。重建 ARM64 原生载荷还需要兼容的 Zig C 编译器（见 `tools/native_build.py`）。签名密钥位于 `tools/pocket-signing.pem`，并且目前被 Git 跟踪；应将其视为可能已暴露的发布密钥风险。直接轮换会导致旧安装包无法按常规方式升级，需先确定迁移策略。

## 测试

```sh
python -m unittest discover -s tests -v
```

静态测试、语法检查和 APK 打包检查不能替代 Android 真机验证。本工作区尚未进行设备上的安装、root 授权、触控或游戏内 hook 实测；相关功能不应仅凭构建通过就宣称在设备上可用。
