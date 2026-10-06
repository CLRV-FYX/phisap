package app.phisap.pocket;

import android.content.Context;
import android.content.SharedPreferences;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;

/** 计划文件和偏移。游戏进程通过 ModProvider 来读，不直接打开私有目录。 */
public final class PocketStore {
    private static final String PREFS = "pocket";
    private static final String OFFSET = "offset_ms";
    private static final String PLAN = "plan.json";
    private static final String INJECTED_AT = "injected_at";
    private static final String PLAY_AT = "play_at";

    private final Context context;

    public PocketStore(Context context) {
        this.context = context.getApplicationContext();
    }

    public void savePlan(String json) throws Exception {
        File file = new File(context.getFilesDir(), PLAN);
        try (FileOutputStream out = new FileOutputStream(file)) {
            out.write(json.getBytes(StandardCharsets.UTF_8));
        }
    }

    public String readPlan() throws Exception {
        File file = new File(context.getFilesDir(), PLAN);
        if (!file.isFile()) {
            return null;
        }
        byte[] buf = new byte[(int) file.length()];
        try (FileInputStream in = new FileInputStream(file)) {
            int n = 0;
            while (n < buf.length) {
                int got = in.read(buf, n, buf.length - n);
                if (got < 0) {
                    break;
                }
                n += got;
            }
        }
        return new String(buf, StandardCharsets.UTF_8);
    }

    public void setOffset(int ms) {
        prefs().edit().putInt(OFFSET, ms).apply();
    }

    public int offset() {
        return prefs().getInt(OFFSET, 0);
    }

    public void markInjected() {
        prefs().edit().putLong(INJECTED_AT, System.currentTimeMillis()).apply();
    }

    public boolean injectedRecently() {
        long at = prefs().getLong(INJECTED_AT, 0);
        return at > 0 && System.currentTimeMillis() - at < 30 * 60 * 1000L;
    }

    public void requestPlay() {
        prefs().edit().putLong(PLAY_AT, System.currentTimeMillis()).apply();
    }

    public long playRequest() {
        return prefs().getLong(PLAY_AT, 0);
    }

    private SharedPreferences prefs() {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
