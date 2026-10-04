#!/system/bin/sh
# 方式二：写 wrap（LD_PRELOAD），拉起触摸守护，再把钩子送进已经在跑的游戏。
# 正式版游戏不会走 wrap，所以后面还有 ptrace 注入。两条路加载的是同一个 so。
FILES="$1"
LIBDIR="$2"
PKG="$3"
DW="$4"
DH="$5"
ROT="$6"
D=/data/local/tmp

if [ -z "$PKG" ]; then
  echo "没有包名"
  exit 1
fi

mkdir -p "$D" /sdcard/phisap 2>/dev/null
rm -f "$D/phisap-stop"

# 优先用安装时解出来的库目录。有的手机 /data/local/tmp 不允许执行，dlopen 也会失败。
SO="$LIBDIR/libphisap.so"
TAP="$LIBDIR/libphisap-tapd.so"
INJ="$LIBDIR/libphisap-inject.so"
if [ ! -f "$SO" ]; then
  if [ -f "$FILES/libphisap.so" ]; then
    cp -f "$FILES/libphisap.so" "$D/libphisap.so"
    SO="$D/libphisap.so"
  else
    echo "没有 libphisap.so"
    exit 1
  fi
fi
if [ ! -f "$TAP" ]; then
  cp -f "$FILES/phisap-tapd" "$D/phisap-tapd" || { echo "没有触摸守护"; exit 1; }
  TAP="$D/phisap-tapd"
fi
if [ ! -f "$INJ" ]; then
  cp -f "$FILES/phisap-inject" "$D/phisap-inject" || { echo "没有注入器"; exit 1; }
  INJ="$D/phisap-inject"
fi
chmod 755 "$SO" "$TAP" "$INJ" 2>/dev/null || true
cp -f "$SO" "$D/libphisap.so" 2>/dev/null || true

printf 'dw=%s\ndh=%s\nrot=%s\nuw=0\nuh=0\n' "$DW" "$DH" "$ROT" > "$D/phisap-hook.cfg"
chmod 666 "$D/phisap-hook.cfg"
: > "$D/phisap-hook.log"
chmod 666 "$D/phisap-hook.log"
echo "等 il2cpp" > "$D/phisap-status"
chmod 666 "$D/phisap-status"

cat > "$D/phisap-wrap.sh" << END
#!/system/bin/sh
export LD_PRELOAD=$SO
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
if [ "$OLD" = "Enforcing" ]; then
  setenforce 0 2>/dev/null || true
fi

if ! pm path "$PKG" >/dev/null 2>&1; then
  echo "没找到 $PKG"
  exit 1
fi

pids() {
  pidof "$1" 2>/dev/null || true
}

if [ -z "$(pids "$PKG")" ]; then
  am start -a android.intent.action.MAIN -c android.intent.category.LAUNCHER -p "$PKG" >/dev/null 2>&1 || true
fi

i=0
ok=0
while [ "$i" -lt 40 ]; do
  PIDS=$(pids "$PKG")
  if [ -n "$PIDS" ]; then
    sleep 0.3
    for PID in $PIDS; do
      if "$D/phisap-inject" "$PID" "$SO" 2>>"$D/phisap-tapd.log" || "$INJ" "$PID" "$SO"; then
        echo "已注入 $PKG pid=$PID"
        ok=1
      else
        echo "注入 $PID 失败"
      fi
    done
    if [ "$ok" = 1 ]; then
      break
    fi
  fi
  i=$((i + 1))
  sleep 0.25
done

if [ "$ok" != 1 ]; then
  echo "没能把钩子送进 $PKG"
  exit 1
fi
sleep 0.4
if [ -s "$D/phisap-status" ]; then
  cat "$D/phisap-status"
fi
