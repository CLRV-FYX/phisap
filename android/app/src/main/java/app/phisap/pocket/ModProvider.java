package app.phisap.pocket;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;

/**
 * 只接受本模块和 Phigros。配置从应用进程写进来，游戏进程读出去。
 * 这是单 APK 能跨进程的原因，不是把文件放到公共目录。
 */
public final class ModProvider extends ContentProvider {
    public static final String AUTH = "app.phisap.pocket.modprovider";
    public static final Uri URI = Uri.parse("content://" + AUTH);
    private static final String GAME = "com.PigeonGames.Phigros";

    @Override
    public boolean onCreate() {
        return true;
    }

    @Override
    public Bundle call(String method, String arg, Bundle extras) {
        if (!allowed()) {
            return null;
        }
        PocketStore store = new PocketStore(getContext());
        Bundle out = new Bundle();
        if ("injected".equals(method)) {
            store.markInjected();
            out.putBoolean("ok", true);
            return out;
        }
        if ("offset".equals(method)) {
            out.putInt("offset", store.offset());
            return out;
        }
        if ("plan".equals(method)) {
            try {
                out.putString("plan", store.readPlan());
            } catch (Exception e) {
                out.putString("plan", null);
            }
            return out;
        }
        if ("requestPlay".equals(method)) {
            store.requestPlay();
            out.putBoolean("ok", true);
            return out;
        }
        if ("playRequest".equals(method)) {
            out.putLong("at", store.playRequest());
            return out;
        }
        return null;
    }

    private boolean allowed() {
        String caller = getCallingPackage();
        if (caller == null) {
            return true;
        }
        return GAME.equals(caller) || "app.phisap.pocket".equals(caller);
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public String getType(Uri uri) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
        return 0;
    }
}
