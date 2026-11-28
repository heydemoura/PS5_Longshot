/* (longshot) The Steam Deck's own gyroscope and accelerometer, read from the controller itself (host/deck_imu.h).

   Why: started from Steam, SDL opens only Steam Input's stand-in pad, which has no sensors (host/steam_pads.h), so a
   Deck player gets no gyro. The Deck's state report carries the sensors, and this reader takes that report on the
   controller's own hidraw node without touching anything else on the device: no lizard mode change, no mappings
   cleared, no watchdog. Valve's protocol as SDL's Steam Deck driver uses it (zlib license, Copyright 2020 Valve
   Corporation: controller_structs.h, controller_constants.h, SDL_hidapi_steamdeck.c).

   The sensors are off until asked for (SETTING_IMU_MODE): Steam turns them on when a layout uses the gyro and leaves
   them off otherwise, which is why the same node reads zeros under Steam Input. One feature report turns them on, and
   the value is put back when the reader stops.

   Linux only: /sys/class/hidraw and a /dev/hidraw node. Everywhere else this file says nothing and deck_imu_present()
   answers 0, so the pad path is all there is. */

#include "deck_imu.h"
#include <stdio.h>
#include <string.h>
void hos_log(const char *fmt, ...);   /* (hos.h; declared here so a test builds this file alone) */

#ifndef LONGSHOT_ENV_H
#include "../gpu/vk/compat/longshot_env.h"
#endif

#ifdef __linux__

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define DECK_IMU_VENDOR 0x28DE          /* Valve's */
#define DECK_IMU_SETTING 48             /* SETTING_IMU_MODE (Valve's settings list, append-only) */
#define DECK_IMU_RAW_ACCEL_GYRO 0x18    /* SETTING_GYRO_MODE_SEND_RAW_ACCEL | _SEND_RAW_GYRO */
#define DECK_IMU_ID_SET_SETTINGS_VALUES 0x87
#define DECK_IMU_ID_GET_SETTINGS_VALUES 0x89
#define DECK_IMU_FEATURE_BYTES (DECK_IMU_REPORT_BYTES + 1)   /* report number 0, then the body */

static int imu_fd = -1;
static int imu_writable;          /* opened for the feature report too */
static int imu_set = -1;          /* what we set SETTING_IMU_MODE to, -1 while we have not */
static int imu_saved = -1;        /* the setting as it was found, -1 when the device would not say */
static int imu_said;              /* (the one line about which sensors are being read) */
static int64_t imu_last_ns;       /* the last report read */
static int64_t imu_same_ns;       /* since when the reading has not changed */
static int64_t imu_set_ns;        /* the last time the sensors were turned on */
static int64_t imu_scan_ns;       /* the last look at /sys/class/hidraw */
static int64_t imu_probe_ns;      /* the next candidate may be tried at this point (one every 100 ms) */
static int imu_probe_fd = -1;      /* candidate held open while its first report arrives */
static int imu_probe_writable;
static int64_t imu_probe_until_ns;
static char imu_cand[8][32];      /* (the Valve nodes the last look found: bounded, the mouse and keyboard interfaces included) */
static int imu_candidates, imu_probe_next;
static float imu_prev_a[3], imu_prev_g[3];
static int imu_have_prev;
static float imu_held_a[3], imu_held_g[3];   /* (the newest reading, handed out again between reports: SDL's sensor
                                                reads return the current values every time, and the filter's clock
                                                depends on being asked every poll) */
static int imu_held;

/* _IOWR('H', nr, size): hidraw's feature report ioctls (linux/hidraw.h's HIDIOCSFEATURE/HIDIOCGFEATURE, which SteamOS
   keeps out of /usr/include with the rest of them: the two numbers here are all that file gives) */
#define DECK_IMU_IOC(dir, nr, size) (((unsigned long)(dir) << 30) | (((unsigned long)(size) & 0x3FFF) << 16) | \
                                     ((unsigned long)('H') << 8) | (unsigned long)(nr))
