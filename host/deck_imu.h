/* (longshot) The Steam Deck's built-in gyroscope and accelerometer (host/deck_imu.c), read from the controller when Steam
   Input's stand-in pad is the only one SDL opens.

   Started from Steam, the game gets an SDL_GAMECONTROLLER_IGNORE_DEVICES list with Valve's controllers in it, and SDL
   then opens only Steam Input's stand-in pad, which has no sensors (host/steam_pads.h). The Deck's own controls keep
   theirs: the same 64-byte state report SDL's Steam Deck driver reads carries the accelerometer and the gyroscope, and
   opening that hidraw node read-only disturbs nothing (no lizard mode change, no mapping cleared, no watchdog), so the
   reader here is a fallback beside a real pad, never instead of one.

   The report is Valve's controller_structs.h/controller_constants.h (zlib license, Copyright 2020 Valve Corporation):
   a header (report version at 0, message type at 2, length at 3) and the Deck's state payload. The fields and their
   scaling are SDL's own (SDL_hidapi_steamdeck.c, zlib license): the accelerometer in m/s^2 and the gyroscope in rad/s,
   in SDL's frame (+X right, +Y out of the face buttons, +Z toward the player), which is what host/hos_motion.h takes.
   Pure (no I/O): tests/test_deck_imu.py compiles it as it is. */

#pragma once
#include <stdint.h>

/* the state report Steam's controller streams: 64 bytes, message type 9 (ID_CONTROLLER_DECK_STATE), version 1 */
#define DECK_IMU_REPORT_BYTES 64
#define DECK_IMU_MSG_DECK_STATE 9
#define DECK_IMU_MSG_VERSION 0x0001

#define DECK_IMU_G 9.80665f
#define DECK_IMU_PI 3.14159265f
#define DECK_IMU_GYRO_DPS 2000.0f   /* the gyroscope's full scale, degrees a second at 32768 */

static inline int16_t deck_imu_s16(const uint8_t *p) {
    return (int16_t)(uint16_t)((uint16_t)p[0] | (uint16_t)((uint16_t)p[1] << 8));
}

/* One report as SDL would hand its sensors over: 1 for the Deck's state report, 0 for anything else (another device's
   report, a short read, a version the fields are not known in). accel: m/s^2, gyro: rad/s, SDL's frame. */
static inline int deck_imu_parse(const uint8_t *pkt, int len, float accel[3], float gyro[3]) {
    if (!pkt || len < DECK_IMU_REPORT_BYTES) return 0;
    if ((uint16_t)((uint16_t)pkt[0] | (uint16_t)((uint16_t)pkt[1] << 8)) != DECK_IMU_MSG_VERSION) return 0;
    if (pkt[2] != DECK_IMU_MSG_DECK_STATE || pkt[3] != DECK_IMU_REPORT_BYTES) return 0;
    const int16_t ax = deck_imu_s16(pkt + 24), ay = deck_imu_s16(pkt + 26), az = deck_imu_s16(pkt + 28);
    const int16_t gx = deck_imu_s16(pkt + 30), gy = deck_imu_s16(pkt + 32), gz = deck_imu_s16(pkt + 34);
    if (accel) {
        accel[0] = (float)ax / 32768.0f * 2.0f * DECK_IMU_G;
        accel[1] = (float)az / 32768.0f * 2.0f * DECK_IMU_G;
        accel[2] = (float)-ay / 32768.0f * 2.0f * DECK_IMU_G;
    }
    if (gyro) {
        const float rad = 2000.0f * DECK_IMU_PI / 180.0f / 32768.0f;
        gyro[0] = (float)gx * rad;
        gyro[1] = (float)gz * rad;
        gyro[2] = (float)-gy * rad;
    }
    return 1;
}

/* (host/deck_imu.c, Linux; elsewhere each returns 0 and the pad path is all there is) */

/* Whether a Deck state report has been read in the last few seconds, which is what the Motion controls row calls a
   gyro found. False until a report has arrived: a Valve hidraw node is often the same device's mouse or keyboard
   interface, so a vendor id alone is not the controller. Answers from the reader's own state; it never scans. */
int deck_imu_present(int64_t now_ns);

/* The reading since the last call: 1 with accel/gyro filled (SDL's frame and units), 0 when there is none (no device,
   0 before the first report). Between reports it hands back the newest reading, as SDL's sensor reads do, and the filter
   takes a repeat for no reading. The sensors are turned on the first time a reading is wanted, and again if they stop
   changing, and only where their setting could be read first, so it can be put back. Never waits on the device: a
   candidate node is tried with one non-blocking read per call. now_ns is the pad poll's clock. */
int deck_imu_poll(float accel[3], float gyro[3], int64_t now_ns);

/* The sensors as they were found, and the device closed: at the run's end (or the menu's Motion controls going off). */
void deck_imu_stop(void);
