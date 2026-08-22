/*
 * hook_tts.c v3 — armv7 32-bit LD_PRELOAD hook（无 libc 依赖，Mac clang 交叉编译）
 * 目标进程：mico_aivs_lab（官方小爱）。
 *
 * 拦截官方把 "name":"Speak" 指令写入 instruction.log 的 write/writev 瞬间，
 * 直接 kill mediaplayer —— 官方 TTS（云端 STREAM）的唯一播放者。
 * 官方还没发出 TTS 调度（落盘后 ~0.2-0.3s）mediaplayer 就已死，官方话音零泄漏。
 *
 * 关键事实（实测验证）：
 *   - 官方 TTS = 官方进程收到云端 Speak → 调度 mediaplayer 播放（pcmC0D2p 实测）。
 *   - 我们的 TTS/音乐 = miplayer（tts_play.sh 本地合成文件 / play_url），
 *     完全不依赖 mediaplayer —— 杀 mediaplayer 对我们零影响。
 *   - 杀 mibrain 无效（只是 entry point）；杀官方进程会诱发云端补发死循环且我们
 *     的 TTS 合成依赖官方存活。mediaplayer 是最干净的切点。
 *
 * 保护官方合法应答（EXCEPT 白名单：闹钟/音量/新闻等官方独占应答）：
 *   - 官方合法应答的 Speak 没有「试听/会员/黑胶/版权/APP」等音乐版权失败话术特征，
 *     且此时我们不在作答（/tmp/xdf_our_pending 过期）→ 放行。
 *   - 点歌场景：官方「试听版…」话术含特征词 → 杀；或我们的作答标记新鲜 → 杀。
 *
 * 直连模式（/tmp/xdf_direct_mode 存在，Mac 挂了）：全部放行。
 *
 * 编译（Mac 上）：
 *   clang --target=armv7-linux-gnueabihf -marm -nostdlib -fPIC -shared -O2 \
 *     -fno-stack-protector -fno-builtin -ffreestanding hook_tts.c -o hook_tts.so
 */

#define SYS_exit        1
#define SYS_read        3
#define SYS_write       4
#define SYS_close       6
#define SYS_time        13
#define SYS_kill        37
#define SYS_writev      146
#define SYS_getdents64  217
#define SYS_openat      322
#define SYS_newfstatat  327

#define AT_FDCWD (-100)
#define SIGKILL   9

#define O_RDONLY    0
#define O_WRONLY    1
#define O_CREAT     0100
#define O_APPEND    02000
#define O_DIRECTORY 040000  /* arm32: O_DIRECTORY=040000(0x4000)，非64位的0200000 */

typedef unsigned int u32;
typedef unsigned long size_t;
typedef unsigned long long u64;

static inline long __svc1(long n, long a) {
    register long r7 __asm__("r7") = n;
    register long r0 __asm__("r0") = a;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7) : "r1", "r2", "r3", "memory");
    return r0;
}
static inline long __svc2(long n, long a, long b) {
    register long r7 __asm__("r7") = n;
    register long r0 __asm__("r0") = a;
    register long r1 __asm__("r1") = b;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7), "r"(r1) : "r2", "r3", "memory");
    return r0;
}
static inline long __svc3(long n, long a, long b, long c) {
    register long r7 __asm__("r7") = n;
    register long r0 __asm__("r0") = a;
    register long r1 __asm__("r1") = b;
    register long r2 __asm__("r2") = c;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7), "r"(r1), "r"(r2) : "r3", "memory");
    return r0;
}
static inline long __svc4(long n, long a, long b, long c, long d) {
    register long r7 __asm__("r7") = n;
    register long r0 __asm__("r0") = a;
    register long r1 __asm__("r1") = b;
    register long r2 __asm__("r2") = c;
    register long r3 __asm__("r3") = d;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7), "r"(r1), "r"(r2), "r"(r3) : "memory");
    return r0;
}

#define S_open(p, f)      __svc3(SYS_openat, AT_FDCWD, (long)(p), (long)(f))
#define S_read(f, b, n)   __svc3(SYS_read, (f), (long)(b), (long)(n))
#define S_write(f, b, n)  __svc3(SYS_write, (f), (long)(b), (long)(n))
#define S_close(f)        __svc1(SYS_close, (f))
#define S_kill(p, s)      __svc2(SYS_kill, (p), (s))
#define S_getdents64(f, b, n) __svc3(SYS_getdents64, (f), (long)(b), (long)(n))

