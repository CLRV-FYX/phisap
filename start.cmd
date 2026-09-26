@echo off
chcp 65001 >nul
title phisap 启动器
cd /d %~dp0

echo.
echo [1/4] 检查 Python...
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python。
    echo        请先安装 Python 3.11 或更高版本（64 位），网址: https://www.python.org/downloads/
    echo        安装时务必勾选 "Add Python to PATH"。
    pause
    exit /b 1
)
echo [OK] 检测到 Python，正在安装/更新依赖...
findstr /i /c:"av==10" /c:"numpy==1.25" requirements.txt >nul 2>nul
if not errorlevel 1 (
    echo [错误] 当前目录的 requirements.txt 是旧版本（含 av==10.0.0 / numpy==1.25.0），
    echo        这两个版本没有 Python 3.12+ 的预编译包，必然安装失败。
    echo        请用仓库最新的 requirements.txt 替换，内容应为:
    echo            av^>=12.3.0
    echo            rich^>=13.3.4
    pause
    exit /b 1
)
python -m pip install --upgrade pip
rem 只安装预编译包，绝不在本地编译（本地编译需要 C 编译器，且旧版本与新 Python 不兼容）
python -m pip install --only-binary=:all: -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重新运行本脚本。
    echo        如果提示 "No matching distribution"，说明当前 Python 版本不受支持，
    echo        请安装 Python 3.11 或更高版本（64 位）后重试。
    pause
    exit /b 1
)

echo.
echo [2/4] 检查 scrcpy-server（已随仓库提供，缺失时自动下载）...
if exist scrcpy-server-v4.1 (
    for %%F in (scrcpy-server-v4.1) do if %%~zF==0 del scrcpy-server-v4.1
)
if not exist scrcpy-server-v4.1 (
    echo 正在从 GitHub 下载 scrcpy-server-v4.1 ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/Genymobile/scrcpy/releases/download/v4.1/scrcpy-server-v4.1' -OutFile 'scrcpy-server-v4.1'"
    if not exist scrcpy-server-v4.1 (
        echo [错误] 下载失败。请手动到 https://github.com/Genymobile/scrcpy/releases/tag/v4.1
        echo        下载 scrcpy-server-v4.1 ，并将其放到本目录（与 main.py 同级）。
        pause
        exit /b 1
    )
)
echo [OK] scrcpy-server-v4.1 已就绪

echo.
echo [3/4] 检查 adb...
where adb >nul 2>nul
if errorlevel 1 (
    echo [警告] 未检测到 adb。
    echo        adb 用于列出安卓设备和自动检测屏幕尺寸（可选但强烈建议安装 platform-tools）。
    echo        没有 adb 时屏幕尺寸请手动选择预设或输入 宽×高 。
)

echo.
echo [4/4] 启动 phisap...
echo      运行日志和报错信息会显示在本窗口中，请不要关闭本窗口（关闭会同时退出 phisap）。
echo.
python main.py
if errorlevel 1 (
    echo.
    echo [错误] phisap 异常退出，请查看上面的报错信息（也已保存到 phisap_error.log）。
    pause
    exit /b 1
)
exit /b 0
