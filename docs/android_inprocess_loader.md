# Android root-only game-process loader

The release pocket APK (`android/phisap-pocket.apk`, built by
`tools/build_apk.py`) is self-contained. After the user grants SU/root, its
bundled ARM64 `phisap-inject` helper attaches to the selected running Phigros
process and calls the target process's dynamic linker to load the bundled
`libphisap.so`. If Phigros is not running, the helper launches it and waits for
`libil2cpp.so` before injecting. If Phigros is already open, it does not force
stop or restart it. The APK also bundles and starts its root touch daemon.

This release path does **not** install, enable, or configure LSPosed, Zygisk,
NPatch, or a separate module. It confirms the library mapping in
`/proc/<pid>/maps`, then relays the native hook's bounded startup status to the
app. A missing mapping, failed ptrace, linker error, or hook initialization
failure is reported as an error; the app does not treat process detection as a
successful load or wait forever. Native IL2CPP API discovery is capped at 15
seconds and `JudgeLineControl` lookup at 10 seconds, inside the launcher's
30-second status deadline. Hard failures remain visible and direct the user to
restart the game before retrying rather than silently repeating the wait. Phira
uses the root touch daemon and does not use the Phigros-specific IL2CPP hook.

The packaged injector exposes only the direct `<pid> <so-path>` entry point;
the native build strips the old boot-mode code and rejects Zygisk strings in the
payload. It does not set a persistent launch property or modify system mounts.
If SELinux blocks the initial attach, the helper makes one best-effort retry
with SELinux temporarily permissive and restores the original Enforcing state
on every exit path. Version 3.7 performs a one-time migration from old saved
settings: it selects the in-process path and replaces old `System.load` status
text with a visible `PhiSAP Root 3.7` marker.

## Build and verification

```sh
python tools/build_apk.py
python -m unittest tests.test_root_loader tests.test_inprocess_native
```

Install the Python requirements first; APK signing requires `cryptography`.
A Zig 0.13-compatible compiler (`zig cc`, via `PATH` or `ZIG`) is needed to
rebuild the ARM64 hook, touch daemon, ioctl bridge, and root injector from source.
The builder uses the checked-in `aapt2` and fetches the Android platform jar if
it is missing. The output is `android/phisap-pocket.apk` (version code 29 / 3.7).
The builder includes all native payloads in the same signed APK, audits the
hand-built DEX, inspects the manifest and payload, and verifies v1/v2 signatures.
Static checks validate the root loading path and shell syntax; they do not prove
runtime injection on a device.

## Device verification

After granting root and pressing Start, the UI must first report that
`libphisap.so` is mapped in the running Phigros PID, then report a native hook
status such as `已挂钩 UpdateInfo`. On a rooted device this can be independently
checked with:

```sh
adb shell su -c 'for p in $(pidof com.PigeonGames.Phigros); do grep libphisap.so /proc/$p/maps; done'
adb shell su -c 'cat /data/local/tmp/phisap-status; tail -n 40 /data/local/tmp/phisap-hook.log; cat /data/local/tmp/phisap-inject.err'
```

No Android device is available in this workspace, so runtime injection and
actual in-game hook behavior remain unverified here. The APK build and static
regression tests must not be described as a device-level success test.