/* ---- 日志辅助 ---- */
static void log_line(const char *s, int num) {
    char line[96];
    int len = 0, i, nd = 0, t;
    char digits[12];
    long fd;
    fd = S_open("/tmp/hook-tts.log", O_WRONLY | O_CREAT | O_APPEND);
    if (fd < 0) return;
    for (i = 0; s[i]; i++) line[len++] = s[i];
    t = num;
    while (t) { digits[nd++] = '0' + (t % 10); t /= 10; }
    if (!nd) digits[nd++] = '0';
    for (i = nd - 1; i >= 0; i--) line[len++] = digits[i];
    line[len++] = '\n';
    S_write(fd, line, len);
    S_close(fd);
}

static void log_raw(const char *tag, const unsigned char *p, long n) {
    char line[200];
    int len = 0, i;
    long fd = S_open("/tmp/hook-tts.log", O_WRONLY | O_CREAT | O_APPEND);
    if (fd < 0) return;
    for (i = 0; tag[i]; i++) line[len++] = tag[i];
    for (i = 0; i < n && i < 70 && len < 185; i++) {
        unsigned char c = p[i];
        line[len++] = (c >= 32 && c < 127) ? (char)c : '.';
    }
    line[len++] = '\n';
    S_write(fd, line, len);
    S_close(fd);
}

static int direct_mode(void) {
    unsigned char st[128];
    long r = __svc4(SYS_newfstatat, AT_FDCWD, (long)"/tmp/xdf_direct_mode", (long)st, 0);
    return r == 0;
}

/* 在 buf 前 n 字节内找子串 marker（纯字节匹配） */
static int has_marker(const unsigned char *p, long n, const char *m) {
    long i, k;
    for (i = 0; i + 8 < n; i++) {
        int hit = 1;
        for (k = 0; m[k]; k++) {
            if (i + k >= n || p[i + k] != (unsigned char)m[k]) { hit = 0; break; }
        }
        if (hit) return 1;
    }
    return 0;
}

struct d64 {
    unsigned long long d_ino;
    long long d_off;
    unsigned short d_reclen;
    unsigned char d_type;
    char d_name[];
};

/* 杀所有 mediaplayer 实例 —— 官方 TTS 的唯一播放者（我们的 TTS/音乐走 miplayer，不受影响）。
 * 必须全杀：mediaplayer 被重启后会留下僵尸进程（comm 仍为 mediaplayer，ps 显示 [mediaplayer]），
 * 只杀第一个 pid 可能命中僵尸（kill 僵尸=无害 no-op），真实进程漏杀导致官方话音泄漏。 */
static void kill_mediaplayer(void) {
    char buf[2048];
    long fd, r, pos, clen;
    int found = 0;
    const char *comm = "mediaplayer";
    int comm_len = 0;
    while (comm[comm_len]) comm_len++;

    fd = S_open("/proc", O_RDONLY | O_DIRECTORY);
    if (fd < 0) { log_line("NOMEDIA rc=", -2); return; }
    for (;;) {
        r = S_getdents64(fd, buf, sizeof buf);
        if (r <= 0) break;
        pos = 0;
        while (pos < r) {
            struct d64 *d = (struct d64 *)(buf + pos);
            if (d->d_reclen == 0) break;
            if (d->d_name[0] >= '0' && d->d_name[0] <= '9') {
                char path[96], cbuf[64];
                int pi = 0, ci, match = 1, pid = 0;
                const char *q = "/proc/", *w = d->d_name, *suf = "/comm";
                while (*q) path[pi++] = *q++;
                while (*w) path[pi++] = *w++;
                while (*suf) path[pi++] = *suf++;
                path[pi] = 0;
                long cfd = S_open(path, O_RDONLY);
                if (cfd >= 0) {
                    clen = S_read(cfd, cbuf, sizeof cbuf);
                    S_close(cfd);
                    if (clen > 0) {
                        match = 1;
                        for (ci = 0; ci < comm_len; ci++)
                            if (ci >= clen || cbuf[ci] != (char)comm[ci]) { match = 0; break; }
                        if (match && (clen <= comm_len || cbuf[comm_len] == '\n')) {
                            w = d->d_name;
                            while (*w >= '0' && *w <= '9') { pid = pid * 10 + (*w - '0'); w++; }
                            {
                                long rc = S_kill(pid, SIGKILL);
                                found = 1;
                                log_line("KILLMEDIA pid=", pid);
                                log_line("killrc=", (int)rc);
                            }
                        }
                    }
                }
            }
            pos += d->d_reclen;
        }
    }
    S_close(fd);
    if (!found) log_line("NOMEDIA", 0);
}

