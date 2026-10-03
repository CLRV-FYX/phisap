package app.phisap.pocket;

import android.os.SystemClock;
import android.view.MotionEvent;

import java.util.List;
import java.util.concurrent.atomic.AtomicBoolean;

/** 按计划时刻注入。正偏移延后，负偏移提前。和电脑版的旋钮同一个方向。 */
public final class PlanPlayer implements Runnable {
    public interface Sink {
        void inject(MotionEvent event);
        int[] screenSize();
    }

    private final DevicePlan plan;
    private final int offsetMs;
    private final Sink sink;
    private final AtomicBoolean stop = new AtomicBoolean(false);
    private final TouchTrack track = new TouchTrack();

    public PlanPlayer(DevicePlan plan, int offsetMs, Sink sink) {
        this.plan = plan;
        this.offsetMs = offsetMs;
        this.sink = sink;
    }

    public void requestStop() {
        stop.set(true);
    }

    @Override
    public void run() {
        if (plan.events.isEmpty()) {
            return;
        }
        int origin = plan.events.get(0).ms;
        long t0 = SystemClock.uptimeMillis();
        for (DevicePlan.Event event : plan.events) {
            if (stop.get()) {
                break;
            }
            long due = t0 + (event.ms - origin) + offsetMs;
            long wait = due - SystemClock.uptimeMillis();
            if (wait > 3) {
                try {
                    Thread.sleep(wait - 2);
                } catch (InterruptedException ignored) {
                    break;
                }
            }
            while (!stop.get() && SystemClock.uptimeMillis() < due) {
                Thread.yield();
            }
            if (stop.get()) {
                break;
            }
            int[] size = sink.screenSize();
            ScreenMap map = new ScreenMap(size[0], size[1]);
            MotionEvent motion = track.build(
                    event.action, event.pointer, map.x(event.x), map.y(event.y), SystemClock.uptimeMillis());
            if (motion != null) {
                sink.inject(motion);
                motion.recycle();
            }
        }
        track.clear();
    }

    public static DevicePlan tapCenter() {
        List<DevicePlan.Event> events = java.util.Arrays.asList(
                new DevicePlan.Event(0, TouchTrack.DOWN, 1000, 640, 360),
                new DevicePlan.Event(40, TouchTrack.UP, 1000, 640, 360));
        return new DevicePlan("测试", events);
    }
}
