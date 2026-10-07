/* root 触摸守护。游戏里的钩子把点写到抽象套接字，这里用 uinput 发出去。
 * 坐标换算和电脑版注入器的 toDev 一致：先对上当前屏幕，再转成自然方向。
 */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <linux/input.h>
#include <linux/uinput.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <time.h>

#define MAGIC 0x31534850u
#define MAX_DISPLAY_SIZE 8000

struct Msg {
    uint32_t magic;
    int32_t action;
    int32_t slot;
    int32_t x;
    int32_t y;
};

static int ufd = -1;
static int fingers;
static int active_slot[10];
static int track = 1;
static int cur_w = 1080, cur_h = 2400, rot = 0;
static int uw, uh;
static int nat_w = 1080, nat_h = 2400;
static int dev_ready;

static void die(const char *s) {
    perror(s);
    exit(1);
}

static int cfg_int(const char *text, const char *key, int fallback) {
    char pat[32];
    snprintf(pat, sizeof pat, "%s=", key);
    const char *p = strstr(text, pat);
    if (!p) return fallback;
    return atoi(p + strlen(pat));
}

static void load_cfg(void) {
    FILE *f = fopen("/data/local/tmp/phisap-hook.cfg", "r");
    if (!f) return;
    char buf[512];
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = 0;
    int w = cfg_int(buf, "dw", cur_w);
    int h = cfg_int(buf, "dh", cur_h);
    int r = cfg_int(buf, "rot", rot);
    int a = cfg_int(buf, "uw", uw);
    int b = cfg_int(buf, "uh", uh);
    if (w > 100 && w <= MAX_DISPLAY_SIZE && h > 100 && h <= MAX_DISPLAY_SIZE) {
        cur_w = w;
        cur_h = h;
    }
    if (r >= 0 && r <= 3) rot = r;
    if (a > 100 && a <= MAX_DISPLAY_SIZE && b > 100 && b <= MAX_DISPLAY_SIZE) {
        uw = a;
        uh = b;
    }
}

static int parse_display(char *text, int *w, int *h, int *r) {
    char *cur = strstr(text, "cur=");
    if (!cur) return 0;
    cur += 4;
    int ww = 0, hh = 0;
    if (sscanf(cur, "%dx%d", &ww, &hh) != 2 ||
        ww < 100 || ww > MAX_DISPLAY_SIZE || hh < 100 || hh > MAX_DISPLAY_SIZE) return 0;
    *w = ww;
    *h = hh;
    int rr = *r;
    char *mr = strstr(text, "mRotation=");
    if (mr) {
        mr += 10;
        if (*mr >= '0' && *mr <= '3') rr = *mr - '0';
    }
    char *name = strstr(text, "ROTATION_");
    if (name) {
        name += 9;
        if (*name == '0') rr = 0;
        else if (*name == '9') rr = 1;
        else if (*name == '1') rr = 2;
        else if (*name == '2') rr = 3;
    }
    *r = rr;
    return 1;
}

static int refresh_display(void) {
    FILE *f = popen("/system/bin/dumpsys window", "r");
    if (!f) return 0;
    char *buf = malloc(256 * 1024);
    if (!buf) { pclose(f); return 0; }
    size_t n = fread(buf, 1, 256 * 1024 - 1, f);
    pclose(f);
    buf[n] = 0;
    int changed = 0;
    int w = cur_w, h = cur_h, r = rot;
    if (parse_display(buf, &w, &h, &r)) {
        changed = w != cur_w || h != cur_h || r != rot;
        cur_w = w;
        cur_h = h;
        rot = r;
    }
    free(buf);
    return changed;
}

static int emit(int type, int code, int value) {
    struct input_event ev;
    if (ufd < 0 || !dev_ready) return -1;
    memset(&ev, 0, sizeof ev);
    ev.type = (uint16_t)type;
    ev.code = (uint16_t)code;
    ev.value = value;
    if (write(ufd, &ev, sizeof ev) != (ssize_t)sizeof ev) {
        /* 设备失效时关闭 fd；后续按下会尝试重建。 */
        close(ufd);
        ufd = -1;
        dev_ready = 0;
        fingers = 0;
        memset(active_slot, 0, sizeof active_slot);
        return -1;
    }
    return 0;
}

