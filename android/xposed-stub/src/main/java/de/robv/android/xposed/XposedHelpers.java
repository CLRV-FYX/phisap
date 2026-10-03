package de.robv.android.xposed;

public final class XposedHelpers {
    private XposedHelpers() {}

    public static XC_MethodHook.Unhook findAndHookMethod(
            String className, ClassLoader loader, String methodName, Object... parameterTypesAndCallback) {
        throw new UnsupportedOperationException("compile stub");
    }
}
