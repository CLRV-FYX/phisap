package app.phisap.pocket;

/** 和电脑版同一套黑边：计划是 1280×720，真机屏幕按短边等比铺，多出来的是黑边。 */
public final class ScreenMap {
    public final float scale;
    public final float x0;
    public final float y0;

    public ScreenMap(int screenW, int screenH) {
        scale = Math.min(screenW / 1280f, screenH / 720f);
        x0 = (screenW - 1280f * scale) / 2f;
        y0 = (screenH - 720f * scale) / 2f;
    }

    public float x(int planX) {
        return x0 + planX * scale;
    }

    public float y(int planY) {
        return y0 + planY * scale;
    }
}
