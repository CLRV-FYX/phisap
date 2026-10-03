package app.phisap.pocket;

import android.app.Application;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedHelpers;

/**
 * LSPosed 和 NPatch 都认 assets/xposed_init 里的这个类。
 * 作用域只有 Phigros。模块加载进游戏进程后，才挂侧边的开始。
 */
public final class HookEntry implements IXposedHookLoadPackage {
    private static final String GAME = "com.PigeonGames.Phigros";

    @Override
    public void handleLoadPackage(LoadPackageParam lpparam) {
        if (!GAME.equals(lpparam.packageName)) {
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
