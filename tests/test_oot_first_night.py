"""Check that a save stuck in the first adult night's time lock runs its clock again, in Hyrule Field too.

The game holds the clock (at 20:00 on the reports) while quest Meet_DekuTree (0x4dbca27f) is not Ready and
`HrlField_HasEnteredFirstTime_Adult` (MurmurHash3 0x8592aa82) is 1 (f_8347c020, called from f_803edf70). The Temple of
Time sets the flag when Link becomes adult; only the arrival that plays when adult Link walks out of the Market's
main gate onto the field clears it. So the lock belongs to the Temple and the ruined Market: reaching any other map
as adult another way (a warp song, Farore's Wind, a teleport, a save made in the field with the flag still set)
keeps it there for good (Longshot #1064, #2636).

`oot_inventory_refresh` runs twice a second and already reads the map and Link's age, so the repair lives there: with
no event running, clear the flag with the game's own setter (which queues the save's copy too: a write of the live
byte alone lasts until the next load) and log it once, anywhere but the Market maps (the Temple is inside them) and the
Chamber of Sages while Link is adult. Every write and every first-night log line is counted, and the stand-ins can
fail an age read or the setter (another game build): nothing is cleared then, and the refusal is logged once.

This cuts the real `oot_inventory_refresh` (and `oot_first_night_fix` when the source has it) out of game/oot_tools.c and
runs it against made-up save state, with no game and no romfs. Run it with:

    python -I tests/test_oot_first_night.py --cc gcc
"""
import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # (python -I leaves the check's own folder off the path)
from compiler_command import compiler_command, parse_compiler  # noqa: E402

RUN_ALL_ARGS = ["--cc", "{cc}"]
ROOT = Path(__file__).resolve().parents[1]

