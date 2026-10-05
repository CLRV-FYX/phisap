#!/system/bin/sh
# 方式二：悬浮窗已经开着。这里只负责把钩子送进游戏，并把每一步写到状态文件。
# 不把「等 il2cpp」写死。注入失败、进程还没起来、钩子已进，都改这一行。
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

mkdir -p "$D" /sdcard/phisap 2>/dev/null
rm -f "$D/phisap-stop"
say "悬浮窗开着，正在进游戏"

SO="$LIBDIR/libphisap.so"
TAP="$LIBDIR/libphisap-tapd.so"
INJ="$LIBDIR/libphisap-inject.so"
if [ ! -f "$SO" ]; then
  if [ -f "$FILES/libphisap.so" ]; then
    cp -f "$FILES/libphisap.so" "$D/libphisap.so"
    SO="$D/libphisap.so"
  else
    say "没有 libphisap.so"
    exit 1
  fi
fi
if [ ! -f "$TAP" ]; then
  cp -f "$FILES/phisap-tapd" "$D/phisap-tapd" || { say "没有触摸守护"; exit 1; }
  TAP="$D/phisap-tapd"
fi
if [ ! -f "$INJ" ]; then
  cp -f "$FILES/phisap-inject" "$D/phisap-inject" || { say "没有注入器"; exit 1; }
  INJ="$D/phisap-inject"
fi
chmod 755 "$SO" "$TAP" "$INJ" 2>/dev/null || true
cp -f "$SO" "$D/libphisap.so" 2>/dev/null || true
chmod 755 "$D/libphisap.so" 2>/dev/null || true
chcon u:object_r:system_file:s0 "$D/libphisap.so" 2>/dev/null || true

GAMESO="/data/user/0/$PKG/files/libphisap.so"
mkdir -p "/data/user/0/$PKG/files" 2>/dev/null || true
if cp -f "$SO" "$GAMESO" 2>/dev/null; then
  chmod 755 "$GAMESO" 2>/dev/null || true
  chcon u:object_r:app_data_file:s0 "$GAMESO" 2>/dev/null || true
  owner=$(stat -c %u "/data/user/0/$PKG" 2>/dev/null || true)
  if [ -n "$owner" ]; then
    chown "$owner:$owner" "$GAMESO" 2>/dev/null || chown "$owner" "$GAMESO" 2>/dev/null || true
  fi
else
  GAMESO=""
fi

printf 'dw=%s\ndh=%s\nrot=%s\nuw=0\nuh=0\n' "$DW" "$DH" "$ROT" > "$D/phisap-hook.cfg"
chmod 666 "$D/phisap-hook.cfg"
: > "$D/phisap-hook.log"
chmod 666 "$D/phisap-hook.log"
printf '%s\n' "$PKG" > "$D/phisap-target"
chmod 666 "$D/phisap-target" 2>/dev/null || true
SO_USE="$D/libphisap.so"
if [ -n "$GAMESO" ] && [ -f "$GAMESO" ]; then
  SO_USE="$GAMESO"
fi

# 不设置 wrap.包名。PhiSkin 也是正常启动游戏再进进程。
# wrap 会让系统每次拉起游戏都先跑脚本，脚本一失败，图标点了也打不开。

if ! pidof phisap-tapd >/dev/null 2>&1; then
  cp -f "$TAP" "$D/phisap-tapd" 2>/dev/null || true
  chmod 755 "$D/phisap-tapd" 2>/dev/null || true
  if [ -x "$D/phisap-tapd" ]; then
    "$D/phisap-tapd" >>"$D/phisap-tapd.log" 2>&1 &
    sleep 0.2
  fi
  if ! pidof phisap-tapd >/dev/null 2>&1; then
    "$TAP" >>"$D/phisap-tapd.log" 2>&1 &
    sleep 0.2
  fi
fi
cp -f "$INJ" "$D/phisap-inject" 2>/dev/null || true
chmod 755 "$D/phisap-inject" 2>/dev/null || true

echo 0 > /proc/sys/kernel/yama/ptrace_scope 2>/dev/null
OLD=$(getenforce 2>/dev/null || true)
restore() {
  if [ "$OLD" = "Enforcing" ]; then
    setenforce 1 2>/dev/null || true
  fi
}
clear_wrap() {
  setprop "wrap.$PKG" "" 2>/dev/null || true
  setprop wrap.com.PigeonGames.Phigros "" 2>/dev/null || true
  setprop wrap.org.flos.phira "" 2>/dev/null || true
  setprop wrap.org.flos.phira.modded "" 2>/dev/null || true
  resetprop --delete "wrap.$PKG" 2>/dev/null || true
  resetprop --delete wrap.com.PigeonGames.Phigros 2>/dev/null || true
  resetprop --delete wrap.org.flos.phira 2>/dev/null || true
  resetprop --delete wrap.org.flos.phira.modded 2>/dev/null || true
  rm -f "$D/phisap-wrap.sh"
}
unlock_game() {
  clear_wrap
  killall phisap-inject 2>/dev/null || true
  killall libphisap-inject.so 2>/dev/null || true
}
trap 'unlock_game; restore' EXIT
unlock_game

if ! pm path "$PKG" >/dev/null 2>&1; then
  say "没找到 $PKG"
  exit 1
fi

game_pids() {
  pidof "$1" 2>/dev/null || true
  for d in /proc/[0-9]*; do
    cmd=$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null) || continue
    case "$cmd" in
      "$1"|"$1 "*|"$1:"*)
        echo "${d##*/}"
        ;;
    esac
  done
}

