#include <libusb-1.0/libusb.h>
#include <stdio.h>
int main(void) {
    libusb_context *ctx = NULL;
    libusb_init(&ctx);
    libusb_device **list = NULL;
    ssize_t n = libusb_get_device_list(ctx, &list);
    for (ssize_t i = 0; i < n; i++) {
        struct libusb_device_descriptor d;
        if (libusb_get_device_descriptor(list[i], &d) != 0) continue;
        printf("设备 %04x:%04x 配置数=%d\n", d.idVendor, d.idProduct, d.bNumConfigurations);
        for (int c = 0; c < d.bNumConfigurations; c++) {
            struct libusb_config_descriptor *cfg = NULL;
            if (libusb_get_config_descriptor(list[i], c, &cfg) != 0) continue;
            for (int j = 0; j < cfg->bNumInterfaces; j++) {
                const struct libusb_interface *itf = &cfg->interface[j];
                for (int k = 0; k < itf->num_altsetting; k++) {
                    const struct libusb_interface_descriptor *a = &itf->altsetting[k];
                    printf("  接口%d alt%d: class=0x%02x sub=0x%02x proto=0x%02x 端点=%d\n",
                           a->bInterfaceNumber, a->bAlternateSetting, a->bInterfaceClass,
                           a->bInterfaceSubClass, a->bInterfaceProtocol, a->bNumEndpoints);
                }
            }
            libusb_free_config_descriptor(cfg);
        }
    }
    if (n > 0) libusb_free_device_list(list, 1);
    libusb_exit(ctx);
    return 0;
}
