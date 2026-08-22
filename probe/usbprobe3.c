#include <libusb-1.0/libusb.h>
#include <stdio.h>
#include <unistd.h>

static FILE *plf;

int main(void) {
    plf = fopen("/tmp/probe3.log", "w");
    fprintf(plf, "step1 开始\n"); fflush(plf); usleep(300000);
    libusb_context *ctx = NULL;
    int rc = libusb_init(&ctx);
    fprintf(plf, "step2 libusb_init rc=%d\n", rc); fflush(plf); usleep(300000);
    libusb_set_debug(ctx, LIBUSB_LOG_LEVEL_NONE);
    libusb_device **list = NULL;
    ssize_t n = libusb_get_device_list(ctx, &list);
    fprintf(plf, "step3 get_device_list n=%zd\n", n); fflush(plf); usleep(300000);
    for (ssize_t i = 0; i < n; i++) {
        struct libusb_device_descriptor d;
        if (libusb_get_device_descriptor(list[i], &d) == 0) {
            fprintf(plf, "  设备 %04x:%04x class=0x%02x\n",
                    d.idVendor, d.idProduct, d.bDeviceClass);
        }
    }
    if (n > 0) libusb_free_device_list(list, 1);
    fprintf(plf, "step4 完成\n"); fflush(plf);
    fclose(plf);
    return 0;
}