FIXTURE = r'''
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { uint64_t x[32]; } Ctx;

static int flag = 1;                 /* HrlField_HasEnteredFirstTime_Adult */
static int child = 1;                /* Sequence_IsChildhood: 1 child, 0 adult */
static int switch_on = 1;            /* HOS_OOT_FIRST_NIGHT_FIX */
static int event_playing;            /* the game's event manager has an active event */
static const char *map_name = "Market_Child";
static int cleared;
static int saved = 1;                /* the save's copy of the flag: only the game's own setter queues it */
static int writes;                   /* every write of the flag, a change or not */
static int logs;                     /* the repair's own log lines */
static int age_unreadable;           /* Sequence_IsChildhood not found */
static int setter_fails;             /* the game's Bool setter not found */

__attribute__((unused)) static int env_on_or(const char *name, int def) {
    if (!strcmp(name, "HOS_OOT_FIRST_NIGHT_FIX")) return switch_on;
    return def;
}
static void hos_log(const char *fmt, ...) { if (strstr(fmt, "first adult night")) logs++; }
static void rx_option_set(int id, int value) { (void)id; (void)value; }
static int oot_item_count(Ctx *c, const char *name) { (void)c; (void)name; return 0; }
static int oot_pouch_count(Ctx *c, const char *name) { (void)c; (void)name; return -1; }
/* (the flags service, game/flags.h) */
#define MGR 0x1000u
static uint64_t game_flags_mgr(void) { return MGR; }
static int game_flag_bool(uint64_t mgr, uint32_t hash, int *value) {
    if (mgr != MGR) return 0;
    if (hash == 0xcea220d3u) { if (age_unreadable) return 0; *value = child; return 1; }
    if (hash == 0x8592aa82u) { *value = flag; return 1; }
    return 0;
}
__attribute__((unused)) static int game_flag_set_bool(uint64_t mgr, uint32_t hash, int value) {
    if (mgr != MGR || hash != 0x8592aa82u) return 0;
    writes++;
    if (flag != value) cleared++;
    flag = value;
    return 1;
}
static int game_flag_text(uint64_t mgr, uint32_t hash, char *buf, size_t size) {
    if (mgr != MGR || hash != 0x1d6189dau) return 0;
    snprintf(buf, size, "%s", map_name);
    return 1;
}
/* (the game's own setter, game/oot_tools.c: queues the live value and the save's copy; NULL when it was called) */
__attribute__((unused)) static const char *oot_gamedata_queue_bool(Ctx *c, uint32_t hash, int value) {
    (void)c;
    if (hash != 0x8592aa82u) return "another flag";
    if (setter_fails) return "the game's Bool setter";
    writes++;
    if (flag != value) cleared++;
    flag = saved = value;
    return NULL;
}
__attribute__((unused)) static int oot_event_playing(Ctx *c) { (void)c; return event_playing; }
typedef struct { int unused; } SRWLOCK;
static SRWLOCK map_lock;
#define AcquireSRWLockExclusive(x) ((void)(x))
#define ReleaseSRWLockExclusive(x) ((void)(x))
static char map_now[100];
static void perf_pose_clear(void) {}

#define OOT_CHEAT_ITEM_BASE 100
#define OOT_CHEAT_AGE_ID 200
#define OOT_ITEM_COUNT 2
#define OOT_CHEAT_ITEM_PROBES "Item_DekuSeed"
static const char *const oot_item_names[OOT_ITEM_COUNT] = {"Item_A", "Item_B"};

// FIX

// REFRESH

static void check(const char *what, int want_flag, int want_cleared) {
    if (flag != want_flag || cleared != want_cleared) {
        fprintf(stderr, "FAIL: %s: flag %d (want %d), clears %d (want %d)\n", what, flag, want_flag, cleared, want_cleared);
        exit(1);
    }
}

static void check_count(const char *what, int got, int want) {
    if (got != want) {
        fprintf(stderr, "FAIL: %s: %d (want %d)\n", what, got, want);
        exit(1);
    }
}

int main(void) {
    Ctx c = {0};
    int64_t now = 0;

#ifdef SWITCH_OFF
    /* HOS_OOT_FIRST_NIGHT_FIX=0: the repair is off for the whole run. */
    flag = 1, child = 0, map_name = "HyruleField_Adult", switch_on = 0, cleared = 0;
    oot_inventory_refresh(&c, now);
    check("the switch off leaves the flag", 1, 0);
    check_count("the switch off writes nothing", writes, 0);
    check_count("the switch off logs nothing", logs, 0);
    check_count("the switch off leaves the save's copy", saved, 1);
    puts("PASS: HOS_OOT_FIRST_NIGHT_FIX=0 leaves the flag alone");
    return 0;
#else
    /* #2636's state: an adult save in Hyrule Field with the flag still set, loaded there. */
    flag = 1, child = 0, map_name = "HyruleField_Adult", switch_on = 1, cleared = 0;
    oot_inventory_refresh(&c, now);
    check("the flag is cleared in Hyrule Field", 0, 1);
    check_count("the clear is logged once", logs, 1);
    check_count("the save's copy is cleared too (a load does not bring the lock back)", saved, 0);

    /* #1064's state: the flag left set in a save that is in the Market, as a child. */
    now += 500000000LL;
    flag = 1, child = 1, map_name = "Market_Child", cleared = 0;
    oot_inventory_refresh(&c, now);
    check("the flag is cleared in the Market as a child", 0, 1);

    /* Anywhere else the warp songs reach. */
    now += 500000000LL;
    flag = 1, child = 0, map_name = "KakarikoVillage_Adult", cleared = 0;
    oot_inventory_refresh(&c, now);
    check("the flag is cleared in Kakariko", 0, 1);

    /* The lock's own place: adult in the Temple of Time (in the Market's map) and the ruined Market. */
    now += 500000000LL;
    flag = 1, child = 0, map_name = "Market_Child", cleared = 0;
    oot_inventory_refresh(&c, now);
    check("the flag is left alone in the Temple as adult", 1, 0);
    now += 500000000LL;
    map_name = "Market_Adult";
    oot_inventory_refresh(&c, now);
    check("the flag is left alone in the ruined Market", 1, 0);
    now += 500000000LL;
    map_name = "SageRoom";
    oot_inventory_refresh(&c, now);
    check("the flag is left alone in the Chamber of Sages", 1, 0);

    /* A save that is already clear is not written again. */
    now += 500000000LL;
    flag = 0, map_name = "HyruleField_Adult", cleared = 0, writes = 0, logs = 0;
    oot_inventory_refresh(&c, now);
    check("a clear flag is left clear", 0, 0);
    check_count("a clear flag is not written", writes, 0);
    check_count("a clear flag logs nothing", logs, 0);

    /* No map name this refresh: nothing to decide on. */
    now += 500000000LL;
    flag = 1, map_name = "", cleared = 0;
    oot_inventory_refresh(&c, now);
    check("an empty map leaves the flag", 1, 0);

    /* An event running (the field's arrival, which clears the flag itself) leaves it for a later refresh. */
    now += 500000000LL;
    flag = 1, map_name = "HyruleField_Adult", event_playing = 1, cleared = 0;
    oot_inventory_refresh(&c, now);
    check("an event running leaves the flag", 1, 0);

    /* The event over, the next refresh clears it. */
    now += 500000000LL;
    event_playing = 0, cleared = 0;
    oot_inventory_refresh(&c, now);
    check("after the event it clears", 0, 1);

    /* The age not readable: the lock's own maps keep the flag; the field does not need the age. */
    now += 500000000LL;
    flag = 1, child = 1, map_name = "Market_Child", age_unreadable = 1, cleared = 0;
    oot_inventory_refresh(&c, now);
    check("an unreadable age leaves the flag in the Market", 1, 0);
    now += 500000000LL;
    map_name = "HyruleField_Adult";
    oot_inventory_refresh(&c, now);
    check("an unreadable age still clears it in Hyrule Field", 0, 1);
    age_unreadable = 0;

    /* The setter not found (another game build): nothing is cleared, the refusal is logged once, not every refresh. */
    now += 500000000LL;
    flag = 1, setter_fails = 1, cleared = 0, logs = 0;
    oot_inventory_refresh(&c, now);
    now += 500000000LL;
    oot_inventory_refresh(&c, now);
    check("a missing setter clears nothing", 1, 0);
    check_count("a missing setter is logged once", logs, 1);

    puts("PASS: the first adult night's time lock is cleared everywhere but the Temple and the ruined Market");
    return 0;
#endif
}
'''


