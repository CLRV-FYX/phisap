#!/system/bin/sh
# PhiSkin-style loading path: LSPosed calls assets/xposed_init inside the game
# process; HookEntry copies the APK asset and invokes System.load after Application.attach.
# This root helper only prepares shared config/tapd, launches if needed, and
# verifies /proc/<pid>/maps. It never claims success from process detection alone.
FILES="$1"
LIBDIR="$2"
PKG="$3"
DW="$4"
DH="$5"
ROT="$6"
D=/data/local/tmp

say() {
  printf '%s\n' "$*" > "$D/phisap-status"
  chmod 666 "$D/phisap-status" 2>/dev/null || true
  echo "$*"
}

if [ -z "$PKG" ]; then
  say "没有包名"
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

say "正在准备游戏内加载"
stop_old_tapd

TAP="$LIBDIR/libphisap-tapd.so"
if [ ! -f "$TAP" ] && [ -f "$FILES/phisap-tapd" ]; then
  cp -f "$FILES/phisap-tapd" "$D/phisap-tapd" 2>/dev/null || true
  TAP="$D/phisap-tapd"
fi
if [ ! -f "$TAP" ]; then
  say "没有触摸守护"
  exit 1
fi
chmod 755 "$TAP" 2>/dev/null || true

# The Xposed entry reads these before Application.attach. Prepare them before
# starting the game; do not start the game from the app process first.
printf 'dw=%s\ndh=%s\nrot=%s\nuw=0\nuh=0\n' "$DW" "$DH" "$ROT" > "$D/phisap-hook.cfg"
chmod 666 "$D/phisap-hook.cfg" 2>/dev/null || true
printf '%s\n' "$PKG" > "$D/phisap-target"
chmod 666 "$D/phisap-target" 2>/dev/null || true
: > "$D/phisap-hook.log"
chmod 666 "$D/phisap-hook.log" 2>/dev/null || true
rm -f "$D/phisap-stop"

if ! pidof phisap-tapd >/dev/null 2>&1 && ! pidof libphisap-tapd.so >/dev/null 2>&1; then
  cp -f "$TAP" "$D/phisap-tapd" 2>/dev/null || true
  chmod 755 "$D/phisap-tapd" 2>/dev/null || true
  if [ -x "$D/phisap-tapd" ]; then
    "$D/phisap-tapd" >>"$D/phisap-tapd.log" 2>&1 &
    sleep 0.2
  fi
  if ! pidof phisap-tapd >/dev/null 2>&1 && ! pidof libphisap-tapd.so >/dev/null 2>&1; then
    "$TAP" >>"$D/phisap-tapd.log" 2>&1 &
    sleep 0.2
  fi
fi

if ! pm path "$PKG" >/dev/null 2>&1; then
  say "没找到游戏包 $PKG"
  exit 1
fi

game_pids() {
  pidof "$1" 2>/dev/null || true
  for d in /proc/[0-9]*; do
    [ -r "$d/cmdline" ] || continue
    cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    case "$cmd" in
      "$1"|"$1 "*|"$1:"*) echo "${d##*/}" ;;
    esac
  done
}

hooked_pid() {
  for pid in $(game_pids "$PKG"); do
    if [ -r "/proc/$pid/maps" ] && grep -q 'libphisap\.so' "/proc/$pid/maps" 2>/dev/null; then
      echo "$pid"
      return 0
    fi
  done
  return 1
}

stopped() {
  [ -f "$D/phisap-stop" ]
}

launch() {
  comp=$(cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER "$PKG" 2>/dev/null | tail -n 1)
  case "$comp" in
    */*) am start --user 0 -n "$comp" >/dev/null 2>&1 || true ;;
    *)
      am start --user 0 -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p "$PKG" >/dev/null 2>&1 || true
      monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
      ;;
  esac
}

run_boot() {
  existing=$(game_pids "$PKG")
  if [ -n "$existing" ]; then
    # Give an attach already in progress a short chance to finish, but never
    # attach to or silently restart a process that was already open without LSPosed.
    i=0
    while [ "$i" -lt 25 ]; do
      if pid=$(hooked_pid); then
        say "已确认 libphisap.so 映射在游戏进程 $pid"
        return 0
      fi
      stopped && { say "已停止"; return 1; }
      i=$((i + 1))
      sleep 0.2
    done
    say "游戏进程仍在运行，但 maps 中没有 libphisap.so。LSPosed 只在进程启动时加载模块，不能追溯注入；启用 PhiSAP 并勾选 $PKG 后，请强制停止再重新打开游戏。当前未注入。"
    return 1
  fi

  say "正在打开游戏，等待进程内 System.load"
  launch
  i=0
  while [ "$i" -lt 180 ]; do
    stopped && { say "已停止"; return 1; }
    if pid=$(hooked_pid); then
      say "已确认 libphisap.so 映射在游戏进程 $pid"
      return 0
    fi
    if [ -z "$(game_pids "$PKG")" ] && [ "$i" -gt 20 ]; then
      break
    fi
    i=$((i + 1))
    sleep 0.5
  done

  if [ -n "$(game_pids "$PKG")" ]; then
    say "游戏已打开，但 maps 中没有 libphisap.so。请确认 LSPosed 中已启用 PhiSAP、作用域包含 $PKG，并在重启游戏后再试。没有把进程检测当作注入成功。"
  else
    say "游戏没有启动，且未检测到 libphisap.so"
  fi
  return 1
}

if run_boot; then
  exit 0
fi
exit 1
