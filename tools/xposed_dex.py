"""Small Xposed entry point used by the hand-built pocket APK.

The launcher APK is also an LSPosed module.  Its entry runs in the target app
process, copies the native payload from the module APK into the target app's
private files directory, marks it read-only, then calls System.load there.
No ptrace, process restart, or cross-process loader is used here.
"""
from __future__ import annotations

from dexlib import Asm, DexBuilder

PUB = 0x1
PRIV = 0x2
PROT = 0x4
STAT = 0x8
FINAL = 0x10
INIT = 0x10001

OBJ = 'Ljava/lang/Object;'
STR = 'Ljava/lang/String;'
CTX = 'Landroid/content/Context;'
CL = 'Ljava/lang/ClassLoader;'
APP = 'Landroid/app/Application;'
FILE = 'Ljava/io/File;'
FOS = 'Ljava/io/FileOutputStream;'
IN = 'Ljava/io/InputStream;'
ZIP = 'Ljava/util/zip/ZipFile;'
ENTRY = 'Ljava/util/zip/ZipEntry;'

HOOK = 'Lapp/phisap/pocket/HookEntry;'
ATTACH = 'Lapp/phisap/pocket/AttachHook;'
LOADER = 'Lapp/phisap/pocket/NativeLoader;'

IX_LOAD = 'Lde/robv/android/xposed/IXposedHookLoadPackage;'
IX_ZYGOTE = 'Lde/robv/android/xposed/IXposedHookZygoteInit;'
LP = 'Lde/robv/android/xposed/callbacks/XC_LoadPackage$LoadPackageParam;'
STARTUP = 'Lde/robv/android/xposed/IXposedHookZygoteInit$StartupParam;'
HELPERS = 'Lde/robv/android/xposed/XposedHelpers;'
XC_HOOK = 'Lde/robv/android/xposed/XC_MethodHook;'
HOOK_PARAM = 'Lde/robv/android/xposed/XC_MethodHook$MethodHookParam;'
UNHOOK = 'Lde/robv/android/xposed/XC_MethodHook$Unhook;'

TAG = 'PhiSAP-Xposed'


def M(cls: str, name: str, ret: str = 'V', args: tuple[str, ...] = ()) -> tuple:
    return (cls, name, (ret, tuple(args)))


def F(cls: str, name: str, typ: str) -> tuple:
    return (cls, name, typ)


