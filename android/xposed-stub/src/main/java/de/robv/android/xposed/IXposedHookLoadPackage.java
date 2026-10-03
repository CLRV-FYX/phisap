package de.robv.android.xposed;

/**
 * 编译期桩。运行时由 LSPosed / NPatch 提供同名类，这个模块不能打进 APK。
 */
public interface IXposedHookLoadPackage {
    void handleLoadPackage(LoadPackageParam lpparam) throws Throwable;

    class LoadPackageParam {
        public String packageName;
        public String processName;
        public ClassLoader classLoader;
    }
}
