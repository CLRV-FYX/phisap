#!/system/bin/sh
# Single-APK root launcher/injector. Existing game processes are injected in place.
FILES="$1"
LIBDIR="$2"
PKG="$3"
DW="$4"
DH="$5"
ROT="$6"
D=/data/local/tmp
APP_STATUS="$FILES/status.txt"
HOOK_STATUS="/data/user/0/$PKG/files/phisap-status"
HOOK_LOG="$D/phisap-hook.log"
INJECTOR_LOG="$D/phisap-inject.log"
SELINUX_OLD=""
SELINUX_CHANGED=0
SELINUX_RESTORE_FAILED=0
LAST_ERR=""

restore_selinux() {
  if [ "$SELINUX_CHANGED" = 1 ]; then
    if setenforce 1 >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null)" = "Enforcing" ]; then
      SELINUX_CHANGED=0
      SELINUX_RESTORE_FAILED=0
    else
      SELINUX_RESTORE_FAILED=1
    fi
  fi
}
trap 'restore_selinux' EXIT
trap 'restore_selinux; exit 130' INT
trap 'restore_selinux; exit 143' TERM

APP_UID=$(stat -c %u /data/user/0/app.phisap.pocket 2>/dev/null || true)

say() {
  msg="$*"
  mkdir -p "$D" 2>/dev/null || true
  printf '%s\n' "$msg" > "$D/phisap-status" 2>/dev/null || true
  chmod 666 "$D/phisap-status" 2>/dev/null || true
  if [ -n "$APP_STATUS" ] && [ -d "$FILES" ]; then
    printf '%s\n' "$msg" > "$APP_STATUS" 2>/dev/null || true
    chmod 644 "$APP_STATUS" 2>/dev/null || true
    if [ -n "$APP_UID" ]; then chown "$APP_UID:$APP_UID" "$APP_STATUS" 2>/dev/null || true; fi
    restorecon "$APP_STATUS" 2>/dev/null || true
  fi
  echo "$msg"
}

if [ "$(id -u 2>/dev/null)" != 0 ]; then
  say "没有获得 root 权限；请在 SU 弹窗中允许 phisap"
  exit 1
fi
if [ -z "$PKG" ]; then
  say "没有包名"
  exit 1
fi
case "$PKG" in
  com.PigeonGames.Phigros|org.flos.phira|org.flos.phira.modded) ;;
  *) say "不支持的游戏包：$PKG"; exit 1 ;;
esac
if ! pm path "$PKG" >/dev/null 2>&1; then
  say "没找到游戏包 $PKG"
  exit 1
fi
mkdir -p "$D" /sdcard/phisap 2>/dev/null || true

stop_old_tapd() {
  touch "$D/phisap-stop" 2>/dev/null || true
  killall phisap-tapd libphisap-tapd.so 2>/dev/null || true
  i=0
  while [ "$i" -lt 20 ]; do
    if ! pidof phisap-tapd >/dev/null 2>&1 && ! pidof libphisap-tapd.so >/dev/null 2>&1; then break; fi
    killall phisap-tapd libphisap-tapd.so 2>/dev/null || true
    sleep 0.1
    i=$((i + 1))
  done
  rm -f "$D/phisap-stop"
}

say "正在准备 root 触摸和游戏加载"
stop_old_tapd
TAP="$LIBDIR/libphisap-tapd.so"
if [ ! -f "$TAP" ] && [ -f "$FILES/phisap-tapd" ]; then
  TAP="$FILES/phisap-tapd"
fi
if [ ! -f "$TAP" ]; then
  say "APK 内没有触摸守护"
  exit 1
