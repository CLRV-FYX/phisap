package app.phisap.pocket;

import android.content.Context;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/** Copies the APK payload to the target app's private dir, then loads it there. */
final class NativeLoader {
    private static final String TAG = "PhiSAP-Xposed";
    private static boolean attempted;

    private NativeLoader() {}

    static synchronized void load(Context target, String modulePath) {
        if (attempted) {
            return;
        }
        attempted = true;
        report(target, "attach-reached");
        ZipFile module = null;
        try {
            if (modulePath == null || modulePath.isEmpty()) {
                throw new IllegalStateException("LSPosed did not provide modulePath");
            }
            module = new ZipFile(modulePath);
            ZipEntry entry = module.getEntry("assets/libphisap.so");
            if (entry == null) {
                throw new IllegalStateException("assets/libphisap.so is missing from the APK");
            }

            File dir = new File(target.getFilesDir(), "phisap");
            if (!dir.isDirectory() && !dir.mkdirs()) {
                throw new IllegalStateException("cannot create " + dir);
            }
            report(target, "extracting-native");
            File temp = new File(dir, "libphisap.so.tmp");
            File dest = new File(dir, "libphisap.so");
            try (InputStream in = module.getInputStream(entry);
                 FileOutputStream out = new FileOutputStream(temp, false)) {
                byte[] buffer = new byte[8192];
                int count;
                while ((count = in.read(buffer)) != -1) {
                    out.write(buffer, 0, count);
                }
                out.getFD().sync();
            }
            // Required by newer Android releases before loading dynamic code.
            if (!temp.setReadOnly()) {
                throw new IllegalStateException("cannot mark native payload read-only");
            }
            if (dest.exists() && !dest.delete()) {
                throw new IllegalStateException("cannot replace old native payload");
            }
            if (!temp.renameTo(dest)) {
                throw new IllegalStateException("cannot install native payload");
            }
            report(target, "calling-System.load");
            System.load(dest.getAbsolutePath());
            report(target, "loaded");
            Log.i(TAG, "System.load succeeded in target process: " + dest);
        } catch (Throwable error) {
            report(target, "error: " + error);
            Log.e(TAG, "System.load failed in target process", error);
        } finally {
            if (module != null) {
                try {
                    module.close();
                } catch (Exception ignored) {
                }
            }
        }
    }

    private static void report(Context target, String status) {
        try {
            File dir = new File(target.getFilesDir(), "phisap");
            if (!dir.isDirectory() && !dir.mkdirs()) {
                return;
            }
            File statusFile = new File(dir, "xposed.status");
            try (FileOutputStream out = new FileOutputStream(statusFile, false)) {
                out.write(status.getBytes(java.nio.charset.StandardCharsets.UTF_8));
                out.getFD().sync();
            }
        } catch (Throwable ignored) {
        }
    }
}