#define DECK_IMU_IOCTL_SFEATURE DECK_IMU_IOC(3, 0x06, DECK_IMU_FEATURE_BYTES)
#define DECK_IMU_IOCTL_GFEATURE DECK_IMU_IOC(3, 0x07, DECK_IMU_FEATURE_BYTES)

static int deck_imu_read_setting(int fd);   /* (below: what SETTING_IMU_MODE was, before the reader sets it) */

/* the switch: HOS_DECK_IMU=0 leaves the reader out (the A/B) */
static int deck_imu_on(void) {
    static int on = -1;
    if (on < 0) on = env_on_or("HOS_DECK_IMU", 1);
    return on;
}

/* Whether this uevent file (of a hidraw node) is a Valve device's. */
static int deck_imu_valve_uevent(const char *path) {
    char buf[1024];
    FILE *f = fopen(path, "r");
    if (!f) return 0;
    size_t n = fread(buf, 1, sizeof buf - 1, f);
    fclose(f);
    buf[n] = 0;
    const char *id = strstr(buf, "HID_ID=");
    if (!id) return 0;
    unsigned bus = 0, vendor = 0, product = 0;
    if (sscanf(id, "HID_ID=%x:%x:%x", &bus, &vendor, &product) != 3) return 0;
    return vendor == DECK_IMU_VENDOR;
}

/* The Valve nodes there are now, by path: the mouse, keyboard and controller interfaces of one device all appear, and
   only the controller's streams the state report. Re-read at most every 2 s, on the caller's clock. */
static void deck_imu_scan(int64_t now_ns) {
    if (imu_scan_ns && now_ns - imu_scan_ns < 2000000000LL) return;
    imu_scan_ns = now_ns;
    imu_candidates = 0;
    imu_probe_next = 0;
    DIR *d = opendir("/sys/class/hidraw");
    if (!d) return;
    struct dirent *e;
    const int cap = (int)(sizeof imu_cand / sizeof imu_cand[0]);
    while ((e = readdir(d))) {
        if (strncmp(e->d_name, "hidraw", 6) != 0) continue;
        char uevent[256];
        snprintf(uevent, sizeof uevent, "/sys/class/hidraw/%s/device/uevent", e->d_name);
        if (!deck_imu_valve_uevent(uevent)) continue;   /* (vendor only: which interface it is, the report says) */
        if (imu_candidates >= cap) break;
        snprintf(imu_cand[imu_candidates], sizeof imu_cand[0], "/dev/%s", e->d_name);
        imu_candidates++;
    }
    closedir(d);
}

/* Hold a candidate for up to 100 ms, reading once per poll without waiting. A newly opened hidraw queue can be empty
   until the next report arrives; other interfaces may send unrelated reports or none at all. */
