package app.phisap.pocket;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;

/** 配置只有计划和偏移。演奏发生在游戏里，不在这个界面上假装已经点下去了。 */
public final class MainActivity extends Activity {
    private static final int PICK = 1;
    private PocketStore store;
    private TextView status;
    private TextView planName;
    private TextView planMeta;
    private TextView offsetValue;
    private int offset;

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
        status.setText(store.injectedRecently() ? R.string.status_live : R.string.status_waiting);
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
        getContentResolver().call(ModProvider.URI, "requestPlay", null, null);
        Toast.makeText(this, "Phigros 侧边按钮 4 秒内会开始。Phira 请用 root 真实触摸，进程内点不进去。", Toast.LENGTH_LONG).show();
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
