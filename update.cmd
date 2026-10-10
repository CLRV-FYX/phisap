@echo off
chcp 65001 >nul
title phisap 更新器
cd /d "%~dp0"
set "PHISAP_SELF=%~f0"
rem 以下整行在执行前已被 cmd 完整读取，因此更新过程中本文件被覆盖也不会出错
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText($env:PHISAP_SELF,[Text.Encoding]::UTF8); $i=$s.IndexOf('#PHISAP'+'_UPDATER_BEGIN'); Invoke-Expression $s.Substring($i)" & echo. & pause & exit /b
#PHISAP_UPDATER_BEGIN
# ---------------------------------------------------------------------------
# 以下为 PowerShell 代码（由上面的 cmd 行读取本文件并执行），兼容 Windows 自带的 PowerShell 5.1
# ---------------------------------------------------------------------------
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # 关闭进度条，否则 PowerShell 5.1 下载会非常慢
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$Repo   = 'CLRV-FYX/phisap'
$Branch = 'arena/47d37e24-phisap'
# 更新时保留的用户数据（其余旧文件会先移入 .old_version 备份目录，再放入新版本文件）
$Keep   = @('Assets', 'cache', '.git', '.old_version')
# 下载地址：依次尝试，GitHub 直连失败时使用第三方加速镜像
$ZipUrls = @(
    "https://codeload.github.com/$Repo/zip/refs/heads/$Branch",
    "https://github.com/$Repo/archive/refs/heads/$Branch.zip",
    "https://ghfast.top/https://github.com/$Repo/archive/refs/heads/$Branch.zip",
    "https://gh-proxy.com/https://github.com/$Repo/archive/refs/heads/$Branch.zip"
)

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