fi
chmod 755 "$TAP" 2>/dev/null || true
printf 'dw=%s\ndh=%s\nrot=%s\nuw=0\nuh=0\n' "$DW" "$DH" "$ROT" > "$D/phisap-hook.cfg"
chmod 644 "$D/phisap-hook.cfg" 2>/dev/null || true
printf '%s\n' "$PKG" > "$D/phisap-target"
chmod 644 "$D/phisap-target" 2>/dev/null || true
: > "$HOOK_LOG"
chmod 666 "$HOOK_LOG" 2>/dev/null || true
rm -f "$D/phisap-stop" "$D/phisap-inject.err" 2>/dev/null || true

if ! pidof phisap-tapd >/dev/null 2>&1 && ! pidof libphisap-tapd.so >/dev/null 2>&1; then
  cp -f "$TAP" "$D/phisap-tapd" 2>/dev/null || true
  chmod 755 "$D/phisap-tapd" 2>/dev/null || true
  if [ -x "$D/phisap-tapd" ]; then
    "$D/phisap-tapd" >>"$D/phisap-tapd.log" 2>&1 &
  fi
fi
i=0
while [ "$i" -lt 20 ]; do
  if pidof phisap-tapd >/dev/null 2>&1 || pidof libphisap-tapd.so >/dev/null 2>&1; then break; fi
  i=$((i + 1))
  sleep 0.1
done
if ! pidof phisap-tapd >/dev/null 2>&1 && ! pidof libphisap-tapd.so >/dev/null 2>&1; then
  tap_err=$(tail -n 4 "$D/phisap-tapd.log" 2>/dev/null | tr '\n' ' ' | cut -c1-120)
  if [ -n "$tap_err" ]; then say "触摸守护启动失败：$tap_err"; else say "触摸守护启动失败（请检查 /dev/uinput）"; fi
  exit 1
fi