static void deck_imu_probe(int64_t now_ns) {
    if (imu_probe_fd < 0) {
        if (now_ns < imu_probe_ns) return;
        deck_imu_scan(now_ns);
        if (!imu_candidates) return;
        if (imu_probe_next >= imu_candidates) imu_probe_next = 0;
        const char *node = imu_cand[imu_probe_next++];
        imu_probe_writable = 1;
        imu_probe_fd = open(node, O_RDWR | O_NONBLOCK);
        if (imu_probe_fd < 0) {
            imu_probe_fd = open(node, O_RDONLY | O_NONBLOCK);
            imu_probe_writable = 0;
        }
        imu_probe_ns = now_ns + 100000000LL;
        if (imu_probe_fd < 0) return;
        imu_probe_until_ns = imu_probe_ns;
    }
    uint8_t pkt[DECK_IMU_REPORT_BYTES];
    float a[3], g[3];
    const ssize_t n = read(imu_probe_fd, pkt, sizeof pkt);
    if (n != (ssize_t)sizeof pkt || !deck_imu_parse(pkt, (int)n, a, g)) {
        if (now_ns >= imu_probe_until_ns || n == 0 ||
            (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
            close(imu_probe_fd);
            imu_probe_fd = -1;
        }
        return;
    }
    imu_fd = imu_probe_fd;
    imu_probe_fd = -1;
    imu_writable = imu_probe_writable;
    imu_saved = imu_writable ? deck_imu_read_setting(imu_fd) : -1;   /* (-1: not read, so never written back) */
    imu_set = -1;
    imu_have_prev = 0;
    imu_last_ns = now_ns;
    memcpy(imu_held_a, a, sizeof imu_held_a);
    memcpy(imu_held_g, g, sizeof imu_held_g);
    imu_held = 1;
}

/* One feature report: {report number 0, type, length, the settings}. 1 when the device took it. */
static int deck_imu_send_setting(int fd, unsigned char type, int value) {
    uint8_t buf[DECK_IMU_FEATURE_BYTES];
    memset(buf, 0, sizeof buf);
    buf[0] = 0x00;   /* (the report number: Valve's reports are unnumbered, 0) */
    buf[1] = type;
    buf[2] = 3;      /* (one ControllerSetting: a setting number and its 16-bit value) */
    buf[3] = DECK_IMU_SETTING;
    buf[4] = (uint8_t)(value & 0xFF);
    buf[5] = (uint8_t)((value >> 8) & 0xFF);
    return ioctl(fd, DECK_IMU_IOCTL_SFEATURE, buf) >= 0;
}

/* What SETTING_IMU_MODE was before the reader set it, for putting it back: -1 when the device would not say (the value
   read is only taken when it is the setting that was asked for). */
static int deck_imu_read_setting(int fd) {
    if (!deck_imu_send_setting(fd, DECK_IMU_ID_GET_SETTINGS_VALUES, 0)) return -1;
    uint8_t buf[DECK_IMU_FEATURE_BYTES];
    memset(buf, 0, sizeof buf);
    buf[0] = 0x00;
    if (ioctl(fd, DECK_IMU_IOCTL_GFEATURE, buf) < 0) return -1;
    if (buf[1] != DECK_IMU_ID_GET_SETTINGS_VALUES) return -1;
    for (int off = 2; off + 3 <= DECK_IMU_FEATURE_BYTES; off += 3)
        if (buf[off] == DECK_IMU_SETTING) return (int)((uint16_t)buf[off + 1] | ((uint16_t)buf[off + 2] << 8));
    return -1;
}

static void deck_imu_close(void) {
    if (imu_probe_fd >= 0) {
        close(imu_probe_fd);
        imu_probe_fd = -1;
    }
    if (imu_fd < 0) return;
    /* The setting is put back only where this reader still owns it: the value read now has to be the one this reader
       wrote (Steam changes the same setting when its layout turns the gyro on or off, and a later owner's value is left
       alone). Nothing is written at all when the value found at open could not be read: writing it could not be undone. */
    if (imu_writable && imu_set >= 0 && imu_saved >= 0 && deck_imu_read_setting(imu_fd) == imu_set)
        deck_imu_send_setting(imu_fd, DECK_IMU_ID_SET_SETTINGS_VALUES, imu_saved);
    close(imu_fd);
    imu_fd = -1;
    imu_set = -1;
    imu_saved = -1;
    imu_have_prev = 0;
    imu_held = 0;
}

int deck_imu_present(int64_t now_ns) {
    if (!deck_imu_on()) return 0;
    /* A reading, not a vendor id: a Valve node is often the mouse or the keyboard interface of the same device, so
       presence follows the state report itself, and lapses when the reports stop. Nothing scans the device here. */
    return imu_fd >= 0 && imu_held && now_ns - imu_last_ns < 3000000000LL;
}

int deck_imu_poll(float accel[3], float gyro[3], int64_t now_ns) {
    if (!deck_imu_on()) {
        deck_imu_close();
        return 0;
    }
    if (imu_fd < 0) {
        /* One non-blocking read per poll, and a directory look at most every 2 s. */
        deck_imu_probe(now_ns);
        if (imu_fd < 0) return 0;
    }
    /* the sensors on: Steam leaves them off unless a layout uses the gyro, and the report then carries zeros. Only where
       the setting could be read first, so what the reader writes can be put back */
    const int64_t clock_now = now_ns;
    if (imu_writable && imu_set != DECK_IMU_RAW_ACCEL_GYRO && clock_now - imu_set_ns > 1000000000LL) {
        if (imu_saved < 0) {
            if (!imu_said) {
                imu_said = 2;
                hos_log("hid: the Steam Deck's IMU setting could not be read: its sensors are left as Steam set them");
            }
        } else {
            imu_set_ns = clock_now;
            if (deck_imu_send_setting(imu_fd, DECK_IMU_ID_SET_SETTINGS_VALUES, DECK_IMU_RAW_ACCEL_GYRO))
                imu_set = DECK_IMU_RAW_ACCEL_GYRO;
            else if (!imu_said)
                hos_log("hid: the Steam Deck's motion sensors could not be turned on: no gyro from its built-in controls");
            if (imu_set == DECK_IMU_RAW_ACCEL_GYRO && imu_said != 1) {
                imu_said = 1;
                hos_log("hid: the Steam Deck's own gyroscope and accelerometer: read from the controller "
                        "(HOS_DECK_IMU=0 stops it)");
            }
        }
    }
    int got = 0;
    uint8_t pkt[DECK_IMU_REPORT_BYTES];
    for (;;) {
        ssize_t n = read(imu_fd, pkt, sizeof pkt);
        if (n <= 0) break;
        if (n == (ssize_t)sizeof pkt && deck_imu_parse(pkt, (int)n, accel, gyro)) got = 1;
    }
    if (got) {
        memcpy(imu_held_a, accel, sizeof imu_held_a);
        memcpy(imu_held_g, gyro, sizeof imu_held_g);
        imu_held = 1;
        imu_last_ns = clock_now;
        if (imu_have_prev && !memcmp(accel, imu_prev_a, sizeof imu_prev_a) && !memcmp(gyro, imu_prev_g, sizeof imu_prev_g)) {
            if (!imu_same_ns) imu_same_ns = clock_now;
            /* (a Deck lying perfectly still reads its accelerometer's last bit moving, as any real one does: the same
               numbers for over a second mean the sensors were turned off under us, as Steam's layout switch can) */
            else if (clock_now - imu_same_ns > 1200000000LL && clock_now - imu_set_ns > 3000000000LL) {
                imu_set = -1;
                imu_same_ns = 0;
                hos_log("hid: the Steam Deck's gyroscope and accelerometer stopped changing: turned on again");
            }
        } else {
            if (!imu_have_prev) imu_have_prev = 1;
            memcpy(imu_prev_a, accel, sizeof imu_prev_a);
            memcpy(imu_prev_g, gyro, sizeof imu_prev_g);
            imu_same_ns = 0;
        }
    }
    /* no report for a while: the node or Steam's hold on the device changed, so it is opened again */
    if (clock_now - imu_last_ns > 3000000000LL) {
        deck_imu_close();
        imu_probe_ns = clock_now + 1000000000LL;
        return 0;
    }
    /* no new report since the last poll: the newest reading again, as SDL's sensor reads give it. The filter takes a
       repeat for no reading and counts the time, so the next new one is one step, not a gap */
    if (!got) {
        if (!imu_held) return 0;
        memcpy(accel, imu_held_a, sizeof imu_held_a);
        memcpy(gyro, imu_held_g, sizeof imu_held_g);
    }
    return 1;
}

void deck_imu_stop(void) { deck_imu_close(); }

#else   /* (not Linux: no hidraw, the pad path is all there is) */

int deck_imu_present(int64_t now_ns) {
    (void)now_ns;
    return 0;
}
int deck_imu_poll(float accel[3], float gyro[3], int64_t now_ns) {
    (void)accel;
    (void)gyro;
    (void)now_ns;
    return 0;
}
void deck_imu_stop(void) {}

#endif
