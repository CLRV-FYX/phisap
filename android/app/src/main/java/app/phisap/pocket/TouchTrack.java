package app.phisap.pocket;

import android.view.InputDevice;
import android.view.MotionEvent;

import java.util.LinkedHashMap;
import java.util.Map;

/** 把计划里的按下/移动/抬起收成 Android 能认的多指事件。 */
public final class TouchTrack {
    public static final int DOWN = 0;
    public static final int UP = 1;
    public static final int MOVE = 2;

    private final LinkedHashMap<Integer, float[]> down = new LinkedHashMap<>();

    public MotionEvent build(int action, int pointer, float x, float y, long when) {
        if (action == DOWN) {
            down.put(pointer, new float[]{x, y});
        } else if (!down.containsKey(pointer)) {
            return null;
        } else {
            down.put(pointer, new float[]{x, y});
        }
        int n = down.size();
        MotionEvent.PointerProperties[] props = new MotionEvent.PointerProperties[n];
        MotionEvent.PointerCoords[] coords = new MotionEvent.PointerCoords[n];
        int index = 0;
        int i = 0;
        for (Map.Entry<Integer, float[]> e : down.entrySet()) {
            MotionEvent.PointerProperties prop = new MotionEvent.PointerProperties();
            prop.id = e.getKey();
            prop.toolType = MotionEvent.TOOL_TYPE_FINGER;
            props[i] = prop;
            MotionEvent.PointerCoords coord = new MotionEvent.PointerCoords();
            coord.x = e.getValue()[0];
            coord.y = e.getValue()[1];
            coord.pressure = 1f;
            coords[i] = coord;
            if (e.getKey() == pointer) {
                index = i;
            }
            i++;
        }
        int masked;
        if (action == DOWN) {
            masked = n == 1 ? MotionEvent.ACTION_DOWN : MotionEvent.ACTION_POINTER_DOWN;
        } else if (action == UP) {
            masked = n == 1 ? MotionEvent.ACTION_UP : MotionEvent.ACTION_POINTER_UP;
        } else {
            masked = MotionEvent.ACTION_MOVE;
        }
        MotionEvent ev = MotionEvent.obtain(
                when, when, masked | (index << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                n, props, coords, 0, 0, 1f, 1f, 0, 0,
                InputDevice.SOURCE_TOUCHSCREEN, 0);
        if (action == UP) {
            down.remove(pointer);
        }
        return ev;
    }

    public void clear() {
        down.clear();
    }
}
