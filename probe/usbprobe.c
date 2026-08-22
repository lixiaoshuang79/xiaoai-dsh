// USB 探测器 v2：启动即打印状态，带心跳；并 dump 当前设备接口信息
#include <libusb-1.0/libusb.h>
#include <stdio.h>
#include <time.h>

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static void dump_all(libusb_context *ctx) {
    libusb_device **list = NULL;
    ssize_t n = libusb_get_device_list(ctx, &list);
    printf("dump: %zd 个设备\n", n);
    for (ssize_t i = 0; i < n; i++) {
        struct libusb_device_descriptor d;
        if (libusb_get_device_descriptor(list[i], &d) == 0) {
            printf("  dev %04x:%04x class=0x%02x sub=0x%02x proto=0x%02x confs=%d\n",
                   d.idVendor, d.idProduct, d.bDeviceClass, d.bDeviceSubClass,
                   d.bDeviceProtocol, d.bNumConfigurations);
            struct libusb_config_descriptor *cfg = NULL;
            if (libusb_get_config_descriptor(list[i], 0, &cfg) == 0) {
                for (int j = 0; j < cfg->bNumInterfaces; j++) {
                    const struct libusb_interface *itf = &cfg->interface[j];
                    for (int k = 0; k < itf->num_altsetting; k++) {
                        const struct libusb_interface_descriptor *a = &itf->altsetting[k];
                        printf("    接口%d alt%d class=0x%02x sub=0x%02x proto=0x%02x\n",
                               a->bInterfaceNumber, a->bAlternateSetting,
                               a->bInterfaceClass, a->bInterfaceSubClass, a->bInterfaceProtocol);
                    }
                }
                libusb_free_config_descriptor(cfg);
            }
        }
    }
    if (n > 0) libusb_free_device_list(list, 1);
}

int main(void) {
    printf("probe v2 started\n"); fflush(stdout);
    libusb_context *ctx = NULL;
    int rc = libusb_init(&ctx);
    printf("libusb_init rc=%d\n", rc); fflush(stdout);
    if (rc < 0) return 1;
    libusb_set_debug(ctx, LIBUSB_LOG_LEVEL_NONE);
    dump_all(ctx);
    fflush(stdout);

    double t0 = now_ms();
    int last_aml = -1, last_xm = -1;
    long loops = 0;
    while (now_ms() - t0 < 300000) { // 5 分钟
        libusb_device **list = NULL;
        ssize_t n = libusb_get_device_list(ctx, &list);
        int aml = 0, xm = 0;
        for (ssize_t i = 0; i < n; i++) {
            struct libusb_device_descriptor d;
            if (libusb_get_device_descriptor(list[i], &d) == 0) {
                if (d.idVendor == 0x1b8e && d.idProduct == 0xc003) aml = 1;
                if (d.idVendor == 0x2717) xm = 1;
            }
        }
        if (n > 0) libusb_free_device_list(list, 1);
        if (aml != last_aml || xm != last_xm) {
            printf("[%8.1fms] amlogic刷机接口=%d 小米设备=%d\n", now_ms() - t0, aml, xm);
            fflush(stdout);
            last_aml = aml; last_xm = xm;
        }
        if (aml) {
            printf("!!! FOUND AMLOGIC 1b8e:c003 !!!\n"); fflush(stdout);
            return 0;
        }
        loops++;
        if (loops % 2000 == 0) { printf("心跳 %ld 轮 %.0fms\n", loops, now_ms() - t0); fflush(stdout); }
    }
    printf("探测结束（5分钟无发现）共 %ld 轮\n", loops);
    return 2;
}
