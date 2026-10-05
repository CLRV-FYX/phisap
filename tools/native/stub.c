/* 只给链接器看的空符号。装进手机后由系统的 libc 提供真正实现。
   dlopen / dlsym 放在 libdl 桩里，避免和 libc 抢同一个符号。 */
int usleep(unsigned u) { (void)u; return 0; }
int pthread_create(unsigned long *t, const void *a, void *(*fn)(void *), void *arg) {
    (void)t; (void)a; (void)fn; (void)arg; return 0;
}
int open(const char *p, int f, int m) { (void)p; (void)f; (void)m; return 0; }
int close(int fd) { (void)fd; return 0; }
long write(int fd, const void *b, unsigned long n) { (void)fd; (void)b; (void)n; return 0; }
long read(int fd, void *b, unsigned long n) { (void)fd; (void)b; (void)n; return 0; }
int clock_gettime(int c, void *ts) { (void)c; (void)ts; return 0; }
void *mmap(void *a, unsigned long n, int p, int f, int fd, long o) {
    (void)a; (void)n; (void)p; (void)f; (void)fd; (void)o; return 0;
}
int mprotect(void *a, unsigned long n, int p) { (void)a; (void)n; (void)p; return 0; }
long lseek(int fd, long o, int w) { (void)fd; (void)o; (void)w; return 0; }
long pwrite(int fd, const void *b, unsigned long n, long o) {
    (void)fd; (void)b; (void)n; (void)o; return 0;
}
int socket(int d, int t, int p) { (void)d; (void)t; (void)p; return 0; }
int connect(int fd, const void *a, unsigned int n) { (void)fd; (void)a; (void)n; return 0; }
long send(int fd, const void *b, unsigned long n, int f) {
    (void)fd; (void)b; (void)n; (void)f; return 0;
}
void (*signal(int s, void (*fn)(int)))(int) { (void)s; (void)fn; return 0; }
void *memcpy(void *d, const void *s, unsigned long n) { (void)s; (void)n; return d; }
void *memset(void *d, int c, unsigned long n) { (void)c; (void)n; return d; }
void *memmove(void *d, const void *s, unsigned long n) { (void)s; (void)n; return d; }
int memcmp(const void *a, const void *b, unsigned long n) { (void)a; (void)b; (void)n; return 0; }