function Update-Phisap {
    $Root = Split-Path -Parent $env:PHISAP_SELF
    Set-Location -LiteralPath $Root
    $VersionFile = Join-Path $Root '.phisap_version'
    Say ''
    Say "phisap 更新器  (仓库 $Repo, 分支 $Branch)" 'Cyan'
    Say "安装目录: $Root"

    # ---- 用 git 克隆的目录：直接 git pull ----
    if ((Test-Path -LiteralPath (Join-Path $Root '.git')) -and (Get-Command git -ErrorAction SilentlyContinue)) {
        Say ''
        Say '[1/1] 检测到 git 仓库，执行 git pull ...' 'Cyan'
        & git -C $Root pull --ff-only origin $Branch
        if ($LASTEXITCODE -ne 0) { throw 'git pull 失败（可能本地有未提交的修改），请手动处理' }
        Say '[完成] 已更新到最新代码' 'Green'
        return
    }

    # ---- 是否正在运行 ----
    try {
        $running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
            Where-Object { $_.CommandLine -and $_.CommandLine -like '*main.py*' }
        if ($running) { Say '[警告] 检测到 phisap 可能正在运行，建议先关闭它再更新。' 'Yellow' }
    } catch {}

    # ---- 查询最新版本 ----
    Say ''
    Say '[1/4] 查询最新版本...' 'Cyan'
    $remote = $null
    try {
        $info = Invoke-RestMethod -UseBasicParsing -TimeoutSec 20 -Uri "https://api.github.com/repos/$Repo/commits/$Branch"
        $remote = [string]$info.sha
        $msg = ([string]$info.commit.message -split "`n")[0]
        Say ("      最新版本: {0}  ({1})" -f $remote.Substring(0, 7), $info.commit.committer.date)
        Say ("      更新内容: {0}" -f $msg)
    } catch {
        Say '      无法查询版本信息（GitHub API 不可用），将直接下载最新代码' 'Yellow'
    }
    $local = $null
    if (Test-Path -LiteralPath $VersionFile) { $local = (Get-Content -LiteralPath $VersionFile -Raw).Trim() }
    if ($local) { Say ("      当前版本: {0}" -f $local.Substring(0, [Math]::Min(7, $local.Length))) } else { Say '      当前版本: 未知' }
    if ($remote -and $local -eq $remote) {
        $ans = Read-Host '      已经是最新版本。仍要重新下载并覆盖吗？[y/N]'
        if ($ans -notmatch '^[yY]') { Say '[完成] 无需更新' 'Green'; return }
    }

    # ---- 下载并解压 ----
    Say ''
    Say '[2/4] 下载最新代码...' 'Cyan'
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('phisap_update_' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp 'phisap.zip'
    $ext = Join-Path $tmp 'x'
    $src = $null
    try {
        foreach ($url in $ZipUrls) {
            Say "      尝试: $url"
            try {
                if (Test-Path -LiteralPath $ext) { Remove-Item -LiteralPath $ext -Recurse -Force }
                Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri $url -OutFile $zip
                Expand-Archive -LiteralPath $zip -DestinationPath $ext -Force
                $dir = Get-ChildItem -LiteralPath $ext -Directory | Select-Object -First 1
                if ($dir -and (Test-Path -LiteralPath (Join-Path $dir.FullName 'main.py'))) { $src = $dir.FullName; break }
                Say '      下载内容不完整，换下一个地址' 'Yellow'
            } catch {
                Say ("      失败: {0}" -f $_.Exception.Message) 'Yellow'
            }
        }
        if (-not $src) { throw '所有下载地址均失败，请检查网络（或挂代理）后重试。当前文件未做任何改动。' }
        $count = (Get-ChildItem -LiteralPath $src -Recurse -File -Force | Measure-Object).Count
        Say "      下载完成，共 $count 个文件" 'Green'

        # ---- 移走旧文件 ----
        Say ''
        Say '[3/4] 删除旧文件（备份到 .old_version，保留 Assets 谱面库 和 cache 设置）...' 'Cyan'
        $bak = Join-Path $Root '.old_version'
        if (Test-Path -LiteralPath $bak) { Remove-Item -LiteralPath $bak -Recurse -Force }
        New-Item -ItemType Directory -Path $bak | Out-Null
        try { $f = Get-Item -LiteralPath $bak -Force; $f.Attributes = $f.Attributes -bor [IO.FileAttributes]::Hidden } catch {}
        foreach ($item in Get-ChildItem -LiteralPath $Root -Force) {
            if ($Keep -contains $item.Name) { continue }
            try {
                Move-Item -LiteralPath $item.FullName -Destination $bak -Force
            } catch {
                # 被占用无法移动的文件（例如正在运行的 update.cmd 本身）：先复制一份备份，稍后直接覆盖
                try { Copy-Item -LiteralPath $item.FullName -Destination $bak -Recurse -Force } catch {}
            }
        }

        # ---- 放入新文件 ----
        Say ''
        Say '[4/4] 安装新文件...' 'Cyan'
        foreach ($item in Get-ChildItem -LiteralPath $src -Force) {
            if ($Keep -contains $item.Name) { continue }
            $dest = Join-Path $Root $item.Name
            if ($item.PSIsContainer -and (Test-Path -LiteralPath $dest)) {
                Copy-Item -Path (Join-Path $item.FullName '*') -Destination $dest -Recurse -Force
            } else {
                Copy-Item -LiteralPath $item.FullName -Destination $dest -Recurse -Force
            }
        }
        if ($remote) { Set-Content -LiteralPath $VersionFile -Value $remote -Encoding ASCII -Force }
        try { $f = Get-Item -LiteralPath $VersionFile -Force; $f.Attributes = $f.Attributes -bor [IO.FileAttributes]::Hidden } catch {}
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }

    Say ''
    Say '[完成] 已更新到最新代码！运行 start.cmd 即可启动。' 'Green'
    Say '       旧版本文件已备份在隐藏目录 .old_version 中（下次更新时会被覆盖）。'
    Say '       安卓：安装 android\phisap-pocket.apk 并授予 root 即可；APK 内置进程注入器，无需安装或配置 LSPosed。游戏已打开时会直接注入并核对 maps。' 'Yellow'
}

try {
    Update-Phisap
} catch {
    Say ''
    Say ("[错误] 更新失败: {0}" -f $_.Exception.Message) 'Red'
    Say '       如果旧文件已被移走，可以从 .old_version 目录中恢复。' 'Red'
}
