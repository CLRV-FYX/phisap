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
python -m pip install --upgrade pip
python -m pip install --prefer-binary --only-binary=av -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败，请检查网络后重新运行本脚本。
    echo        如果提示找不到 av 的可用版本，说明当前 Python 版本过新/过旧，
    echo        请安装 Python 3.11 或更高版本（64 位）后重试。
    pause
    exit /b 1
)

echo.
echo [2/4] 检查 scrcpy-server（已随仓库提供，缺失时自动下载）...
if exist scrcpy-server-v2.0 (
    for %%F in (scrcpy-server-v2.0) do if %%~zF==0 del scrcpy-server-v2.0
)
if not exist scrcpy-server-v2.0 (
    echo 正在从 GitHub 下载 scrcpy-server-v2.0 ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://github.com/Genymobile/scrcpy/releases/download/v2.0/scrcpy-server-v2.0' -OutFile 'scrcpy-server-v2.0'"
    if not exist scrcpy-server-v2.0 (
        echo [错误] 下载失败。请手动到 https://github.com/Genymobile/scrcpy/releases 的
        echo        v2.0 版本页面下载 scrcpy-server-v2.0 ，并将其放到本目录（与 main.py 同级）。
        pause
        exit /b 1
    )
)
echo [OK] scrcpy-server-v2.0 已就绪

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
where pythonw >nul 2>nul
if errorlevel 1 (
    start "" python main.py
) else (
    start "" pythonw main.py
)
exit /b 0