static int setup_dev(void) {
    if (rot == 1 || rot == 3) { nat_w = cur_h; nat_h = cur_w; }
    else { nat_w = cur_w; nat_h = cur_h; }
    if (nat_w < 100) nat_w = 1080;
    if (nat_h < 100) nat_h = 2400;
    ufd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (ufd < 0) {
        perror("open /dev/uinput");
        return -1;
    }
    ioctl(ufd, UI_SET_EVBIT, EV_SYN);
    ioctl(ufd, UI_SET_EVBIT, EV_KEY);
    ioctl(ufd, UI_SET_EVBIT, EV_ABS);
    ioctl(ufd, UI_SET_KEYBIT, BTN_TOUCH);
    ioctl(ufd, UI_SET_KEYBIT, BTN_TOOL_FINGER);
    int axes[] = {ABS_MT_SLOT, ABS_MT_TOUCH_MAJOR, ABS_MT_POSITION_X, ABS_MT_POSITION_Y,
                  ABS_MT_TRACKING_ID, ABS_MT_PRESSURE, ABS_X, ABS_Y};
    for (unsigned i = 0; i < sizeof axes / sizeof axes[0]; i++) ioctl(ufd, UI_SET_ABSBIT, axes[i]);
    ioctl(ufd, UI_SET_PROPBIT, INPUT_PROP_DIRECT);
    struct uinput_user_dev dev;
    memset(&dev, 0, sizeof dev);
    snprintf(dev.name, sizeof dev.name, "phisap");
    dev.id.bustype = BUS_VIRTUAL;
    dev.absmax[ABS_MT_POSITION_X] = nat_w - 1;
    dev.absmax[ABS_MT_POSITION_Y] = nat_h - 1;
    dev.absmax[ABS_X] = nat_w - 1;
    dev.absmax[ABS_Y] = nat_h - 1;
    dev.absmax[ABS_MT_SLOT] = 9;
    dev.absmax[ABS_MT_TOUCH_MAJOR] = 255;
    dev.absmax[ABS_MT_PRESSURE] = 255;
    dev.absmax[ABS_MT_TRACKING_ID] = 65535;
    if (write(ufd, &dev, sizeof dev) != (ssize_t)sizeof dev) {
        perror("write uinput dev");
        close(ufd);
        ufd = -1;
        return -1;
    }
    if (ioctl(ufd, UI_DEV_CREATE) < 0) {
        perror("UI_DEV_CREATE");
        close(ufd);
        ufd = -1;
        return -1;
    }
    dev_ready = 1;
    fingers = 0;
    memset(active_slot, 0, sizeof active_slot);
    fprintf(stderr, "uinput %dx%d rot=%d cur=%dx%d\n", nat_w, nat_h, rot, cur_w, cur_h);
    return 0;
}

static void to_dev(int x, int y, int *ox, int *oy) {
    int sx = x, sy = y;
    if (uw > 0 && uh > 0) {
        sx = (int)((long)x * cur_w / uw);
        sy = (int)((long)y * cur_h / uh);
    }
    if (rot == 1) { *ox = cur_h - sy; *oy = sx; }
    else if (rot == 2) { *ox = cur_w - sx; *oy = cur_h - sy; }
    else if (rot == 3) { *ox = sy; *oy = cur_w - sx; }
    else { *ox = sx; *oy = sy; }
    if (*ox < 0) *ox = 0;
    if (*oy < 0) *oy = 0;
    if (*ox >= nat_w) *ox = nat_w - 1;
    if (*oy >= nat_h) *oy = nat_h - 1;
}

static void finger(int slot, int action, int x, int y) {
    if (!dev_ready || slot < 0 || slot >= 10) return;
    if (action == 1 && !active_slot[slot]) return;
    if (action == 2 && !active_slot[slot]) return;
    if (action < 0 || action > 2) return;
#define EMIT_EVENT(type, code, value) do { if (emit((type), (code), (value)) != 0) return; } while (0)

    EMIT_EVENT(EV_ABS, ABS_MT_SLOT, slot);
    if (action == 0) {
        int had_touch = fingers > 0;
        if (active_slot[slot]) {
            EMIT_EVENT(EV_ABS, ABS_MT_TRACKING_ID, -1);
            active_slot[slot] = 0;
            if (fingers > 0) fingers--;
        }
        int id = track++;
        if (track > 60000) track = 1;
        EMIT_EVENT(EV_ABS, ABS_MT_TRACKING_ID, id);
        EMIT_EVENT(EV_ABS, ABS_MT_POSITION_X, x);
        EMIT_EVENT(EV_ABS, ABS_MT_POSITION_Y, y);
        EMIT_EVENT(EV_ABS, ABS_MT_PRESSURE, 40);
        active_slot[slot] = 1;
        fingers++;
        if (!had_touch) {
            EMIT_EVENT(EV_KEY, BTN_TOUCH, 1);
            EMIT_EVENT(EV_KEY, BTN_TOOL_FINGER, 1);
        }
    } else if (action == 1) {
        EMIT_EVENT(EV_ABS, ABS_MT_TRACKING_ID, -1);
        active_slot[slot] = 0;
        if (fingers > 0) fingers--;
        if (fingers == 0) {
            EMIT_EVENT(EV_KEY, BTN_TOUCH, 0);
            EMIT_EVENT(EV_KEY, BTN_TOOL_FINGER, 0);
        }
    } else {
        EMIT_EVENT(EV_ABS, ABS_MT_POSITION_X, x);
        EMIT_EVENT(EV_ABS, ABS_MT_POSITION_Y, y);
    }
    EMIT_EVENT(EV_SYN, SYN_REPORT, 0);
#undef EMIT_EVENT
}

static void lift_all(void) {
    if (!dev_ready) return;
    for (int s = 0; s < 10; s++) {
        if (active_slot[s]) finger(s, 1, 0, 0);
    }
    fingers = 0;
}

