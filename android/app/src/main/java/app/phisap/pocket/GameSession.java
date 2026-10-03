package app.phisap.pocket;

import android.app.Activity;
import android.app.Application;
import android.app.Instrumentation;
import android.content.Context;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.widget.FrameLayout;
import android.widget.TextView;

/**
 * 进了游戏进程之后才挂上。配置不在这里改，只留一个开始/停止。
 */
public final class GameSession implements PlanPlayer.Sink {
    private static GameSession live;
    private final Context context;
    private final Instrumentation instrumentation = new Instrumentation();
    private final Handler main = new Handler(Looper.getMainLooper());
    private Activity activity;
    private TextView pill;
    private Thread thread;
    private PlanPlayer player;

    private GameSession(Context context) {
        this.context = context.getApplicationContext();
    }

    public static void install(Application app) {
        if (live != null) {
            return;
        }
        live = new GameSession(app);
        try {
            app.getContentResolver().call(ModProvider.URI, "injected", null, null);
        } catch (Throwable ignored) {
        }
        app.registerActivityLifecycleCallbacks(new Application.ActivityLifecycleCallbacks() {
            @Override public void onActivityResumed(Activity activity) { live.attach(activity); }
            @Override public void onActivityCreated(Activity a, Bundle b) {}
            @Override public void onActivityStarted(Activity a) {}
            @Override public void onActivityPaused(Activity a) {}
            @Override public void onActivityStopped(Activity a) {}
            @Override public void onActivitySaveInstanceState(Activity a, Bundle b) {}
            @Override public void onActivityDestroyed(Activity a) {}
        });
        live.main.post(live::watchAppStart);
    }

    private long seenPlay;

    private void watchAppStart() {
        try {
            Bundle req = context.getContentResolver().call(ModProvider.URI, "playRequest", null, null);
            long at = req == null ? 0 : req.getLong("at");
            if (at > seenPlay && System.currentTimeMillis() - at < 4000 && (thread == null || !thread.isAlive())) {
                seenPlay = at;
                toggle();
            }
        } catch (Throwable ignored) {
        }
        main.postDelayed(this::watchAppStart, 250);
    }

    private void attach(Activity activity) {
        this.activity = activity;
        View decor = activity.getWindow().getDecorView();
        if (!(decor instanceof FrameLayout) || pill != null) {
            return;
        }
        TextView view = new TextView(activity);
        view.setText("phisap");
        view.setTextColor(Color.parseColor("#2A1020"));
        view.setTypeface(Typeface.DEFAULT_BOLD);
        view.setGravity(Gravity.CENTER);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.parseColor("#F26192"));
        bg.setCornerRadius(40f);
        view.setBackground(bg);
        view.setPadding(28, 16, 28, 16);
        view.setOnClickListener(v -> toggle());
        FrameLayout.LayoutParams lp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT);
        lp.gravity = Gravity.END | Gravity.CENTER_VERTICAL;
        lp.rightMargin = 12;
        ((FrameLayout) decor).addView(view, lp);
        pill = view;
    }

    private void toggle() {
        if (thread != null && thread.isAlive()) {
            stop();
            return;
        }
        Bundle plan = context.getContentResolver().call(ModProvider.URI, "plan", null, null);
        Bundle offset = context.getContentResolver().call(ModProvider.URI, "offset", null, null);
        String json = plan == null ? null : plan.getString("plan");
        int ms = offset == null ? 0 : offset.getInt("offset");
        if (json == null || json.isEmpty()) {
            setPill("无计划");
            return;
        }
        try {
            player = new PlanPlayer(DevicePlan.parse(json), ms, this);
        } catch (Exception e) {
            setPill("计划坏了");
            return;
        }
        thread = new Thread(player, "phisap-play");
        thread.start();
        setPill("停止");
    }

    public void playTestTap() {
        stop();
        player = new PlanPlayer(PlanPlayer.tapCenter(), 0, this);
        thread = new Thread(player, "phisap-test");
        thread.start();
    }

    private void stop() {
        if (player != null) {
            player.requestStop();
        }
        setPill("phisap");
    }

    private void setPill(String text) {
        main.post(() -> {
            if (pill != null) {
                pill.setText(text);
            }
        });
    }

    @Override
    public void inject(MotionEvent event) {
        Activity host = activity;
        if (host == null) {
            return;
        }
        try {
            instrumentation.sendPointerSync(event);
        } catch (Throwable ignored) {
            host.runOnUiThread(() -> host.dispatchTouchEvent(event));
        }
    }

    @Override
    public int[] screenSize() {
        Activity host = activity;
        if (host == null) {
            return new int[]{1280, 720};
        }
        View decor = host.getWindow().getDecorView();
        return new int[]{Math.max(1, decor.getWidth()), Math.max(1, decor.getHeight())};
    }
}