def add_xposed_loader(dex: DexBuilder) -> None:
    """Add the real Xposed entry and the in-process System.load path."""
    dex.add_class(HOOK, OBJ, PUB | FINAL, interfaces=(IX_LOAD, IX_ZYGOTE))
    dex.add_field(HOOK, 'modulePath', STR, PRIV | STAT)

    init = Asm(1, 1)
    init.invoke('direct', M(OBJ, '<init>'), [0])
    init.ret()
    dex.add_method(HOOK, '<init>', 'V', (), INIT, init)

    iz = Asm(3, 2)  # this=v1, StartupParam=v2
    iz.iget(0, 2, F(STARTUP, 'modulePath', STR), 'object')
    iz.sput(0, F(HOOK, 'modulePath', STR), 'object')
    iz.ret()
    dex.add_method(HOOK, 'initZygote', 'V', (STARTUP,), PUB, iz)

    # Package and process names are both checked: loading into a service process
    # would make the native constructor reject the process anyway.
    target = Asm(6, 2)  # pkg=v4, process=v5
    target.const_string(0, 'com.PigeonGames.Phigros')
    target.invoke('virtual', M(STR, 'equals', 'Z', (OBJ,)), [0, 4], 1, 'int')
    target.if_nez(1, 'known')
    target.const_string(0, 'org.flos.phira')
    target.invoke('virtual', M(STR, 'equals', 'Z', (OBJ,)), [0, 4], 1, 'int')
    target.if_nez(1, 'known')
    target.const_string(0, 'org.flos.phira.modded')
    target.invoke('virtual', M(STR, 'equals', 'Z', (OBJ,)), [0, 4], 1, 'int')
    target.if_eqz(1, 'no')
    target.label('known')
    target.invoke('virtual', M(STR, 'equals', 'Z', (OBJ,)), [4, 5], 1, 'int')
    target.ret(1, 'int')
    target.label('no')
    target.const(1, 0)
    target.ret(1, 'int')
    dex.add_method(HOOK, 'targetProcess', 'Z', (STR, STR), PRIV | STAT, target)

    hp = Asm(12, 2)  # this=v10, LoadPackageParam=v11
    hp.label('try')
    hp.iget(0, 11, F(LP, 'packageName', STR), 'object')
    hp.iget(1, 11, F(LP, 'processName', STR), 'object')
    hp.invoke('static', M(HOOK, 'targetProcess', 'Z', (STR, STR)), [0, 1], 2, 'int')
    hp.if_eqz(2, 'end')
    hp.iget(3, 11, F(LP, 'classLoader', CL), 'object')

    # Xposed's string-based overload accepts a parameter class name, avoiding
    # a const-class dependency in the tiny hand-written DEX assembler.
    hp.const(4, 2)
    hp.new_array(5, 4, '[Ljava/lang/Object;')
    hp.const_string(6, 'android.content.Context')
    hp.const(7, 0)
    hp.aput(6, 5, 7, 'object')
    hp.new(6, ATTACH)
    hp.invoke('direct', M(ATTACH, '<init>'), [6])
    hp.const(7, 1)
    hp.aput(6, 5, 7, 'object')
    hp.const_string(8, 'android.app.Application')
    hp.const_string(9, 'attach')
    hp.invoke(
        'static', M(HELPERS, 'findAndHookMethod', UNHOOK, (STR, CL, STR, '[Ljava/lang/Object;')),
        [8, 3, 9, 5],
    )
    hp.label('end')
    hp.ret()
    hp.label('handler')
    hp.move_exception(0)
    hp.const_string(1, TAG)
    hp.const_string(2, 'Failed to hook Application.attach')
    hp.invoke('static', M('Landroid/util/Log;', 'e', 'I', (STR, STR, 'Ljava/lang/Throwable;')), [1, 2, 0], 3, 'int')
    hp.ret()
    hp.try_all('try', 'end', 'handler')
    dex.add_method(HOOK, 'handleLoadPackage', 'V', (LP,), PUB, hp)

    dex.add_class(ATTACH, XC_HOOK, PUB | FINAL)
    ah_init = Asm(1, 1)
    ah_init.invoke('direct', M(XC_HOOK, '<init>'), [0])
    ah_init.ret()
    dex.add_method(ATTACH, '<init>', 'V', (), INIT, ah_init)

    after = Asm(8, 2)  # this=v6, MethodHookParam=v7
    after.label('try')
    after.iget(0, 7, F(HOOK_PARAM, 'args', '[Ljava/lang/Object;'), 'object')
    after.const(1, 0)
    after.aget(2, 0, 1, 'object')
    after.check_cast(2, CTX)
    after.invoke('static', M(LOADER, 'load', 'V', (CTX,)), [2])
    after.label('end')
    after.ret()
    after.label('handler')
    after.move_exception(0)
    after.const_string(1, TAG)
    after.const_string(2, 'Application.attach callback failed')
    after.invoke('static', M('Landroid/util/Log;', 'e', 'I', (STR, STR, 'Ljava/lang/Throwable;')), [1, 2, 0], 3, 'int')
    after.ret()
    after.try_all('try', 'end', 'handler')
    dex.add_method(ATTACH, 'afterHookedMethod', 'V', (HOOK_PARAM,), PROT, after)

    dex.add_class(LOADER, OBJ, PUB | FINAL)
    dex.add_field(LOADER, 'attempted', 'I', PRIV | STAT)
    load = Asm(16, 1)  # Context=v15
    load.label('try')
    load.sget(0, F(LOADER, 'attempted', 'I'), 'int')
    load.if_nez(0, 'end')
    load.const(0, 1)
    load.sput(0, F(LOADER, 'attempted', 'I'), 'int')

    load.sget(0, F(HOOK, 'modulePath', STR), 'object')
    load.if_eqz(0, 'missing_path')
    load.new(1, ZIP)
    load.invoke('direct', M(ZIP, '<init>', 'V', (STR,)), [1, 0])
    load.const_string(2, 'assets/libphisap.so')
    load.invoke('virtual', M(ZIP, 'getEntry', ENTRY, (STR,)), [1, 2], 2)
    load.if_eqz(2, 'missing_asset')

    load.invoke('virtual', M(CTX, 'getFilesDir', FILE), [15], 3)
    load.const_string(4, 'phisap')
    load.new(5, FILE)
    load.invoke('direct', M(FILE, '<init>', 'V', (FILE, STR)), [5, 3, 4])
    load.invoke('virtual', M(FILE, 'mkdirs', 'Z'), [5], 6, 'int')

    load.const_string(4, 'libphisap.so.tmp')
    load.new(6, FILE)
    load.invoke('direct', M(FILE, '<init>', 'V', (FILE, STR)), [6, 5, 4])
    load.invoke('virtual', M(FILE, 'delete', 'Z'), [6], 10, 'int')
    load.new(7, FOS)
    load.invoke('direct', M(FOS, '<init>', 'V', (FILE,)), [7, 6])
    load.invoke('virtual', M(ZIP, 'getInputStream', IN, (ENTRY,)), [1, 2], 8)
    load.const(10, 8192)
    load.new_array(9, 10, '[B')

    load.label('copy')
    load.invoke('virtual', M(IN, 'read', 'I', ('[B',)), [8, 9], 10, 'int')
    load.if_lez(10, 'copied')
    load.const(11, 0)
    load.invoke('virtual', M(FOS, 'write', 'V', ('[B', 'I', 'I')), [7, 9, 11, 10])
    load.goto('copy')
    load.label('copied')
    load.invoke('virtual', M(IN, 'close'), [8])
    load.invoke('virtual', M(FOS, 'close'), [7])

    load.invoke('virtual', M(FILE, 'setReadOnly', 'Z'), [6], 10, 'int')
    load.if_eqz(10, 'readonly_failed')
    load.const_string(4, 'libphisap.so')
    load.new(5, FILE)
    load.invoke('direct', M(FILE, '<init>', 'V', (FILE, STR)), [5, 3, 4])
    load.invoke('virtual', M(FILE, 'delete', 'Z'), [5], 10, 'int')
    load.invoke('virtual', M(FILE, 'renameTo', 'Z', (FILE,)), [6, 5], 10, 'int')
    load.if_eqz(10, 'rename_failed')
    load.invoke('virtual', M(FILE, 'getAbsolutePath', STR), [5], 13)
    load.invoke('static', M('Ljava/lang/System;', 'load', 'V', (STR,)), [13])
    load.invoke('virtual', M(ZIP, 'close'), [1])
    load.const_string(12, TAG)
    load.const_string(13, 'System.load succeeded in target process')
    load.invoke('static', M('Landroid/util/Log;', 'i', 'I', (STR, STR)), [12, 13], 14, 'int')
    load.goto('end')

    load.label('missing_path')
    load.const_string(0, TAG)
    load.const_string(1, 'Xposed modulePath is missing; enable/reload the module')
    load.invoke('static', M('Landroid/util/Log;', 'e', 'I', (STR, STR)), [0, 1], 2, 'int')
    load.goto('end')
    load.label('missing_asset')
    load.invoke('virtual', M(ZIP, 'close'), [1])
    load.const_string(0, TAG)
    load.const_string(1, 'assets/libphisap.so is missing from the module APK')
    load.invoke('static', M('Landroid/util/Log;', 'e', 'I', (STR, STR)), [0, 1], 2, 'int')
    load.goto('end')
    load.label('readonly_failed')
    load.invoke('virtual', M(ZIP, 'close'), [1])
    load.const_string(0, TAG)
    load.const_string(1, 'Could not make the native library read-only')
    load.invoke('static', M('Landroid/util/Log;', 'e', 'I', (STR, STR)), [0, 1], 2, 'int')
    load.goto('end')
    load.label('rename_failed')
    load.invoke('virtual', M(ZIP, 'close'), [1])
    load.const_string(0, TAG)
    load.const_string(1, 'Could not install the native library in app files')
    load.invoke('static', M('Landroid/util/Log;', 'e', 'I', (STR, STR)), [0, 1], 2, 'int')
    load.goto('end')

    load.label('end')
    load.ret()
    load.label('handler')
    load.move_exception(0)
    load.const_string(1, TAG)
    load.const_string(2, 'System.load failed in target process')
    load.invoke('static', M('Landroid/util/Log;', 'e', 'I', (STR, STR, 'Ljava/lang/Throwable;')), [1, 2, 0], 3, 'int')
    load.ret()
    load.try_all('try', 'end', 'handler')
    dex.add_method(LOADER, 'load', 'V', (CTX,), PUB | STAT, load)