/* 读 /tmp/xdf_our_pending 的 epoch 秒数；0 = 读不到 */
static long pending_epoch(void) {
    char buf[32];
    long fd = S_open("/tmp/xdf_our_pending", O_RDONLY);
    long n, v = 0, i;
    if (fd < 0) return 0;
    n = S_read(fd, buf, sizeof buf - 1);
    S_close(fd);
    if (n <= 0) return 0;
    for (i = 0; i < n; i++) {
        if (buf[i] >= '0' && buf[i] <= '9') v = v * 10 + (buf[i] - '0');
        else break;
    }
    return v;
}

/* 当前 epoch 秒数（clock_gettime CLOCK_REALTIME；aarch64 32 位兼容层 time(13) 返回不可靠） */
static long now_epoch(void) {
    long ts[2];
    long r = __svc2(263, 0, (long)ts);   /* SYS_clock_gettime, CLOCK_REALTIME=0 */
    if (r != 0) return 0;
    return ts[0];
}

/* 官方音乐版权失败话术特征（官方合法应答不含这些词） */
static int official_leak_markers(const unsigned char *p, long n) {
    return has_marker(p, n, "\u8bd5\u542c")            /* 试听 */
        || has_marker(p, n, "\u9ed1\u80f6")            /* 黑胶 */
        || has_marker(p, n, "\u4f1a\u5458")            /* 会员 */
        || has_marker(p, n, "\u7248\u6743")            /* 版权 */
        || has_marker(p, n, "APP");
}

/*
 * Speak 判定：
 *   直连模式 → 放行；
 *   官方版权失败话术（试听/黑胶/会员/版权/APP）→ 杀 mediaplayer；
 *   我们正在作答（pending ≤15s）→ 杀 mediaplayer（官方抢答）；
 *   其余（闹钟/音量等官方独占确认）→ 放行。
 */
static void check_write(const unsigned char *p, long n) {
    int m, dm;
    long pe, age;
    if (!p || n <= 0) return;
    m = has_marker(p, n, "\"name\":\"Speak\"");
    if (!m) return;
    dm = direct_mode();
    if (dm) { log_line("DIRECT-SKIP", 1); return; }
    if (official_leak_markers(p, n)) {
        log_raw("LEAK-KILL ", p, n);
        kill_mediaplayer();
        return;
    }
    pe = pending_epoch();
    age = (pe > 0) ? (now_epoch() - pe) : 9999;
    log_line("DBG pe=", (int)pe);
    log_line("DBG age=", (int)age);
    if (pe > 0 && age >= 0 && age <= 15) {
        log_raw("PEND-KILL ", p, n);
        kill_mediaplayer();
        return;
    }
    log_raw("PASS ", p, n);
}

struct iovec32 { u32 iov_base; u32 iov_len; };

__attribute__((visibility("default")))
long write(int fd, const void *buf, size_t count) {
    check_write((const unsigned char *)buf, (long)count);
    return __svc3(SYS_write, fd, (long)buf, (long)count);
}

__attribute__((visibility("default")))
long writev(int fd, const struct iovec32 *iov, int cnt) {
    int i;
    if (iov && cnt > 0)
        for (i = 0; i < cnt; i++)
            check_write((const unsigned char *)(unsigned long)iov[i].iov_base,
                        (long)iov[i].iov_len);
    return __svc3(SYS_writev, fd, (long)iov, (long)cnt);
}

/* 加载标记（.init_array 由动态链接器调用，无需 libc） */
__attribute__((constructor))
static void _mark_loaded(void) {
    static const char msg[] = "LOADED hook_tts\n";
    long fd = S_open("/tmp/hook-tts.log", O_WRONLY | O_CREAT | O_APPEND);
    if (fd >= 0) { S_write(fd, msg, sizeof msg - 1); S_close(fd); }
}
