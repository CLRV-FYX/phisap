package app.phisap.pocket;

import android.app.Application;
import android.content.Context;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.IXposedHookZygoteInit;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

/**
 * LSPosed entry for the same APK that provides the pocket UI. Native code is
 * loaded from inside the target process before Application.onCreate.
 */
public final class HookEntry implements IXposedHookLoadPackage, IXposedHookZygoteInit {
    private static final String PHIGROS = "com.PigeonGames.Phigros";
    private static final String PHIRA = "org.flos.phira";
    private static final String PHIRA_MODDED = "org.flos.phira.modded";
    private static volatile String modulePath;

    @Override
    public void initZygote(StartupParam startupParam) {
        modulePath = startupParam.modulePath;
    }

    @Override
    public void handleLoadPackage(XC_LoadPackage.LoadPackageParam lpparam) {
        String pkg = lpparam.packageName;
        if (!(PHIGROS.equals(pkg) || PHIRA.equals(pkg) || PHIRA_MODDED.equals(pkg))) {
            return;
        }
        if (!pkg.equals(lpparam.processName)) {
            return;
        }

        XposedHelpers.findAndHookMethod(
                "android.app.Application", lpparam.classLoader, "attach", "android.content.Context",
                new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        NativeLoader.load((Context) param.args[0], modulePath);
                    }
                });
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