static void destroy_dev(void) {
    if (ufd >= 0) {
        if (dev_ready) ioctl(ufd, UI_DEV_DESTROY);
        close(ufd);
    }
    ufd = -1;
    dev_ready = 0;
    fingers = 0;
    memset(active_slot, 0, sizeof active_slot);
}

struct MsgStream {
    unsigned char bytes[sizeof(struct Msg)];
    size_t used;
};

static int handle_msg(const struct Msg *m) {
    if (m->magic != MAGIC) return -1;
    if (m->action == 8) {
        if (m->x < 100 || m->x > MAX_DISPLAY_SIZE ||
            m->y < 100 || m->y > MAX_DISPLAY_SIZE) return -1;
        uw = m->x;
        uh = m->y;
        return 0;
    }
    if (m->action < 0 || m->action > 2 || m->slot < 0 || m->slot >= 10) return -1;
    if (m->action != 1 && (m->x < -100000 || m->x > 100000 ||
                           m->y < -100000 || m->y > 100000)) return -1;
    if (m->action == 0 && !dev_ready) {
        refresh_display();
        if (setup_dev() != 0) return 0;
    }
    if (!dev_ready) return 0;
    int ox = 0, oy = 0;
    if (m->action != 1) to_dev(m->x, m->y, &ox, &oy);
    finger(m->slot, m->action, ox, oy);
    return 0;
}

static int feed_messages(struct MsgStream *stream, const unsigned char *bytes, size_t n) {
    while (n) {
        size_t need = sizeof(struct Msg) - stream->used;
        size_t take = n < need ? n : need;
        memcpy(stream->bytes + stream->used, bytes, take);
        stream->used += take;
        bytes += take;
        n -= take;
        if (stream->used == sizeof(struct Msg)) {
            struct Msg m;
            memcpy(&m, stream->bytes, sizeof m);
            stream->used = 0;
            if (handle_msg(&m) != 0) return -1;
        }
    }
    return 0;
}

static void disconnect_client(int *client, struct MsgStream *stream) {
    if (*client >= 0) close(*client);
    *client = -1;
    stream->used = 0;
    lift_all();
}

static long long monotonic_ms(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static volatile int running = 1;
static void on_stop(int sig) { (void)sig; running = 0; }

int main(void) {
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, on_stop);
    signal(SIGINT, on_stop);
    load_cfg();
    refresh_display();
    int srv = socket(AF_UNIX, SOCK_STREAM, 0);
    if (srv < 0) die("socket");
    struct sockaddr_un addr;
    memset(&addr, 0, sizeof addr);
    addr.sun_family = AF_UNIX;
    addr.sun_path[0] = 0;
    memcpy(addr.sun_path + 1, "phisap", 6);
    if (bind(srv, (struct sockaddr *)&addr, sizeof(sa_family_t) + 7) < 0) die("bind");
    if (listen(srv, 4) < 0) die("listen");
    fprintf(stderr, "phisap-tapd ready\n");
    int client = -1;
    struct MsgStream stream = {{0}, 0};
    long long refresh_at = monotonic_ms() + 1000;
    while (running) {
        if (access("/data/local/tmp/phisap-stop", F_OK) == 0) break;
        struct pollfd fds[2];
        int nfd = 0;
        int polled_client = client;
        fds[nfd].fd = srv;
        fds[nfd].events = POLLIN;
        nfd++;
        if (polled_client >= 0) {
            fds[nfd].fd = polled_client;
            fds[nfd].events = POLLIN;
            nfd++;
        }
        int pr = poll(fds, nfd, 200);
        if (pr < 0) {
            if (errno == EINTR) continue;
            break;
        }

        long long now = monotonic_ms();
        if (now >= refresh_at) {
            int changed = refresh_display();
            refresh_at = monotonic_ms() + 1000;
            if (changed && dev_ready) {
                lift_all();
                destroy_dev();
            }
        }

        int accepted = 0;
        if (fds[0].revents & POLLIN) {
            int fd = accept(srv, 0, 0);
            if (fd >= 0) {
                disconnect_client(&client, &stream);
                client = fd;
                accepted = 1;
            }
        }

        // Don't read a newly accepted fd using readiness reported for the old fd.
        if (!accepted && polled_client >= 0 && client == polled_client && nfd == 2 &&
            (fds[1].revents & (POLLIN | POLLHUP | POLLERR | POLLNVAL))) {
            if (fds[1].revents & POLLNVAL) {
                disconnect_client(&client, &stream);
                continue;
            }
            unsigned char tmp[256];
            ssize_t n = read(polled_client, tmp, sizeof tmp);
            if (n == 0) {
                disconnect_client(&client, &stream);
                continue;
            }
            if (n < 0) {
                if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
                disconnect_client(&client, &stream);
                continue;
            }
            if (feed_messages(&stream, tmp, (size_t)n) != 0) {
                disconnect_client(&client, &stream);
            }
        }
    }
    disconnect_client(&client, &stream);
    destroy_dev();
    close(srv);
    unlink("/data/local/tmp/phisap-stop");
    return 0;
}