unstick() {
  for pid in $(game_pids "$PKG"); do
    st=$(awk '/^State:/{print $2; exit}' "/proc/$pid/status" 2>/dev/null || true)
    case "$st" in
      t|T) kill -CONT "$pid" 2>/dev/null || true ;;
    esac
  done
}

stopped() {
  [ -f "$D/phisap-stop" ]
}

hooked() {
  side="/data/user/0/$PKG/files/phisap-status"
  if [ -s "$side" ]; then
    return 0
  fi
  if [ -f "$D/phisap-hook.log" ] && grep -q 'phisap-hook' "$D/phisap-hook.log" 2>/dev/null; then
    return 0
  fi
  return 1
}

relay() {
  side="/data/user/0/$PKG/files/phisap-status"
  if [ -s "$side" ]; then
    msg=$(tr '\n' ' ' < "$side" | cut -c1-80)
    say "$msg"
    return 0
  fi
  return 1
}

launch() {
  clear_wrap
  comp=$(cmd package resolve-activity --brief -a android.intent.action.MAIN -c android.intent.category.LAUNCHER "$PKG" 2>/dev/null | tail -n 1)
  case "$comp" in
    */*)
      am start --user 0 -n "$comp" >/dev/null 2>&1 || true
      ;;
    *)
      am start --user 0 -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p "$PKG" >/dev/null 2>&1 || true
      monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1 || true
      ;;
  esac
}

try_inject() {
  pid="$1"
  so="$2"
  out=""
  if [ -x "$INJ" ]; then
    out=$("$INJ" "$pid" "$so" 2>&1) && return 0
  fi
  if [ -x "$D/phisap-inject" ] && [ "$D/phisap-inject" != "$INJ" ]; then
    out=$("$D/phisap-inject" "$pid" "$so" 2>&1) && return 0
  fi
  printf '%s' "$out" | tr '\n' ' ' | cut -c1-64
  return 1
}

do_inject() {
  pids="$1"
  seen=" "
  err=""
  if [ "$OLD" = "Enforcing" ]; then
    setenforce 0 2>/dev/null || true
  fi
  for pid in $pids; do
    case "$seen" in
      *" $pid "*) continue ;;
    esac
    seen="$seen$pid "
    for so in $GAMESO "$D/libphisap.so" "$SO"; do
      [ -n "$so" ] && [ -f "$so" ] || continue
      err=$(try_inject "$pid" "$so") && {
        say "已送进，等钩子回报"
        return 0
      }
      case "$err" in
        *读寄存器*|*附加上不去*)
          break 2
          ;;
      esac
    done
  done
  if [ "$OLD" = "Enforcing" ]; then
    setenforce 1 2>/dev/null || true
  fi
  printf '%s\n' "$err" > "$D/phisap-inject.err" 2>/dev/null || true
  printf '%s\n' "$err" >> "$D/phisap-inject.log" 2>/dev/null || true
  return 1
}

# 寄存器被锁时不再反复读。让游戏重新起来的那一下把库送进去，包装用完就撤。
run_boot() {
  say "寄存器被锁，改为启动时送入"
  clear_wrap
  if [ "$OLD" = "Enforcing" ]; then
    setenforce 0 2>/dev/null || true
  fi
  out=""
  if [ -x "$D/phisap-inject" ]; then
    out=$("$D/phisap-inject" boot "$PKG" "$SO_USE" 2>&1) || true
  elif [ -x "$INJ" ]; then
    out=$("$INJ" boot "$PKG" "$SO_USE" 2>&1) || true
  fi
  printf '%s\n' "$out" >> "$D/phisap-boot.log" 2>/dev/null || true
  clear_wrap
  case "$out" in
    *ok*)
      j=0
      while [ "$j" -lt 20 ]; do
        if hooked; then
          relay || say "钩子已在游戏里"
          return 0
        fi
        j=$((j + 1))
        sleep 0.3
      done
      say "库已送进，等钩子回报"
      return 0
      ;;
  esac
  say "启动时没送进，包装已撤，游戏可以自己打开"
  return 1
}

unstick
say "正在打开游戏"
launch
i=0
last=-8
skip_inject=0
while [ "$i" -lt 480 ]; do
  if stopped; then
    say "已停止"
    exit 0
  fi
  if hooked; then
    relay || say "钩子已在游戏里"
    i=$((i + 1))
    sleep 0.5
    continue
  fi
  pids=$(game_pids "$PKG")
  if [ -z "$pids" ]; then
    if [ "$skip_inject" -eq 0 ]; then
      say "正在打开游戏"
    fi
    if [ $((i % 4)) -eq 0 ]; then
      launch
    fi
  elif [ "$skip_inject" -eq 0 ]; then
    say "已打开游戏，正在送进进程"
    if [ $((i - last)) -ge 4 ]; then
      last=$i
      if ! do_inject "$pids"; then
        err=$(tr '\n' ' ' < "$D/phisap-inject.err" 2>/dev/null | cut -c1-40)
        case "$err" in
          *读寄存器*|*附加上不去*)
            skip_inject=1
            if ! run_boot; then
              launch
              exit 1
            fi
            ;;
          *)
            if [ -n "$err" ]; then
              say "游戏开着，还在送进进程 $err"
            else
              say "游戏开着，还在送进进程"
            fi
            ;;
        esac
      fi
      sleep 0.2
      if hooked; then
        relay || say "钩子已在游戏里"
      fi
    fi
  fi
  if [ "$skip_inject" -eq 1 ] && [ "$i" -gt 80 ] && ! hooked; then
    say "库送进去了但没有回报，包装已撤，游戏可以自己打开"
    exit 1
  fi
  i=$((i + 1))
  sleep 0.4
done

if hooked; then
  relay || say "钩子已在游戏里"
  exit 0
fi
say "没能把钩子送进游戏"
exit 1
