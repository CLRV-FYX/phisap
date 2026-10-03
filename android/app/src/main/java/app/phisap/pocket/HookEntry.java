package app.phisap.pocket;

import android.app.Application;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedHelpers;

/**
 * LSPosed 和 NPatch 都认 assets/xposed_init 里的这个类。
 * 进程内的侧边按钮只对 Phigros 有用。Phira 是 Rust，点不进画布，
 * 播放走本应用的 root 真实触摸，这里不要往它的界面上挂。
 */
public final class HookEntry implements IXposedHookLoadPackage {
    private static final String GAME = "com.PigeonGames.Phigros";

    @Override
    public void handleLoadPackage(LoadPackageParam lpparam) {
        String pkg = lpparam.packageName;
        if ("org.flos.phira".equals(pkg) || "org.flos.phira.modded".equals(pkg)) {
            return;
        }
        if (!GAME.equals(pkg)) {
            return;
        }
        XposedHelpers.findAndHookMethod(
                "android.app.Application", lpparam.classLoader, "onCreate",
                new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        GameSession.install((Application) param.thisObject);
                    }
                });
    }
}