def cut(source, pattern, what):
    match = re.search(pattern, source, re.M | re.S)
    if not match:
        raise SystemExit("could not find %s in game/oot_tools.c" % what)
    return match.group()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cc", default=None)
    args = parser.parse_args()
    cc = parse_compiler(args.cc) if args.cc else compiler_command("CC", "gcc", "clang")
    host = (ROOT / "game" / "oot_tools.c").read_text(encoding="utf-8")
    refresh = cut(host, r"^static void oot_inventory_refresh\(Ctx \*c, int64_t now\) \{.*?^\}", "oot_inventory_refresh")
    fix = re.search(r"^static int oot_first_night_fix\(Ctx \*c, const char \*map\) \{.*?^\}", host, re.M | re.S)
    body = FIXTURE.replace("// FIX", fix.group() if fix else "").replace("// REFRESH", refresh)
    with tempfile.TemporaryDirectory(prefix="oot-first-night-") as tmp:
        source = Path(tmp) / "test.c"
        source.write_text(body, encoding="utf-8")
        for name, flags in (("on", []), ("off", ["-DSWITCH_OFF"])):
            binary = Path(tmp) / ("test-%s.exe" % name)
            subprocess.run(cc + ["-std=c11", "-Wall", "-Wextra", "-Werror"] + flags + [str(source), "-o", str(binary)],
                           check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    main()
