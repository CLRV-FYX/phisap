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

WRAPSO="$SO"
if [ -n "$GAMESO" ]; then
  WRAPSO="$GAMESO"
fi
cat > "$D/phisap-wrap.sh" << END
#!/system/bin/sh
export LD_PRELOAD=$WRAPSO
exec "\$@"
END
chmod 755 "$D/phisap-wrap.sh"
setprop "wrap.$PKG" "$D/phisap-wrap.sh"

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
trap restore EXIT

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
  am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p "$PKG" >/dev/null 2>&1 || true
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
        restore
        say "已送进，等钩子回报"
        return 0
      }
    done
  done
  restore
  if [ -n "$err" ]; then
    say "注入失败 $err"
  else
    say "注入失败"
  fi
  return 1
}

launch
i=0
last=-8
saw_pid=0
restarted=0
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
    say "正在等游戏进程"
    if [ $((i % 6)) -eq 0 ]; then
      launch
    fi
  else
    saw_pid=1
    if [ $((i - last)) -ge 6 ]; then
      last=$i
      do_inject "$pids" || true
      sleep 0.3
      if hooked; then
        relay || say "钩子已在游戏里"
      fi
    fi
  fi
  if [ "$i" -eq 36 ] && [ "$restarted" = 0 ] && [ "$saw_pid" = 1 ] && ! hooked; then
    restarted=1
    last=-8
    say "注入没进去，重启游戏再送一次"
    am force-stop "$PKG" >/dev/null 2>&1 || true
    sleep 0.4
    launch
  fi
  i=$((i + 1))
  sleep 0.5
done

if hooked; then
  relay || say "钩子已在游戏里"
  exit 0
fi
say "没能把钩子送进游戏"
exit 1
