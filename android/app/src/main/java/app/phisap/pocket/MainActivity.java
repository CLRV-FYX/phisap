package app.phisap.pocket;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Point;
import android.net.Uri;
import android.os.Bundle;
import android.view.Display;
import android.view.WindowManager;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;

/** Configuration UI and the APK-bundled root-only game-process loader. */
public final class MainActivity extends Activity {
    private static final int PICK = 1;
    private static final String PHIGROS = "com.PigeonGames.Phigros";
    private static final String[] ROOT_PAYLOAD = {
            "inside.sh", "phisap-tapd", "phisap-inject", "libphisap.so"
    };

    private PocketStore store;
    private TextView status;
    private TextView planName;
    private TextView planMeta;
    private TextView offsetValue;
    private int offset;
    private volatile boolean starting;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        store = new PocketStore(this);
        status = findViewById(R.id.status);
        planName = findViewById(R.id.plan_name);
        planMeta = findViewById(R.id.plan_meta);
        offsetValue = findViewById(R.id.offset_value);
        offset = store.offset();
        showOffset();
        showPlan();

        findViewById(R.id.import_plan).setOnClickListener(v -> {
            Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
            intent.addCategory(Intent.CATEGORY_OPENABLE);
            intent.setType("*/*");
            startActivityForResult(intent, PICK);
        });
        findViewById(R.id.offset_minus).setOnClickListener(v -> bump(-5));
        findViewById(R.id.offset_plus).setOnClickListener(v -> bump(5));
        findViewById(R.id.start).setOnClickListener(v -> requestPlay(false));
        findViewById(R.id.test_tap).setOnClickListener(v -> requestPlay(true));
    }

    @Override
    protected void onResume() {
        super.onResume();
        showCurrentStatus();
    }

    private void bump(int delta) {
        offset += delta;
        store.setOffset(offset);
        showOffset();
    }

    private void showOffset() {
        offsetValue.setText((offset > 0 ? "+" : "") + offset);
    }

    private void showPlan() {
        try {
            String json = store.readPlan();
            if (json == null) {
                planName.setText(R.string.plan_empty);
                planMeta.setText(R.string.plan_hint);
                return;
            }
            DevicePlan plan = DevicePlan.parse(json);
            planName.setText(plan.name.isEmpty() ? "已导入" : plan.name);
            planMeta.setText(plan.events.size() + " 个事件 · 1280×720");
        } catch (Exception e) {
            planName.setText(R.string.plan_empty);
            planMeta.setText(e.getMessage());
        }
    }

    private void requestPlay(boolean test) {
        if (test) {
            try {
                store.savePlan("{\"format\":1,\"name\":\"测试点击\",\"width\":1280,\"height\":720,"
                        + "\"events\":[[0,0,1000,640,360],[40,1,1000,640,360]]}");
                showPlan();
            } catch (Exception e) {
                Toast.makeText(this, e.getMessage(), Toast.LENGTH_SHORT).show();
                return;
            }
        }
        if (starting) {
            return;
        }
        starting = true;
        status.setText("正在请求 root；请允许 SU 权限…");
        new Thread(this::runRootLoader, "phisap-root-loader").start();
    }

    private void runRootLoader() {
        File statusFile = new File(getFilesDir(), "status.txt");
        try {
            for (String name : ROOT_PAYLOAD) {
                copyAsset(name);
            }
            statusFile.delete();

            Point size = new Point();
            WindowManager manager = getWindowManager();
            Display display = manager.getDefaultDisplay();
            display.getRealSize(size);
            int rotation = display.getRotation();
            String files = getFilesDir().getAbsolutePath();
            String libDir = getApplicationInfo().nativeLibraryDir;
            String command = "sh " + quote(new File(getFilesDir(), "inside.sh").getAbsolutePath())
                    + " " + quote(files)
                    + " " + quote(libDir)
                    + " " + quote(PHIGROS)
                    + " " + Math.max(1, size.x)
                    + " " + Math.max(1, size.y)
                    + " " + rotation
                    + " > /dev/null 2>&1";

            Process root = Runtime.getRuntime().exec(new String[]{"su", "-c", command});
            while (!root.waitFor(500, TimeUnit.MILLISECONDS)) {
                showCurrentStatus();
            }
            int exitCode = root.exitValue();
            String finalStatus = readStatus(statusFile);
            if (finalStatus.isEmpty()) {
                finalStatus = exitCode == 0
                        ? "root 加载命令已完成，但没有状态回报"
                        : "root 请求失败（退出码 " + exitCode + "）；请允许 phisap 的 SU 权限";
            }
            showStatus(finalStatus);
        } catch (Throwable error) {
            showStatus("root 加载失败：" + error);
        } finally {
            starting = false;
        }
    }

    private void copyAsset(String name) throws Exception {
        File outFile = new File(getFilesDir(), name);
        try (InputStream in = getAssets().open(name);
             FileOutputStream out = new FileOutputStream(outFile, false)) {
            byte[] buffer = new byte[8192];
            int count;
            while ((count = in.read(buffer)) != -1) {
                out.write(buffer, 0, count);
            }
            out.getFD().sync();
        }
    }

    private void showCurrentStatus() {
        showStatus(readStatus(new File(getFilesDir(), "status.txt")));
    }

    private String readStatus(File file) {
        if (!file.isFile()) {
            return "";
        }
        try (InputStream in = new java.io.FileInputStream(file);
             ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[1024];
            int count;
            while ((count = in.read(buffer)) != -1) {
                out.write(buffer, 0, count);
            }
            return out.toString(StandardCharsets.UTF_8.name()).trim();
        } catch (Exception ignored) {
            return "";
        }
    }

    private void showStatus(String value) {
        if (value == null || value.isEmpty()) {
            value = getString(R.string.status_waiting);
        }
        final String message = value;
        runOnUiThread(() -> status.setText(message));
    }

    private static String quote(String value) {
        return "'" + value.replace("'", "'\\''") + "'";
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != PICK || resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        Uri uri = data.getData();
        try (InputStream in = getContentResolver().openInputStream(uri)) {
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] chunk = new byte[8192];
            int n;
            while ((n = in.read(chunk)) >= 0) {
                buf.write(chunk, 0, n);
            }
            String json = buf.toString(StandardCharsets.UTF_8.name());
            DevicePlan.parse(json);
            store.savePlan(json);
            showPlan();
        } catch (Exception e) {
            Toast.makeText(this, "这份计划读不了", Toast.LENGTH_SHORT).show();
        }
    }
}
