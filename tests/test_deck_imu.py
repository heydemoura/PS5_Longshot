"""The Steam Deck's own gyroscope and accelerometer, read from the controller itself (host/deck_imu.h/.c).

Started from Steam, SDL opens only Steam Input's stand-in pad, which has no sensors, so the Deck's own report is the
only gyro there is. The parser is compiled and checked against packets built here:

- the Deck's state report at the offsets Valve's structs give, in SDL's frame and units (accel m/s^2, gyro rad/s):
  +X right, +Y out of the face buttons, +Z toward the player;
- full scale (32767) is the gyroscope's 2000 degrees a second and the accelerometer's 2 g;
- anything else is refused: another report type, another length, another version, a short read, nothing at all;
- host/pad_sdl.c asks only where the pad path has nothing (no open pad with sensors), and only while Motion controls
  is on, and it releases the reader as soon as either stops being true.

python -I -B tests/test_deck_imu.py [--cc clang]
"""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

RUN_ALL_ARGS = ["--cc", "{cc}"]

ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "deck_imu.h"
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); return 1; } } while (0)
#define NEAR(a, b) (fabsf((a) - (b)) <= 0.002f + fabsf(b) * 0.001f)

static void put16(uint8_t *p, int off, int16_t v) { p[off] = (uint8_t)(v & 0xFF); p[off + 1] = (uint8_t)((v >> 8) & 0xFF); }
static void state(uint8_t *p, int16_t ax, int16_t ay, int16_t az, int16_t gx, int16_t gy, int16_t gz) {
    memset(p, 0, DECK_IMU_REPORT_BYTES);
    p[0] = 0x01, p[1] = 0x00;                    /* version 1, little endian */
    p[2] = DECK_IMU_MSG_DECK_STATE;
    p[3] = DECK_IMU_REPORT_BYTES;
    put16(p, 4, 1234);                           /* the packet number */
    put16(p, 8, 4321);                           /* the buttons */
    put16(p, 16, 77), put16(p, 18, -77);         /* the touchpads */
    put16(p, 24, ax), put16(p, 26, ay), put16(p, 28, az);
    put16(p, 30, gx), put16(p, 32, gy), put16(p, 34, gz);
    put16(p, 44, 1111), put16(p, 46, 2222);      /* the triggers */
}

int main(void) {
    uint8_t pkt[DECK_IMU_REPORT_BYTES];
    float a[3], g[3];
    /* a still pad lying face up: 1 g on the axis the SDL frame puts the world's up on, nothing turning */
    state(pkt, 0, 0, 16384, 0, 0, 0);
    CHECK(deck_imu_parse(pkt, sizeof pkt, a, g));
    CHECK(NEAR(a[0], 0.0f) && NEAR(a[1], 9.80665f) && NEAR(a[2], 0.0f));
    CHECK(NEAR(g[0], 0.0f) && NEAR(g[1], 0.0f) && NEAR(g[2], 0.0f));
    /* the axes SDL's Steam Deck driver maps: accel x, z, -y; gyro x, z, -y */
    state(pkt, 8192, -16384, 8192, 8192, -16384, 16384);
    CHECK(deck_imu_parse(pkt, sizeof pkt, a, g));
    CHECK(NEAR(a[0], 4.903325f) && NEAR(a[1], 4.903325f) && NEAR(a[2], 9.80665f));
    CHECK(NEAR(g[0], 8.726646f) && NEAR(g[1], 17.453292f) && NEAR(g[2], 17.453292f));
    /* full scale: 2000 degrees a second, 2 g */
    state(pkt, 32767, 32767, 32767, 32767, 32767, 32767);
    CHECK(deck_imu_parse(pkt, sizeof pkt, a, g));
    CHECK(NEAR(a[0], 19.6133f) && NEAR(a[2], -19.6133f));
    CHECK(NEAR(g[0], 34.90659f) && NEAR(g[2], -34.90659f));
    /* nothing else is a reading */
    state(pkt, 1, 2, 3, 4, 5, 6);
    pkt[2] = 8;
    CHECK(!deck_imu_parse(pkt, sizeof pkt, a, g));          /* another report type */
    state(pkt, 1, 2, 3, 4, 5, 6);
    pkt[3] = 60;
    CHECK(!deck_imu_parse(pkt, sizeof pkt, a, g));          /* another length */
    state(pkt, 1, 2, 3, 4, 5, 6);
    pkt[0] = 0x02;
    CHECK(!deck_imu_parse(pkt, sizeof pkt, a, g));          /* a version the offsets are not known in */
    state(pkt, 1, 2, 3, 4, 5, 6);
    CHECK(!deck_imu_parse(pkt, sizeof pkt - 1, a, g));      /* a short read */
    CHECK(!deck_imu_parse(NULL, sizeof pkt, a, g));
    /* the fields are where the comment says, not somewhere a neighbouring value happens to land */
    state(pkt, 0, 0, 0, 0, 0, 0);
    put16(pkt, 26, 8192);
    CHECK(deck_imu_parse(pkt, sizeof pkt, a, g) && NEAR(a[2], -4.903325f));
    printf("PASS: the Deck's state report is read in SDL's frame and units, and nothing else is a reading\n");
    return 0;
}
'''

def source_checks():
    pad = (ROOT / "host" / "pad_sdl.c").read_text(encoding="utf-8")
    hdr = (ROOT / "host" / "deck_imu.h").read_text(encoding="utf-8")
    defects = []
    if '"deck_imu.h"' not in pad:
        defects.append("pad_sdl.c does not include host/deck_imu.h")
    # the fallback is only where no open pad has sensors, and only while Motion controls is on
    if "const int deck_read = !read && want && !any && deck_imu_poll(" not in pad:
        defects.append("pad_sdl.c: the Deck's sensors are not gated on no pad with sensors, Motion controls on")
    if "if (!want || any) deck_imu_stop();" not in pad:
        defects.append("pad_sdl.c: the reader is not released when a pad's own sensors take over or Motion goes off")
    # the sensors are left as they were found at the run's end
    if "deck_imu_stop();" not in pad:
        defects.append("pad_sdl.c never stops the Deck reader")
    if "MOTION_SRC_DECK" not in pad:
        defects.append("pad_sdl.c: the Deck's reading has no source of its own in motion_poll")
    # one parser, one report size
    if "#define DECK_IMU_REPORT_BYTES 64" not in hdr:
        defects.append("deck_imu.h: the state report is not 64 bytes")
    return defects


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cc", default=os.environ.get("CC", "gcc"))
    a = p.parse_args()
    defects = source_checks()
    if defects:
        for d in defects:
            print(f"FAIL: {d}")
        raise SystemExit(1)
    with tempfile.TemporaryDirectory() as d:
        c, exe = Path(d) / "deck_imu.c", Path(d) / "deck_imu.exe"
        c.write_text(FIXTURE, encoding="utf-8")
        subprocess.run([a.cc, "-std=c11", "-Wall", "-Werror", "-I", str(ROOT / "host"), "-o", str(exe), str(c),
                        "-lm"], check=True)
        r = subprocess.run([str(exe)], capture_output=True, text=True)
        print(r.stdout, end="")
        if r.returncode:
            print(r.stderr, end="")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
