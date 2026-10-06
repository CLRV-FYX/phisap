package de.robv.android.xposed;

/** Compile-time API shape only. LSPosed provides the runtime implementation. */
public interface IXposedHookZygoteInit {
    void initZygote(StartupParam startupParam) throws Throwable;

    class StartupParam {
        public String modulePath;
        public boolean startsSystemServer;
    }
}
