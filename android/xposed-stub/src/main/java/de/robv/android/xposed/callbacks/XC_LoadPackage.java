package de.robv.android.xposed.callbacks;

/** Compile-time shape of the legacy Xposed callback parameter. */
public final class XC_LoadPackage {
    private XC_LoadPackage() {}

    public static class LoadPackageParam {
        public String packageName;
        public String processName;
        public ClassLoader classLoader;
    }
}
