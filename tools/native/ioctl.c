/* 给 app_process 里的 Injector.nioctl 用。不链接 libc：zig 0.16 没有 Android bionic。
 * ioctl 走 aarch64 syscall 29，第三参按已经能用的 tapd.c 传整数值，不走 android.system.Os。
 */
typedef const struct JNINativeInterface_ *JNIEnv;
typedef void *jobject;
typedef void *jclass;
typedef int jint;

typedef jclass (*get_class_fn)(JNIEnv *, jobject);
typedef void *(*get_field_fn)(JNIEnv *, jclass, const char *, const char *);
typedef jint (*get_int_fn)(JNIEnv *, jobject, void *);
typedef void (*clear_fn)(JNIEnv *);

static const char IOCTL_MARK[] = "phisap-ioctl-13";

static int sys_ioctl(int fd, unsigned long req, unsigned long arg) {
    register long x0 __asm__("x0") = fd;
    register long x1 __asm__("x1") = (long)req;
    register long x2 __asm__("x2") = (long)arg;
    register long x8 __asm__("x8") = 29;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x1), "r"(x2), "r"(x8) : "memory", "cc");
    return (int)x0;
}

static int raw_fd(JNIEnv *env, jobject fd) {
    void **table = *(void ***)env;
    get_class_fn get_class = (get_class_fn)table[31];
    get_field_fn get_field = (get_field_fn)table[94];
    get_int_fn get_int = (get_int_fn)table[100];
    clear_fn clear = (clear_fn)table[17];
    jclass cls = get_class(env, fd);
    if (!cls) {
        clear(env);
        return -1;
    }
    void *fid = get_field(env, cls, "descriptor", "I");
    if (!fid) {
        clear(env);
        fid = get_field(env, cls, "fd", "I");
        if (!fid) {
            clear(env);
            return -1;
        }
    }
    return (int)get_int(env, fd, fid);
}

__attribute__((visibility("default")))
jint Java_app_phisap_pocket_Injector_nioctl(JNIEnv *env, jclass clazz, jobject fd, jint req, jint arg) {
    (void)clazz;
    if (IOCTL_MARK[0] != 'p' || !env || !fd) return -1;
    int raw = raw_fd(env, fd);
    if (raw < 0) return -1;
    return sys_ioctl(raw, (unsigned long)req, (unsigned long)(unsigned int)arg);
}
