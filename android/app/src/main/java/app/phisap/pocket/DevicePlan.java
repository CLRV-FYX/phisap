package app.phisap.pocket;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/** 电脑版 device_plan.py 写出的格式。动作 0 按下、1 抬起、2 移动。 */
public final class DevicePlan {
    public final String name;
    public final List<Event> events;

    public DevicePlan(String name, List<Event> events) {
        this.name = name;
        this.events = events;
    }

    public static DevicePlan parse(String json) throws Exception {
        JSONObject obj = new JSONObject(json);
        if (obj.optInt("format") != 1) {
            throw new IllegalArgumentException("不认识的计划版本");
        }
        if (obj.optInt("width") != 1280 || obj.optInt("height") != 720) {
            throw new IllegalArgumentException("计划必须是 1280×720");
        }
        JSONArray rows = obj.getJSONArray("events");
        List<Event> events = new ArrayList<>(rows.length());
        for (int i = 0; i < rows.length(); i++) {
            JSONArray row = rows.getJSONArray(i);
            events.add(new Event(row.getInt(0), row.getInt(1), row.getInt(2), row.getInt(3), row.getInt(4)));
        }
        return new DevicePlan(obj.optString("name", ""), events);
    }

    public static final class Event {
        public final int ms;
        public final int action;
        public final int pointer;
        public final int x;
        public final int y;

        public Event(int ms, int action, int pointer, int x, int y) {
            this.ms = ms;
            this.action = action;
            this.pointer = pointer;
            this.x = x;
            this.y = y;
        }
    }
}