if [ "$PKG" != "com.PigeonGames.Phigros" ]; then
  game_pids() {
    pidof "$PKG" 2>/dev/null || true
    for proc in /proc/[0-9]*; do
      [ -r "$proc/cmdline" ] || continue
      cmdline=$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null) || continue
      case "$cmdline" in
        "$PKG"|"$PKG "*|"$PKG:"*) echo "${proc##*/}" ;;
      esac
    done
  }
  if [ -z "$(game_pids)" ]; then
    say "正在打开 Phira"
    launch_pkg() {
      component=$(cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER "$PKG" 2>/dev/null | tail -n 1)
      case "$component" in
        */*) am start --user 0 -n "$component" >/dev/null 2>&1 || true ;;
        *) am start --user 0 -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p "$PKG" >/dev/null 2>&1 || true
           monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true ;;
      esac
    }
    launch_pkg
  fi
  i=0
  while [ "$i" -lt 60 ] && [ -z "$(game_pids)" ]; do
    [ -f "$D/phisap-stop" ] && { say "已停止"; exit 1; }
    i=$((i + 1))
    sleep 0.5
  done
  if [ -z "$(game_pids)" ]; then
    say "Phira 没有启动；root 触摸守护已就绪"
    exit 1
  fi
  say "Phira 已运行，root 触摸守护已就绪；Phira 不使用 Phigros 原生钩子"
  exit 0
fi

SO="$LIBDIR/libphisap.so"
if [ ! -f "$SO" ] && [ -f "$FILES/libphisap.so" ]; then SO="$FILES/libphisap.so"; fi
if [ ! -f "$SO" ]; then
  say "APK 内没有 libphisap.so"
  exit 1
fi
INJ="$LIBDIR/libphisap-inject.so"
if [ ! -f "$INJ" ] && [ -f "$FILES/phisap-inject" ]; then INJ="$FILES/phisap-inject"; fi
if [ ! -f "$INJ" ]; then
  say "APK 内没有 root 注入器"
  exit 1
fi
chmod 755 "$SO" "$INJ" 2>/dev/null || true

GAME_DIR="/data/user/0/$PKG/files"
GAME_ROOT="/data/user/0/$PKG"
mkdir -p "$GAME_DIR" 2>/dev/null || true
GAMESO="$GAME_DIR/libphisap.so"
if ! cp -f "$SO" "$GAMESO" 2>/dev/null; then
  say "不能把钩子库复制到游戏私有目录：$GAME_DIR"
  exit 1
fi
GAME_UID=$(stat -c %u "$GAME_ROOT" 2>/dev/null || true)
if [ -n "$GAME_UID" ]; then chown "$GAME_UID:$GAME_UID" "$GAMESO" 2>/dev/null || chown "$GAME_UID" "$GAMESO" 2>/dev/null || true; fi
chmod 555 "$GAMESO" 2>/dev/null || true
restorecon "$GAMESO" 2>/dev/null || true
if [ ! -r "$GAMESO" ]; then
  say "游戏进程无权读取钩子库：$GAMESO"
  exit 1
fi
SO_USE="$GAMESO"

stopped() { [ -f "$D/phisap-stop" ]; }
game_pids() {
  pidof "$PKG" 2>/dev/null || true
  for proc in /proc/[0-9]*; do
    [ -r "$proc/cmdline" ] || continue
    cmdline=$(tr '\0' ' ' < "$proc/cmdline" 2>/dev/null) || continue
    case "$cmdline" in
      "$PKG"|"$PKG "*|"$PKG:"*) echo "${proc##*/}" ;;
    esac
  done
}
find_ready_pid() {
  for pid in $(game_pids); do
    [ -r "/proc/$pid/maps" ] || continue
    if grep -q 'libil2cpp\.so' "/proc/$pid/maps" 2>/dev/null &&
       grep -q 'libc\.so' "/proc/$pid/maps" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
  done
  return 1
}
hooked_pid() {
  for pid in $(game_pids); do
    [ -r "/proc/$pid/maps" ] || continue
    if grep -q 'libphisap\.so' "/proc/$pid/maps" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
  done
  return 1
}
launch_game() {
  component=$(cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER "$PKG" 2>/dev/null | tail -n 1)
  case "$component" in
    */*) am start --user 0 -n "$component" >/dev/null 2>&1 || true ;;
    *) am start --user 0 -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p "$PKG" >/dev/null 2>&1 || true
       monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true ;;
  esac
}

pid=$(hooked_pid || true)
if [ -n "$pid" ]; then
  READY_PID="$pid"
  say "libphisap.so 已在游戏进程 $pid 中；跳过重复注入并核对钩子状态"
else
  # A stale status file from a prior process must not be accepted as this load's handshake.
  rm -f "$HOOK_STATUS" 2>/dev/null || true
  STARTED_HERE=0
  if [ -z "$(game_pids)" ]; then
    STARTED_HERE=1
    say "游戏未运行，正在启动 Phigros"
    launch_game
  else
    say "检测到 Phigros 正在运行；不强停，直接处理现有进程"
  fi

  READY_PID=""
  i=0
  while [ "$i" -lt 90 ]; do
    stopped && { say "已停止"; exit 1; }
    READY_PID=$(find_ready_pid || true)
    [ -n "$READY_PID" ] && break
    if [ "$STARTED_HERE" = 0 ] && [ -z "$(game_pids)" ]; then
      break
    fi
    if [ $((i % 10)) -eq 9 ]; then say "等待 Phigros 加载 il2cpp（$(( (i + 1) / 2 )) 秒）"; fi
    i=$((i + 1))
    sleep 0.5
  done
  if [ -z "$READY_PID" ]; then
    if [ -n "$(game_pids)" ]; then
      say "检测到游戏进程，但 45 秒内没有 libil2cpp.so；未注入，请到游戏主界面后重试"
    else
      say "Phigros 没有启动；未执行注入"
    fi
    exit 1
  fi

  inject_once() {
    target_pid="$1"
    output=$("$INJ" "$target_pid" "$SO_USE" 2>&1)
    rc=$?
    if [ "$rc" -ne 0 ]; then
      LAST_ERR=$(printf '%s' "$output" | tr '\n' ' ' | cut -c1-160)
      [ -n "$LAST_ERR" ] || LAST_ERR="注入器退出码 $rc"
      printf '%s\n' "$LAST_ERR" > "$D/phisap-inject.err" 2>/dev/null || true
      printf '%s\n' "$LAST_ERR" >> "$INJECTOR_LOG" 2>/dev/null || true
      return 1
    fi
    j=0
    while [ "$j" -lt 20 ]; do
      mapped=$(hooked_pid || true)
      if [ -n "$mapped" ]; then
        READY_PID="$mapped"
        return 0
      fi
      j=$((j + 1))
      sleep 0.25
    done
    LAST_ERR="注入器返回成功，但 /proc/$target_pid/maps 没有 libphisap.so"
    printf '%s\n' "$LAST_ERR" > "$D/phisap-inject.err" 2>/dev/null || true
    return 1
  }

  say "正在对运行中的 Phigros 进程 $READY_PID 执行 root 注入"
  if ! inject_once "$READY_PID"; then
    if [ "$SELINUX_OLD" = "" ]; then SELINUX_OLD=$(getenforce 2>/dev/null || true); fi
    if [ "$SELINUX_OLD" = "Enforcing" ]; then
      say "首次注入未成功（$LAST_ERR）；尝试一次临时 SELinux 放行"
      if setenforce 0 >/dev/null 2>&1; then
        SELINUX_CHANGED=1
        if [ "$(getenforce 2>/dev/null)" = "Permissive" ]; then
          inject_once "$READY_PID" || true
        else
          LAST_ERR="$LAST_ERR；SELinux 状态无法确认已放行"
        fi
        restore_selinux
      else
        LAST_ERR="$LAST_ERR；SELinux 无法临时放行"
      fi
    fi
  fi
  if [ -z "$(hooked_pid || true)" ]; then
    restore_selinux
    if [ "$SELINUX_CHANGED" = 1 ]; then LAST_ERR="$LAST_ERR；无法恢复 SELinux Enforcing"; fi
    say "root 注入失败：$LAST_ERR"
    exit 1
  fi
  restore_selinux
  if [ "$SELINUX_CHANGED" = 1 ]; then
    say "库已映射，但无法恢复 SELinux Enforcing；请立即检查 getenforce"
    exit 1
  fi
  pid=$(hooked_pid || true)
  [ -n "$pid" ] || pid="$READY_PID"
fi
[ -n "$pid" ] || pid="$READY_PID"
say "已确认 libphisap.so 映射在 Phigros 进程 $pid；正在核对原生钩子状态"

# Verify the library constructor progressed. This is bounded; failure is shown
# to the user instead of leaving the app in an endless "loading" state.
last_hook=""
i=0
while [ "$i" -lt 60 ]; do
  stopped && { say "已停止"; exit 1; }
  if [ -r "$HOOK_STATUS" ]; then
    hook=$(tr '\n' ' ' < "$HOOK_STATUS" 2>/dev/null | cut -c1-160)
    if [ -n "$hook" ] && [ "$hook" != "$last_hook" ]; then
      last_hook="$hook"
      say "钩子状态：$hook"
    fi
    case "$hook" in
      已挂钩*)
        say "已加载并挂钩：$hook"
        exit 0
        ;;
      "没找到 il2cpp"*|"没有 JudgeLineControl"*|"方法对不上"*|"钩子线程没起来"*)
        say "库已映射，但原生钩子失败：$hook"
        exit 1
        ;;
    esac
  fi
  i=$((i + 1))
  sleep 0.5
done
if [ -n "$last_hook" ]; then
  say "库已映射到 PID $pid，但 30 秒内钩子未完成：$last_hook"
else
  say "库已映射到 PID $pid，但 30 秒内没有收到钩子状态；可检查 $HOOK_LOG"
fi
exit 1
