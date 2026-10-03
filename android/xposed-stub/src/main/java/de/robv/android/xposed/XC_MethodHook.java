package de.robv.android.xposed;

public abstract class XC_MethodHook {
    protected void beforeHookedMethod(MethodHookParam param) {}

    protected void afterHookedMethod(MethodHookParam param) {}

    public static class MethodHookParam {
        public Object thisObject;
        public Object[] args;
    }

    public static class Unhook {}
}
