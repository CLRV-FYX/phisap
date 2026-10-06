# Android in-process native loading

The hand-built pocket APK (`tools/build_apk.py`) is also an LSPosed module. Its
`assets/xposed_init` entry hooks the target app's `Application.attach` callback.
Inside that app process it copies `assets/libphisap.so` from the module APK into
the target app's private `files/phisap` directory, marks the file read-only, and
calls `System.load` with the absolute path. The root helper only prepares the
hook configuration/touch daemon, launches the game if it is not already running,
and checks `/proc/<game-pid>/maps` for `libphisap.so`.

This path does not use the old `phisap-inject`/ptrace loader. A process that was
already running before LSPosed loaded the module cannot be retroactively hooked.
The helper deliberately reports that case as **not injected**; it does not kill
or silently restart the game.

## Build

```sh
python tools/build_apk.py
```

Install the Python requirements first; APK signing requires `cryptography`.
The output is `android/phisap-pocket.apk` (version code 26 / version 3.4). The
custom DEX is audited, the manifest is inspected with `aapt2`, and the APK's v1
and v2 signatures are checked by the build script. This is a static build check,
not a device injection test.

## Device setup and use

1. Install/update `android/phisap-pocket.apk` and have a compatible LSPosed
   manager/framework active.
2. In LSPosed, enable **PhiSAP** and manually add the target game package to its
   scope. The hand-built APK omits the optional `xposedscope` resource so its
   fixed UI resource IDs do not shift.
3. If the game is already open, force-stop it yourself. LSPosed hooks processes
   when they start; pressing Start while an unhooked process is alive will show
   an explicit failure and will not claim success.
4. Open PhiSAP and press Start. The helper prepares shared config first, then
   launches the game. It reports loaded only after the game process maps
   `libphisap.so`.

For independent verification on a rooted device:

```sh
adb shell su -c 'for p in $(pidof com.PigeonGames.Phigros); do grep libphisap.so /proc/$p/maps; done'
adb logcat -s PhiSAP-Xposed
```

A missing maps line means the native library is not mapped in that process. Check
that LSPosed enabled the module and the game's scope, then check the tagged
logcat output. No device was available to verify runtime injection while this
APK was being built.
