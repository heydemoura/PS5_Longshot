"""HOS_NATIVE_CLOTH: the cloth passes' hook sites (game/oot_cloth.c; game/sites/sites.toml, the build's locations.toml and
game/sites.def place them).

- The sites: every cloth site of game/sites.def is defined and located in the tables for the supported build, in the form
  and function the glue answers it in (the bending, anchor and Prepare Matrices passes an entry that can return; each
  loop a jump to its way out; the eleven cloth.*End sites at those ways out, where the jump goes, set only in check mode);
  each engine site's callback is defined in game/oot_cloth.c under the name sites.def gives it, and each End site's is
  given to it by hos_oot_cloth_init. Where in the game's code each site is placed (the signatures, each found once) is
  checked by cargo test -p sites and longshot-builder dev sites, not here.
- game/oot_cloth.c, built as it is with the kernels over a guest window of its own (and the solver's constants' page at
  0x84ca4000), the lifted routines stood in for by the hook's own answer (an exact lift) or a wrong one, a64_lookup
  answering those stand-ins for the routines' addresses:
  - the start (hos_oot_cloth_init): off and on set no site; check sets the eleven cloth.*End sites; one the host has no
    line for is named in the start line;
  - off (0 or off): every callback answers 0 and writes nothing; the lifted routines are not called;
  - on (unset or 1): each pass's call is answered with the kernel's bytes (the kernel run on a copy says what they are),
    the link, bend link and tether loops and the two whole routines (bending, anchor) with no register written (nothing
    is handed back: the site jumps to the loop's way out); FPSR is left as it was; the written pages marked; a record the
    kernel cannot take: the records before it are the host's and the callback answers 0 with the registers set for it
    (the loop passes: x9, x10, x11 for the loop's first record, the tethers' v7 too; the bending pass: the lifted routine
    called for the rest, x0 and x3 given back); the anchor
    pass is answered whole or declined whole (a NaN radius is done, a negative count declined); another float mode
    (FPCR) and memory past the window are left to the lifted code, untouched, whatever FPSR holds; a64_lookup with no
    routine at the address: the whole routines' callbacks answer 0 and write nothing;
  - check: the lifted code runs and stays; the host's copy is compared with it at the loop's way out (the loop passes,
    their cloth.*End callbacks) or after the lifted routine (the bending and anchor passes): an exact lift counts as
    compared, a byte or register that differs is a MISMATCH (logged once, counted); the link, bend link and tether loops
    and the whole routines are compared on what the routine or a caller can see (memory, FPSR, the callee-saved
    registers: NZCV or a scratch register off is not looked at);
    the anchor's slots are kept and put back around its kernel; records among the particles a call writes: not
    compared; the "oot: cloth" line every 10 s with the counts;
  - HOS_CLOTH_DECLINES=1 with HOS_CLOTH_CAPTURE (a folder) and HOS_CLOTH_CAPTURE_N=3: the hooks answer as without them;
    each declined call is put down to its reason (the kernel's own: a link value outside the link kernel's class, e.g.
    a NaN rest length, a subnormal stiffness or D under its 2^-74 floor, a scale it does not take, the tether and bend
    link kernels' stops, the bending kernel's; or the whole call's: the gate, memory, the anchor kernel's negative
    count) with the records it left to the lifted code; an "oot: cloth declines" line a pass with the "oot: cloth" line
    and at the end (hos_oot_cloth_end); the capture files hold one call a reason (a quarter of HOS_CLOTH_CAPTURE_N),
    each from its declined record (x0/x3 or x9 for the rest), with its regions and reason, read back here as the replay
    tool reads them.
    Without the switches nothing is counted or logged.
- The hooks as the lift lowers them (tests/sitehooks.py, `longshot-builder dev site-hooks`), in stand-ins for the link
  loop's function (cloth.links at the end of its set-up, cloth.linksEnd at its way out) and the bending routine's
  (cloth.bend first in it), over the glue's callbacks: on, a link loop done whole goes on at the way out without
  running the loop, one done in part runs the loop from the record left; the bending routine done whole or in part
  returns at once (in part after the glue ran the lifted routine for the records left, where the site, reached again,
  answers 0); off, both run as lifted; check, the link loop runs as lifted and the way out's site compares, the
  bending routine returns after the glue ran the lifted routine and compared it. (The lifted routine the glue calls is
  a stand-in without the lowered hook: sitehooks' wrappers share one mark of whether their function went on, which a
  hook reached again inside a callback overwrites.)

python -I -B tests/test_oot_cloth.py [--cc clang] [--cxx c++] [--cargo cargo]
"""
import argparse
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from compiler_command import parse_compiler  # noqa: E402
from cloth_glue import KERNELS, SITE_SET, cloth_sites, copy_glue  # noqa: E402
from sitecheck import engine_callback, load, site  # noqa: E402
import sitehooks  # noqa: E402

RUN_ALL_ARGS = ["--cc", "{clang}", "--cxx", "{cxx}", "--cargo", "{cargo}"]

ROOT = Path(__file__).resolve().parents[1]

# each cloth site the glue answers: its function, its form, whether it can return, and the site at the way out its jump
# goes to (check mode's compare there)
SITES = {
    "cloth.bend": ("f_81c7f880", "entry", True, None),
    "cloth.anchor": ("f_81c7bd00", "entry", True, None),
    "cloth.matrices": ("f_81cdff40", "entry", True, None),
    "cloth.links": ("f_81c86720", "jump", False, "cloth.linksEnd"),
    "cloth.bendLinks": ("f_81c7d2e0", "jump", False, "cloth.bendLinksEnd"),
    "cloth.tethers": ("f_81c88940", "jump", False, "cloth.tethersEnd"),
    "cloth.boxes": ("f_81b38800", "jump", False, "cloth.boxesEnd"),
    "cloth.cull": ("f_81b5a3c0", "jump", False, "cloth.cullEnd"),
    "cloth.verlet": ("f_81c66a60", "jump", False, "cloth.verletEnd"),
    "cloth.capsule": ("f_81b8c000", "jump", False, "cloth.capsuleRecordsEnd"),
    "cloth.capsuleTail": ("f_81b8c000", "jump", False, "cloth.capsuleEnd"),
    "cloth.sphere": ("f_81c1a240", "jump", False, "cloth.sphereRecordsEnd"),
    "cloth.skin": ("f_81db9a40", "jump", False, "cloth.skinEnd"),
    "cloth.bones": ("f_81ce6500", "jump", False, "cloth.bonesEnd"),
}


def check_sites():
    glue = (ROOT / "game" / "oot_cloth.c").read_text(encoding="utf-8")
    ends = {e for _, _, _, e in SITES.values() if e}
    named = {n for _, n, _ in cloth_sites()}
    assert named == set(SITES) | ends, "game/sites.def's cloth sites are not the glue's: %s" % sorted(named ^ (set(SITES) | ends))
    table = {x.name: x for x in load(sitehooks.BUILD)}
    init = glue[glue.index("void hos_oot_cloth_init(void)"):]
    for name, (function, at, can_return, end) in SITES.items():
        x = site(name, function, at, can_return=can_return)   # (defined, located, and its HOS_SITE line)
        fn = engine_callback(x.ident)
        assert fn and re.search(r"^int %s\(Ctx \*A64_NOESC c\) \{" % fn, glue, re.M), \
            "%s: its callback %s is not defined in game/oot_cloth.c" % (name, fn)
        if end is None:
            assert x.to is None, name
            continue
        e = site(end, function, "before")
        assert table[end].inside in (name, x.inside) and e.offset == x.to, "%s is not at %s's way out (%#x, %s)" % (end, name, e.offset or 0, x.to)
        assert engine_callback(e.ident) is None, "%s has an engine callback: check mode sets it" % end
        m = re.search(r'\{"%s", (\w+)\}' % re.escape(end), init)
        assert m and re.search(r"^static int %s\(Ctx \*A64_NOESC c\) \{" % m.group(1), glue, re.M), \
            "%s: hos_oot_cloth_init does not give it a callback of game/oot_cloth.c" % end
    assert len(re.findall(r'\{"cloth\.\w+End", \w+\}', init)) == len(ends)
    print("PASS: the %d cloth sites of game/sites.def: defined and located for the supported build, each in its function and "
          "form, the eleven cloth.*End sites at their jumps' ways out; each engine callback defined in game/oot_cloth.c, each End "
          "site's given by hos_oot_cloth_init" % len(named))


HOS_H = r'''
#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#define A64_NOESC
typedef struct { uint64_t x[32]; uint8_t n, z, c, v; uint64_t vreg[32][2]; uint64_t fpcr, fpsr; } Ctx;
typedef void (*A64Fn)(Ctx *);
A64Fn a64_lookup(uint64_t addr);
#define WINDOW 0x400000ull   /* (4 MB of guest memory, and the constants' page at 0x84ca4000) */
#define TABLE_PAGE 0x84ca4000ull
extern uint8_t *window, table_page[0x1000];
extern uint8_t marks[WINDOW >> 12];
static inline void *hos_guest_range(uint64_t a, uint64_t n) {
    if (a >= TABLE_PAGE && a < TABLE_PAGE + 0x1000) return n <= TABLE_PAGE + 0x1000 - a ? table_page + (a - TABLE_PAGE) : (void *)0;
    return a < WINDOW && n <= WINDOW - a ? (void *)(window + a) : (void *)0;
}
void test_written(const void *p, size_t n);
#define HOS_WRITTEN(p, n) test_written((p), (n))
void hos_log(const char *fmt, ...);
int64_t hos_now_ns(void);
'''

FIXTURE = r'''
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include "hos.h"
#include "oot_cloth.c"   /* (its statics: the answers stand in for an exact lift, the counts are read) */
/* (the sites the lowered hooks in run_sites read: the link loop's and the bending routine's on, the way out's set by
   hos_oot_cloth_init in check mode, through hos_site_set below) */
static int site_none(Ctx *A64_NOESC c) { (void)c; return 0; }
volatile uint8_t hos_site_on_cloth_links = 1, hos_site_on_cloth_bend = 1, hos_site_on_cloth_linksEnd = 0;
int (*volatile hos_site_fn_cloth_links)(Ctx *) = hos_site_cloth_links;
int (*volatile hos_site_fn_cloth_bend)(Ctx *) = hos_site_cloth_bend;
int (*volatile hos_site_fn_cloth_linksEnd)(Ctx *) = site_none;
#define SITE_SLOT(name, fn) \
    if (!strcmp((name), "cloth.linksEnd")) hos_site_fn_cloth_linksEnd = (fn) ? (fn) : site_none, hos_site_on_cloth_linksEnd = (fn) != NULL
''' + SITE_SET + r'''
uint8_t *window, table_page[0x1000];
uint8_t marks[WINDOW >> 12];
static int64_t now_ns = 1;
static int logged_first, logged_lines, dx_lines;
static char last_line[2048], dx_last[K_COUNT][2048], cap_line[2048], all_lines[8192], start_line[2048];
int64_t hos_now_ns(void) { return now_ns; }
void test_written(const void *p, size_t n) {
    const size_t a = (size_t)((const uint8_t *)p - window);
    if ((const uint8_t *)p < window || a + n > WINDOW) { fprintf(stderr, "FAIL: a mark outside the window\n"); exit(1); }
    for (size_t q = a >> 12; q <= (a + n - 1) >> 12; q++) marks[q] = 1;
}
void hos_log(const char *fmt, ...) {
    char line[2048];
    va_list ap;
    va_start(ap, fmt), vsnprintf(line, sizeof line, fmt, ap), va_end(ap);
    if (strstr(line, "MISMATCH (the first)")) logged_first++;
    if (strstr(line, "oot: cloth (HOS_NATIVE_CLOTH=")) {   /* (the counts: one line or more, whole passes each) */
        logged_lines++, strcpy(last_line, line);
        if (strlen(line) > 1023) { fprintf(stderr, "FAIL: a count line over the logger's 1023 characters\n"); exit(1); }
        if (strlen(all_lines) + strlen(line) + 2 < sizeof all_lines) strcat(all_lines, line), strcat(all_lines, "\n");
    }
    for (int k = 0; k < K_COUNT; k++)   /* (HOS_CLOTH_DECLINES: each pass's last line) */
        if (!strncmp(line, "oot: cloth declines ", 20) && !strncmp(line + 20, cloth_name[k], strlen(cloth_name[k])) &&
            line[20 + strlen(cloth_name[k])] == ':')
            strcpy(dx_last[k], line), dx_lines++;
    if (strstr(line, "oot: cloth capture: ")) strcpy(cap_line, line);
    if (strstr(line, "oot: cloth: the host has no site")) strcpy(start_line, line);
    printf("  log: %.300s\n", line);
}
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "FAIL:%d %s\n", __LINE__, #x); exit(1); } } while (0)
static uint32_t fbits_(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
static void put32(uint64_t a, uint32_t v) { memcpy(window + a, &v, 4); }
static void put64(uint64_t a, uint64_t v) { memcpy(window + a, &v, 8); }
static void putf(uint64_t a, float f) { memcpy(window + a, &f, 4); }
static void put16(uint64_t a, uint16_t v) { memcpy(window + a, &v, 2); }

#undef CONST   /* (windows.h, which game/oot_cloth.c includes on Windows, defines a macro of this name) */
enum { OBJ = 0x1000, TH = 0x2000, KP = 0x2010, X4A = 0x2020, CONST = 0x3000, SKIN = 0x5000, AOBJ = 0x6000, REC = 0x10000,
       SLOTS = 0x100000, PDATA = 0x200000, VERTS = 0x300000, SIDE = 8, NP = SIDE * SIDE };
#define SPAN (0x10000u * 16u)
static uint8_t *want_slots, *before;

static void setup(void) {
    memset(window, 0, WINDOW), memset(table_page, 0, sizeof table_page), memset(marks, 0, sizeof marks);
    const uint64_t cst = CONST;
    memcpy(table_page + 0x680, &cst, 8);
    putf(CONST + 0x60, 1.0f), putf(CONST + 0x64, 1.0f);
    for (int q = 0; q < 4; q++) put32(CONST + 0x270 + 4 * q, 0x34800000u);   /* (a small value: the hook reads it) */
    for (int i = 0; i < NP; i++) {   /* a gently folded 8 x 8 sheet, 0.5 apart; the first row pinned */
        const float x = (float)(i % SIDE) * 0.5f, y = (float)(i / SIDE) * 0.5f;
        const float p[4] = {x + 0.01f * (float)(i % 3), y * 0.9f, 0.2f * y + 0.003f * (float)(i % 5), 1.0f};
        memcpy(window + SLOTS + 16 * i, p, 16);
        putf(PDATA + 16 * i + 4, i < SIDE ? 0.0f : 1.0f);
    }
}
static Ctx ctx0(void) { Ctx c; memset(&c, 0, sizeof c); c.fpsr = 0x10; for (int r = 0; r < 31; r++) c.x[r] = 0x1111ull * (uint64_t)r; return c; }
static void begin(void) { memcpy(want_slots, window + SLOTS, SPAN), memset(marks, 0, sizeof marks); }
static int marked(uint64_t a) { return marks[a >> 12]; }

/* ---- the passes' calls ---- */
static uint32_t link_recs(uint32_t n) {   /* structural links along the rows, rest 0.55 */
    for (uint32_t i = 0; i < n; i++) {
        const uint16_t a = (uint16_t)(i + i / (SIDE - 1)), b = (uint16_t)(a + 1);
        put16(REC + 12 * i, a), put16(REC + 12 * i + 2, b), putf(REC + 12 * i + 4, 0.55f), putf(REC + 12 * i + 8, 0.7f);
    }
    put64(OBJ + 0x28, REC);
    return n;
}
static Ctx link_ctx(uint32_t n) {
    Ctx c = ctx0();
    c.x[9] = n, c.x[20] = OBJ, c.x[23] = SLOTS, c.x[24] = PDATA, c.vreg[0][0] = fbits_(0.9f);
    return c;
}
static ClothLinkPass link_want(uint32_t n) {
    ClothLinkPass b;
    memset(&b, 0, sizeof b);
    b.slots = want_slots, b.particles = window + PDATA, b.links = window + REC, b.n = n, b.scale = fbits_(0.9f);
    b.fpsr_in = CLOTH_FPSR_IN;
    cloth_link_pass(&b);
    return b;
}
static void bend_link_recs(uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        const uint16_t a = (uint16_t)(i % (NP - 2 * SIDE)), b = (uint16_t)(a + 2 * SIDE);
        put16(REC + 20 * i, a), put16(REC + 20 * i + 2, b);
        putf(REC + 20 * i + 4, 0.8f), putf(REC + 20 * i + 8, 0.9f), putf(REC + 20 * i + 12, 0.5f), putf(REC + 20 * i + 16, 0.5f);
    }
    put64(OBJ + 0x28, REC);
}
static void tether_recs(uint32_t n) {
    for (uint32_t i = 0; i < n; i++)
        put16(REC + 12 * i, 0), put16(REC + 12 * i + 2, (uint16_t)(i + 1)), putf(REC + 12 * i + 4, 0.3f * (float)(i + 1) * 0.5f),
        putf(REC + 12 * i + 8, 0.6f);
    put64(OBJ + 0x28, REC);
}
static void bend_recs(uint32_t n) {
    for (uint32_t i = 0; i < n; i++) {
        const uint16_t a = (uint16_t)(i % (SIDE - 1) + SIDE * (i / (SIDE - 1))), s[4] = {a, (uint16_t)(a + SIDE + 1), (uint16_t)(a + 1), (uint16_t)(a + SIDE)};
        const float w[6] = {0.25f, 0.25f, -0.25f, -0.25f, 0.5f, 0.1f};
        memcpy(window + REC + 32 * i, w, 24), memcpy(window + REC + 32 * i + 24, s, 8);
    }
    putf(TH, 0.01f), putf(TH + 4, 0.01f), putf(KP, 0.5f);
}
static Ctx bend_ctx(uint32_t n) {
    Ctx c = ctx0();
    c.x[0] = n, c.x[1] = SLOTS, c.x[2] = PDATA, c.x[3] = REC, c.x[4] = TH, c.x[5] = KP;
    return c;
}
static void anchor_setup(uint32_t n) {
    put64(AOBJ + 0x28, REC), put32(AOBJ + 0x30, n), put64(AOBJ + 0x38, 0), put32(AOBJ + 0x40, 0), putf(AOBJ + 0x6c, 1.0f);
    putf(X4A, 0.5f), putf(X4A + 4, 0.25f);
    put64(SKIN + 0x18, VERTS), window[SKIN + 0x24] = 16;
    const float m[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0.125f, 0.25f, 0.375f, 1};
    memcpy(window + SKIN + 0x90, m, 64);
    for (uint32_t i = 0; i < n; i++) {
        float p[4];
        memcpy(p, window + SLOTS + 16 * i, 16);
        const float v[4] = {p[0] - 0.125f + 0.2f, p[1] - 0.25f - 0.1f, p[2] - 0.375f + 0.05f, 1.0f};
        memcpy(window + VERTS + 16 * i, v, 16);
        put16(REC + 16 * i, (uint16_t)i), put16(REC + 16 * i + 2, (uint16_t)i), putf(REC + 16 * i + 4, 0.05f);
    }
}
static Ctx anchor_ctx(void) {
    Ctx c = ctx0();
    c.x[0] = AOBJ, c.x[1] = SLOTS, c.x[3] = SKIN, c.x[4] = X4A, c.x[5] = 0;
    return c;
}

/* ---- the stand-ins for the lifted routines: an exact lift (the hook's own answer), or one that is wrong ---- */
static int lifted_calls, lifted_bad;   /* (lifted_bad: 1 a byte, 2 a callee-saved register, 3 NZCV alone, 4 a scratch register) */
static uint64_t lifted_x0, lifted_x3;
static void spoil(Ctx *c, uint8_t *slot) {
    if (lifted_bad == 1) slot[3] ^= 1;
    if (lifted_bad == 2) c->x[19] ^= 4;
    if (lifted_bad == 3) c->n ^= 1;
    if (lifted_bad == 4) c->vreg[5][1] ^= 4;
}
static void lifted_bend(Ctx *c) {
    lifted_calls++, lifted_x0 = c->x[0], lifted_x3 = c->x[3];
    CHECK(hos_site_cloth_bend(c) == 0);   /* (the site first in the routine, reached again from the callback: answers 0) */
    if (mode_is_check) { uint32_t d; CHECK(bend_answer(c, NULL, &d) == 1); spoil(c, window + SLOTS + 16 * 9); }
}
static void lifted_anchor(Ctx *c) {
    lifted_calls++;
    CHECK(hos_site_cloth_anchor(c) == 0);
    if (mode_is_check) { ClothAnchorResult r; CHECK(anchor_answer(c, &r) == 1); spoil(c, window + SLOTS + 16 * 2); }
}
/* the lifted functions with the sites' hooks as the lift lowers them (run_sites): the link loop's function from the end of
   its set-up to past its way out, the loop an exact lift in check mode; the bending routine, its body an exact lift in
   check mode */
static int loop_runs, body_runs;
static uint64_t loop_x9, loop_x10, body_x0, body_x3;
void f_81c86720(Ctx *__restrict A64_NOESC c) {
    c->x[11] = 0x7fffff;
@LINKS@
    loop_runs++, loop_x9 = c->x[9], loop_x10 = c->x[10];
    if (mode_is_check) { uint32_t d; CHECK(link_answer(c, NULL, &d) == 1); }
way_out:;
@LINKS_END@
    c->x[25] = 0x2525;   /* (on past the way out) */
}
void f_81c7f880(Ctx *__restrict A64_NOESC c) {
@BEND@
    body_runs++, body_x0 = c->x[0], body_x3 = c->x[3];
    if (mode_is_check) { uint32_t d; CHECK(bend_answer(c, NULL, &d) == 1); }
}
/* a64_lookup: the stand-ins at the whole routines' addresses (none: a build with no lifted routine there). (Not
   f_81c7f880 above: sitehooks' wrappers share one mark of whether the function went on, which a lowered hook reached again
   inside a callback would overwrite; the site's answer when reached again is checked in lifted_bend) */
static int lookup_none;
A64Fn a64_lookup(uint64_t a) {
    if (lookup_none) return NULL;
    return a == OOT_CLOTH_BEND_FN ? lifted_bend : a == OOT_CLOTH_ANCHOR_FN ? lifted_anchor : NULL;
}
/* the registers of two contexts the same (the whole-function hooks write none) */
static int regs_same(const Ctx *a, const Ctx *b) {
    return !memcmp(a->x, b->x, sizeof a->x) && !memcmp(a->vreg, b->vreg, sizeof a->vreg) && a->n == b->n && a->z == b->z &&
           a->c == b->c && a->v == b->v && a->fpcr == b->fpcr;
}
/* the anchor kernel run directly on the window (its bytes into want_slots, the window as it was): its result */
static ClothAnchorResult anchor_want(void) {
    memcpy(before, window, WINDOW);
    ClothAnchorCall k = {AOBJ, SLOTS, SKIN, X4A, 0, 0, CLOTH_FPSR_IN, cloth_range};
    ClothAnchorResult r;
    memset(&r, 0, sizeof r);
    CHECK(cloth_anchor_81c7bd00(&k, &r) == 1);
    memcpy(want_slots, window + SLOTS, SPAN), memcpy(window, before, WINDOW), memset(marks, 0, sizeof marks);
    return r;
}
'''

FIXTURE_MAIN = r'''
static void run_off(void) {
    setup(), begin();
    const uint32_t n = link_recs(40);
    memcpy(before, window, WINDOW);
    Ctx c = link_ctx(n);
    CHECK(hos_site_cloth_links(&c) == 0);
    bend_link_recs(30), c = link_ctx(30);
    CHECK(hos_site_cloth_bend_links(&c) == 0);
    tether_recs(20), c = link_ctx(20);
    CHECK(hos_site_cloth_tethers(&c) == 0);
    bend_recs(20), c = bend_ctx(20);
    CHECK(hos_site_cloth_bend(&c) == 0);
    anchor_setup(30), c = anchor_ctx();
    CHECK(hos_site_cloth_anchor(&c) == 0);
    at_end("cloth.linksEnd", &c);
    CHECK(!memcmp(before + SLOTS, window + SLOTS, SPAN) && lifted_calls == 0);
    puts("PASS: off: every callback answers 0 and writes nothing");
}

static void run_on(void) {
    /* no lifted routine at the whole routines' addresses (a64_lookup NULL): their callbacks answer 0, nothing written */
    setup(), bend_recs(SIDE - 1), memcpy(before, window, WINDOW), lookup_none = 1;
    Ctx c0 = bend_ctx(SIDE - 1), c = c0;
    CHECK(hos_site_cloth_bend(&c) == 0 && regs_same(&c, &c0) && c.fpsr == c0.fpsr && !memcmp(before, window, WINDOW));
    setup(), anchor_setup(40), memcpy(before, window, WINDOW), c0 = anchor_ctx(), c = c0;
    CHECK(hos_site_cloth_anchor(&c) == 0 && regs_same(&c, &c0) && c.fpsr == c0.fpsr && !memcmp(before, window, WINDOW));
    CHECK(lifted_calls == 0 && cloth_local[K_BEND][S_CALLS] == 0 && cloth_local[K_ANCHOR][S_CALLS] == 0);
    lookup_none = 0;
    puts("PASS: on: no lifted routine at the bending or anchor routine's address: the callbacks answer 0, nothing written");

    /* the link loop: answered whole, no register written (the routine reads none of what the loop leaves) */
    setup();
    uint32_t n = link_recs(SIDE * (SIDE - 1));
    begin();
    ClothLinkPass w = link_want(n);
    CHECK(w.done == n);
    c0 = link_ctx(n), c = c0;
    CHECK(hos_site_cloth_links(&c) == 1);
    CHECK(!memcmp(window + SLOTS, want_slots, SPAN) && marked(SLOTS) && !marked(PDATA) && c.fpsr == 0x10);
    CHECK(regs_same(&c, &c0));
    puts("PASS: on: the link loop is answered with the kernel's bytes and no register written, FPSR as it was, its pages marked");
    /* a NaN in link 5: links 0-4 the host's, the callback answers 0 with the loop's registers at link 5 */
    setup(), n = link_recs(SIDE * (SIDE - 1)), put32(REC + 12 * 5 + 4, 0x7fc00000u), begin();
    w = link_want(n);
    CHECK(w.done == 5);
    c = link_ctx(n);
    CHECK(hos_site_cloth_links(&c) == 0 && c.x[9] == n - 5 && c.x[10] == REC + 4 + 60 && c.x[11] == 0x7fffff);
    CHECK(!memcmp(window + SLOTS, want_slots, SPAN) && c.fpsr == 0x10);
    puts("PASS: on: a NaN in a link: the links before it are the host's, the callback answers 0 with the loop at it (x9, x10, x11)");
    struct { const char *why; uint64_t fpcr, x23; } no[] = {
        {"default NaN", 1u << 25, SLOTS}, {"round toward zero", 3u << 22, SLOTS}, {"the host's inhibit bit", 1ull << 32, SLOTS},
        {"particles past the window", 0, WINDOW - 0x1000},
    };
    for (unsigned i = 0; i < sizeof no / sizeof no[0]; i++) {
        setup(), n = link_recs(20), memcpy(before, window, WINDOW);
        c0 = link_ctx(n), c0.fpcr = no[i].fpcr, c0.x[23] = no[i].x23, c = c0;
        if (hos_site_cloth_links(&c) != 0 || memcmp(before, window, WINDOW) || memcmp(&c, &c0, sizeof c)) {
            fprintf(stderr, "FAIL: answered for %s\n", no[i].why);
            exit(1);
        }
    }
    /* FPSR does not gate (the lifted code raises no cumulative flag): any FPSR is answered and left as it was */
    static const uint64_t fpsrs[2] = {0, 0x9f};
    for (int f = 0; f < 2; f++) {
        setup(), n = link_recs(20), begin(), link_want(n), c = link_ctx(n), c.fpsr = fpsrs[f];
        CHECK(hos_site_cloth_links(&c) == 1 && c.fpsr == fpsrs[f] && !memcmp(window + SLOTS, want_slots, SPAN));
    }
    setup(), n = link_recs(20), begin(), c = link_ctx(n), c.fpcr = 1u << 24;
    CHECK(hos_site_cloth_links(&c) == 1 && c.fpsr == 0x10);   /* (FZ: answered, FPSR as it was) */
    puts("PASS: on: another float mode and memory past the window are left to the lifted loop, untouched; FPSR does not gate "
         "and is left as it was (FZ too)");

    /* the bend link and tether loops */
    setup(), bend_link_recs(40), begin();
    ClothBendLinkPass bl;
    memset(&bl, 0, sizeof bl);
    bl.slots = want_slots, bl.particles = window + PDATA, bl.links = window + REC, bl.n = 40, bl.scale = fbits_(0.9f);
    bl.fpsr_in = CLOTH_FPSR_IN;   /* (the clamp pair: the constants' +0x50, zero here) */
    cloth_bend_link_pass(&bl);
    CHECK(bl.done == 40);
    c0 = link_ctx(40), c = c0;
    CHECK(hos_site_cloth_bend_links(&c) == 1 && !memcmp(window + SLOTS, want_slots, SPAN) && regs_same(&c, &c0) && c.fpsr == 0x10);
    CHECK(marked(SLOTS));
    /* a huge max length at link 2: links 0-1 the host's, the callback answers 0 with x9, x10, x11 and v16 at link 2 */
    setup(), bend_link_recs(40), putf(REC + 20 * 2 + 8, 1e30f), begin();
    memset(&bl, 0, sizeof bl);
    bl.slots = want_slots, bl.particles = window + PDATA, bl.links = window + REC, bl.n = 40, bl.scale = fbits_(0.9f);
    bl.fpsr_in = CLOTH_FPSR_IN;
    cloth_bend_link_pass(&bl);
    CHECK(bl.done == 2);
    c = link_ctx(40), c.vreg[16][0] = 0x5555, c.vreg[16][1] = 0x5555;
    CHECK(hos_site_cloth_bend_links(&c) == 0 && c.x[9] == 38 && c.x[10] == REC + 40 && c.x[11] == 0x7fffff);
    CHECK(c.vreg[16][0] == 0 && c.vreg[16][1] == 0 && !memcmp(window + SLOTS, want_slots, SPAN));
    setup(), tether_recs(30), begin();
    ClothTetherCall tc = {want_slots, window + REC, 30, fbits_(0.9f), 0, CLOTH_FPSR_IN};
    ClothTetherResult tr;
    cloth_tether_81c88940(&tc, &tr);
    CHECK(tr.done == 30);
    c0 = link_ctx(30), c = c0;
    CHECK(hos_site_cloth_tethers(&c) == 1 && !memcmp(window + SLOTS, want_slots, SPAN) && regs_same(&c, &c0) && c.fpsr == 0x10);
    CHECK(marked(SLOTS));
    setup(), tether_recs(30), put32(REC + 12 * 7 + 8, 0x00000001u), begin();
    cloth_tether_81c88940(&tc, &tr);
    CHECK(tr.done == 7);
    c = link_ctx(30), c.vreg[7][0] = c.vreg[7][1] = 0x7777;
    CHECK(hos_site_cloth_tethers(&c) == 0 && c.x[9] == 23 && c.x[10] == REC + 4 + 12 * 7 && c.x[11] == 0x7fffff);
    CHECK(c.vreg[7][0] == 0 && c.vreg[7][1] == 0 && !memcmp(window + SLOTS, want_slots, SPAN));
    puts("PASS: on: the bend link and tether loops are answered with the kernels' bytes and no register written; at a value "
         "they do not take the callback answers 0 with the loop's registers there");

    /* the bending pass: whole, then an infinite weight in record 3 (the lifted routine does the rest, x0 and x3 given
       back); no register written */
    setup(), bend_recs(SIDE - 1), begin();
    ClothBendPass bb;
    memset(&bb, 0, sizeof bb);
    float k = 0.5f;
    bb.slots = want_slots, bb.particles = window + PDATA, bb.records = window + REC, bb.scale = (const uint8_t *)&k, bb.n = SIDE - 1;
    bb.threshold[0] = bb.threshold[1] = fbits_(0.01f), bb.numerator[0] = bb.numerator[1] = fbits_(1.0f), bb.fpsr_in = CLOTH_FPSR_IN;
    cloth_bend_pass(&bb);
    CHECK(bb.done == SIDE - 1);
    c0 = bend_ctx(SIDE - 1);
    c = c0;
    CHECK(hos_site_cloth_bend(&c) == 1 && lifted_calls == 0 && !memcmp(window + SLOTS, want_slots, SPAN));
    CHECK(regs_same(&c, &c0) && c.fpsr == c0.fpsr && marked(SLOTS));
    setup(), bend_recs(SIDE - 1), put32(REC + 32 * 3, 0x7f800000u), begin(), c = bend_ctx(SIDE - 1);
    CHECK(hos_site_cloth_bend(&c) == 1 && lifted_calls == 1 && lifted_x0 == SIDE - 1 - 3 && lifted_x3 == REC + 96);
    CHECK(c.x[0] == SIDE - 1 && c.x[3] == REC);
    c0 = bend_ctx(0), c = c0;
    CHECK(hos_site_cloth_bend(&c) == 1 && regs_same(&c, &c0) && c.fpsr == c0.fpsr);   /* (a count below 1: nothing) */
    puts("PASS: on: the bending pass is answered whole, no register written; at a record it cannot take, the lifted routine "
         "does the rest");

    /* the anchor pass: answered whole (a NaN radius included: the kernel takes every value), no register written; a float
       mode the kernel does not take, a negative count or memory past the window: declined whole, nothing written */
    setup(), anchor_setup(40);
    ClothAnchorResult ar = anchor_want();
    CHECK(ar.records == 40 && memcmp(window + SLOTS, want_slots, SPAN));
    c0 = anchor_ctx(), c = c0, lifted_calls = 0;
    CHECK(hos_site_cloth_anchor(&c) == 1 && !memcmp(window + SLOTS, want_slots, SPAN) && lifted_calls == 0);
    CHECK(regs_same(&c, &c0) && c.fpsr == c0.fpsr && marked(SLOTS));
    setup(), anchor_setup(40), put32(REC + 16 * 4 + 4, 0x7fc00000u);
    ar = anchor_want();
    c = anchor_ctx();
    CHECK(hos_site_cloth_anchor(&c) == 1 && !memcmp(window + SLOTS, want_slots, SPAN) && lifted_calls == 0);
    for (int i = 0; i < 3; i++) {
        setup(), anchor_setup(40), memcpy(before, window, WINDOW), c0 = anchor_ctx();
        if (i == 0) c0.fpcr = 1u << 25;
        if (i == 1) put32(AOBJ + 0x30, 0xffffffffu), memcpy(before, window, WINDOW);
        if (i == 2) put64(SKIN + 0x18, WINDOW - 0x100), memcpy(before, window, WINDOW);
        c = c0;
        CHECK(hos_site_cloth_anchor(&c) == 0 && !memcmp(before, window, WINDOW) && regs_same(&c, &c0) && c.fpsr == c0.fpsr);
    }
    puts("PASS: on: the anchor pass is answered whole (a NaN radius too), no register written; another float mode, a negative "
         "count, memory past the window: declined whole, nothing written");
    cloth_flush();
    CHECK(cloth_total[K_LINK][S_ALL] == 4 && cloth_total[K_LINK][S_PART] == 1 && cloth_total[K_LINK][S_GATE] == 3);
    CHECK(cloth_total[K_BEND][S_PART] == 1 && cloth_total[K_TETHER][S_PART] == 1);
    CHECK(cloth_total[K_ANCHOR][S_ALL] == 2 && cloth_total[K_ANCHOR][S_PART] == 0 && cloth_total[K_ANCHOR][S_DECLINED] == 3 &&
          cloth_total[K_ANCHOR][S_GATE] == 1 && cloth_total[K_ANCHOR][S_REC_NATIVE] == 80);
    puts("PASS: on: the counts (native, partly, lifted and why)");
    CHECK(cloth_dx_now == 0 && dx_lines == 0 && !cap_line[0] && cloth_dn.has == 0);
    for (int q = 0; q < K_COUNT; q++) CHECK(cloth_dx_tab[q].calls == 0 && cloth_dx_tab[q].nkeys == 0);
    hos_oot_cloth_end();
    CHECK(dx_lines == 0);
    puts("PASS: on without HOS_CLOTH_DECLINES: no decline is counted, noted or logged");
}

/* ---- HOS_CLOTH_DECLINES=1, HOS_CLOTH_CAPTURE=<folder>, HOS_CLOTH_CAPTURE_N=3 (the test's environment) ---- */
static ClothDxKey *find_key(int k, const char *what, int lane, int cls) {
    for (int i = 0; i < cloth_dx_tab[k].nkeys; i++) {
        ClothDxKey *e = &cloth_dx_tab[k].key[i];
        if (!strcmp(e->why.what, what) && e->why.lane == lane && e->why.cls == cls) return e;
    }
    fprintf(stderr, "FAIL: no reason \"%s\" lane %d %s in %s; there are:\n", what, lane, cloth_why_class_name(cls), cloth_name[k]);
    for (int i = 0; i < cloth_dx_tab[k].nkeys; i++)
        fprintf(stderr, "  %s lane %d %s\n", cloth_dx_tab[k].key[i].why.what, cloth_dx_tab[k].key[i].why.lane,
                cloth_why_class_name(cloth_dx_tab[k].key[i].why.cls));
    exit(1);
}
static uint64_t fnv(const uint8_t *p, size_t n) {
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < n; i++) h = (h ^ p[i]) * 1099511628211ull;
    return h;
}

static void run_declines(void) {
    CHECK(cloth_dx() == 1 && !strcmp(cloth_cap_dir, getenv("HOS_CLOTH_CAPTURE")) && cloth_cap_max == 3);
    /* the link loop, a NaN rest length at link 5, three times: the same answer as without the switch; the kernel's
       reason (a link value outside its class), three calls, 3 x 51 records; one captured (a quarter of
       HOS_CLOTH_CAPTURE_N a reason) */
    uint32_t n = 0;
    Ctx c, c0;
    const char *const link_class = label_why_name[CLOTH_LINK_CLASS];
    for (int rep = 0; rep < 3; rep++) {
        setup(), n = link_recs(SIDE * (SIDE - 1)), put32(REC + 12 * 5 + 4, 0x7fc00000u), begin();
        CHECK(link_want(n).done == 5);
        c = link_ctx(n);
        CHECK(hos_site_cloth_links(&c) == 0 && c.x[9] == n - 5 && c.x[10] == REC + 4 + 60 && c.x[11] == 0x7fffff);
        CHECK(!memcmp(window + SLOTS, want_slots, SPAN));
        if (!rep) printf("EXPECT link slots %016llx\n", (unsigned long long)fnv(window + SLOTS + 16 * 5, 16 * 59));
    }
    ClothDxKey *e = find_key(K_LINK, link_class, -1, CW_KERNEL);
    CHECK(e->calls == 3 && e->records == 3 * (long)(n - 5) && e->captured == 1);
    /* a subnormal stiffness at link 0 and D = B - A of 2^-80 at link 0 (under the kernel's 2^-74 floor): the same
       reason, each call declined whole after the kernel ran, nothing written; not captured again */
    setup(), n = link_recs(20), put32(REC + 8, 0x00000003u), memcpy(before, window, WINDOW), c0 = link_ctx(n), c = c0;
    CHECK(hos_site_cloth_links(&c) == 0 && regs_same(&c, &c0) && !memcmp(before, window, WINDOW));
    setup(), n = link_recs(20), memset(window + SLOTS, 0, 32), putf(SLOTS + 16, 0x1p-80f), c = link_ctx(n);
    CHECK(hos_site_cloth_links(&c) == 0);
    CHECK(e->calls == 5 && e->records == 3 * (long)(SIDE * (SIDE - 1) - 5) + 40 && e->captured == 1);
    /* a scale the kernel does not take (subnormal): the call declined whole, a reason of its own, captured */
    setup(), n = link_recs(20), c0 = link_ctx(n), c0.vreg[0][0] = 1, c = c0;
    CHECK(hos_site_cloth_links(&c) == 0 && regs_same(&c, &c0));
    e = find_key(K_LINK, label_why_name[CLOTH_LINK_ENTRY], -1, CW_KERNEL);
    CHECK(e->calls == 1 && e->records == 20 && e->captured == 1 && cloth_dx_tab[K_LINK].captured == 2);
    /* whole calls before the kernel: counted, not captured */
    setup(), n = link_recs(20), c = link_ctx(n), c.fpcr = 1u << 25;
    CHECK(hos_site_cloth_links(&c) == 0);
    setup(), n = link_recs(20), c = link_ctx(n), c.x[23] = WINDOW - 0x1000;
    CHECK(hos_site_cloth_links(&c) == 0);
    CHECK(find_key(K_LINK, "gate: FPCR", -1, CW_CALL)->records == 20);
    CHECK(find_key(K_LINK, "memory: particle slots (1 MB)", -1, CW_CALL)->calls == 1);
    /* all native: counted, not declined */
    setup(), n = link_recs(20), c = link_ctx(n);
    CHECK(hos_site_cloth_links(&c) == 1);
    CHECK(cloth_dx_tab[K_LINK].calls == 9 && cloth_dx_tab[K_LINK].declined == 8 && cloth_dx_tab[K_LINK].nkeys == 4);
    puts("PASS: declines: the link loop's reasons (the kernel's own; the gate; memory), the same answers as without");

    /* the other passes: a subnormal stiffness at tether 7, a huge max length at bend link 2 (the kernels' own reasons),
       an infinite weight at bend record 3 (the bending kernel's), the anchor's whole calls (the gate, the kernel's
       negative count) */
    setup(), tether_recs(30), put32(REC + 12 * 7 + 8, 1u), c = link_ctx(30);
    CHECK(hos_site_cloth_tethers(&c) == 0 && c.x[9] == 23);
    CHECK(find_key(K_TETHER, tether_why_name[CLOTH_TETHER_VALUE], -1, CW_KERNEL)->records == 23);
    setup(), bend_link_recs(40), putf(REC + 20 * 2 + 8, 1e30f), c = link_ctx(40);
    CHECK(hos_site_cloth_bend_links(&c) == 0 && c.x[9] == 38);
    CHECK(find_key(K_BEND_LINK, label_why_name[CLOTH_BEND_LINK_CLASS], -1, CW_KERNEL)->records == 38);
    lifted_calls = 0;
    setup(), bend_recs(SIDE - 1), put32(REC + 32 * 3, 0x7f800000u), c = bend_ctx(SIDE - 1);
    CHECK(hos_site_cloth_bend(&c) == 1 && lifted_calls == 1 && lifted_x0 == SIDE - 1 - 3 && c.x[0] == SIDE - 1);
    CHECK(find_key(K_BEND, bend_why_name[CLOTH_BEND_CLASS], -1, CW_KERNEL)->records == SIDE - 1 - 3);
    setup(), anchor_setup(40), put32(REC + 16 * 4 + 4, 0x7fc00000u), c = anchor_ctx();
    CHECK(hos_site_cloth_anchor(&c) == 1);   /* (a NaN radius: done whole) */
    setup(), anchor_setup(40), c = anchor_ctx(), c.fpcr = 1u << 25;
    CHECK(hos_site_cloth_anchor(&c) == 0);
    CHECK(find_key(K_ANCHOR, "gate: FPCR", -1, CW_CALL)->calls == 1);
    setup(), anchor_setup(40), put32(AOBJ + 0x30, 0xffffffffu), c = anchor_ctx();
    CHECK(hos_site_cloth_anchor(&c) == 0);
    CHECK(find_key(K_ANCHOR, "kernel: a negative record count", -1, CW_CALL)->calls == 1);
    CHECK(cloth_dx_tab[K_ANCHOR].calls == 3 && cloth_dx_tab[K_ANCHOR].declined == 2 && cloth_dx_tab[K_ANCHOR].records == 40);
    for (int k = 0; k < K_COUNT; k++) CHECK(cloth_dx_tab[k].cap_failed == 0 && cloth_dx_tab[k].cap_skipped == 0);
    CHECK(cloth_dx_tab[K_TETHER].captured == 1 && cloth_dx_tab[K_BEND_LINK].captured == 1 && cloth_dx_tab[K_BEND].captured == 1);
    CHECK(cloth_dx_tab[K_ANCHOR].captured == 0);
    puts("PASS: declines: the tether, bend link, bending and anchor passes' reasons (the kernels' own; the anchor's "
         "whole calls, counted, not captured)");

    /* the lines: with the "oot: cloth" line every 10 s, and at the end of the run */
    cloth_flush();   /* (the first starts the clock) */
    now_ns += CLOTH_LOG_NS * 2;
    cloth_flush();
    CHECK(strstr(dx_last[K_LINK], "oot: cloth declines link f_81c86720: calls 9 (declined 8), records 288, lifted 253 (") &&
          strstr(dx_last[K_LINK], "; a link value outside the exactness class (kernel): calls 5, records 193 (") &&
          strstr(dx_last[K_LINK], "; an entry value outside what it takes (kernel): calls 1, records 20 (6.9 %)"));
    CHECK(strstr(dx_last[K_TETHER], "; a value it cannot give the game's bits for (kernel): calls 1, records 23 ("));
    CHECK(strstr(dx_last[K_BEND], "; a record value outside the exactness class (kernel): calls 1, records 4 (") &&
          strstr(dx_last[K_ANCHOR], "; kernel: a negative record count call: calls 1"));
    CHECK(strstr(cap_line, "bend f_81c7f880 1 (skipped 0), link f_81c86720 2 (skipped 0)"));
    const int before = dx_lines;
    hos_oot_cloth_end();
    CHECK(dx_lines == before + K_COUNT);
    puts("PASS: declines: an oot: cloth declines line a pass every 10 s and at the end, by records");
    printf("EXPECT bend x0 %d x3 %llu\n", SIDE - 1 - 3, (unsigned long long)(REC + 96));
}

static void run_check(void) {
    /* the link loop: the callback answers 0 (the lifted loop runs), the way out's site compares on what the routine can
       see after the loop: a byte or a callee-saved register off is a MISMATCH, NZCV or a scratch register is not looked at */
    for (int bad = 0; bad <= 4; bad++) {
        setup(), link_recs(SIDE * (SIDE - 1)), begin();
        Ctx c = link_ctx(SIDE * (SIDE - 1)), c0 = c;
        CHECK(hos_site_cloth_links(&c) == 0 && !memcmp(&c, &c0, sizeof c) && !memcmp(window + SLOTS, want_slots, SPAN));
        CHECK(cloth_pending.k == K_LINK);
        uint32_t d;
        CHECK(link_answer(&c, NULL, &d) == 1);   /* (the lifted loop, exactly) */
        lifted_bad = bad, spoil(&c, window + SLOTS + 16 * 12), lifted_bad = 0;
        at_end("cloth.linksEnd", &c);
        CHECK(cloth_pending.k == -1);
        at_end("cloth.linksEnd", &c);   /* (once only) */
    }
    cloth_flush();
    CHECK(cloth_total[K_LINK][S_CHECKED] == 5 && cloth_total[K_LINK][S_MISMATCH] == 2 &&
          logged_first == 1);
    puts("PASS: check: the link loop runs lifted, its way out compares: an exact lift passes, a byte or a callee-saved register "
         "off is a MISMATCH, NZCV or a scratch register off is not");
    /* links among the particles the call writes: not compared */
    setup(), link_recs(20);
    memcpy(window + SLOTS + 16 * 4, window + REC, 12 * 20), put64(OBJ + 0x28, SLOTS + 16 * 4);   /* (slots 4-18: within 0-22) */
    Ctx c = link_ctx(20);
    CHECK(hos_site_cloth_links(&c) == 0 && cloth_pending.k == -1);
    cloth_flush();
    CHECK(cloth_total[K_LINK][S_UNCHECKED] == 1);
    puts("PASS: check: links stored among the particles the call writes are not compared (counted)");
    /* the bend link and tether loops: an exact lift passes */
    setup(), bend_link_recs(40), c = link_ctx(40);
    CHECK(hos_site_cloth_bend_links(&c) == 0);
    uint32_t d;
    CHECK(bend_link_answer(&c, NULL, &d) == 1);
    at_end("cloth.bendLinksEnd", &c);
    setup(), tether_recs(30), c = link_ctx(30);
    CHECK(hos_site_cloth_tethers(&c) == 0);
    CHECK(tether_answer(&c, NULL, &d) == 1);
    at_end("cloth.tethersEnd", &c);
    cloth_flush();
    CHECK(cloth_total[K_BEND_LINK][S_CHECKED] == 1 && cloth_total[K_TETHER][S_CHECKED] == 1);
    CHECK(cloth_total[K_BEND_LINK][S_MISMATCH] == 0 && cloth_total[K_TETHER][S_MISMATCH] == 0);
    /* the bending and anchor passes: the lifted routine called by the callback, compared after it on what a caller can
       see: a byte or a callee-saved register off is a MISMATCH, NZCV or a scratch register is not looked at */
    lifted_calls = 0;
    for (int bad = 0; bad <= 4; bad++) {
        setup(), bend_recs(SIDE - 1), c = bend_ctx(SIDE - 1), lifted_bad = bad;
        CHECK(hos_site_cloth_bend(&c) == 1);
        setup(), anchor_setup(40), memcpy(before, window, WINDOW), c = anchor_ctx();
        CHECK(hos_site_cloth_anchor(&c) == 1);
    }
    lifted_bad = 0;
    cloth_flush();
    CHECK(lifted_calls == 10);
    CHECK(cloth_total[K_BEND][S_CHECKED] == 5 && cloth_total[K_BEND][S_MISMATCH] == 2);
    CHECK(cloth_total[K_ANCHOR][S_CHECKED] == 5 && cloth_total[K_ANCHOR][S_MISMATCH] == 2);
    puts("PASS: check: the bend link, tether, bending and anchor passes compared, on memory, FPSR and the callee-saved "
         "registers only");
    /* the anchor's kernel writes guest memory itself: check mode keeps the slots and puts them back before the lifted
       routine, so the lifted routine starts from the game's bytes (a stand-in that sees the kernel's bytes fails) */
    setup(), anchor_setup(40), c = anchor_ctx();
    {
        ClothCopy cp = {0};
        ClothAnchorResult r;
        memcpy(before, window, WINDOW);
        CHECK(anchor_check(&c, &cp, &r) == 1 && !memcmp(before, window, WINDOW) && r.records == 40);
        CHECK(cp.sparse && memcmp(cp.buf + 16 * 3, window + SLOTS + 16 * 3, 16));   /* (the kernel's bytes in the copy) */
    }
    puts("PASS: check: the anchor's slots kept and put back around its kernel: the lifted routine starts from the game's bytes");
    now_ns += CLOTH_LOG_NS * 2;
    cloth_flush();
    for (int k = 0; k < K_COUNT; k++) CHECK(strstr(all_lines, cloth_name[k]));
    CHECK(logged_lines == 3 && strstr(all_lines, "link f_81c86720: calls 6 (native 5") && strstr(last_line, "MISMATCHES in all 6"));
    puts("PASS: check: the oot: cloth lines (three, each under 1023 characters), every pass with its counts and the mismatches");
}

/* the hooks as the lift lowers them, over the glue's callbacks (see the docstring) */
static void run_sites(void) {
    const int mode = cloth_mode_read();
    setup();
    uint32_t n = link_recs(SIDE * (SIDE - 1));
    begin(), memcpy(before, window, WINDOW);
    CHECK(link_want(n).done == n);
    Ctx c = link_ctx(n);
    f_81c86720(&c);
    CHECK(c.x[25] == 0x2525 && !memcmp(window + SLOTS, mode == CLOTH_OFF ? before + SLOTS : want_slots, SPAN));
    if (mode == CLOTH_ON) CHECK(loop_runs == 0 && c.x[9] == n && c.x[10] == link_ctx(n).x[10]);   /* (nothing handed back) */
    else CHECK(loop_runs == 1 && loop_x9 == n && loop_x10 == link_ctx(n).x[10]);
    if (mode == CLOTH_CHECK) {
        cloth_flush();
        CHECK(cloth_total[K_LINK][S_CHECKED] == 1 && cloth_total[K_LINK][S_MISMATCH] == 0 && cloth_pending.k == -1);
    }
    if (mode != CLOTH_CHECK) {   /* (a NaN in link 5: a call done in part, which check mode does not compare) */
        setup(), n = link_recs(SIDE * (SIDE - 1)), put32(REC + 12 * 5 + 4, 0x7fc00000u), begin(), link_want(n);
        c = link_ctx(n), loop_runs = 0;
        f_81c86720(&c);
        CHECK(loop_runs == 1 && c.x[25] == 0x2525);
        if (mode == CLOTH_ON) CHECK(loop_x9 == n - 5 && loop_x10 == REC + 4 + 60 && !memcmp(window + SLOTS, want_slots, SPAN));
        else CHECK(loop_x9 == n);
    }
    setup(), bend_recs(SIDE - 1), begin(), c = bend_ctx(SIDE - 1);
    f_81c7f880(&c);
    CHECK(body_runs == (mode == CLOTH_OFF) && (mode != CLOTH_OFF || body_x0 == SIDE - 1));
    CHECK(lifted_calls == (mode == CLOTH_CHECK) && (mode != CLOTH_ON || (memcmp(window + SLOTS, want_slots, SPAN) && marked(SLOTS))));
    if (mode == CLOTH_ON) {   /* (in part: the glue runs the lifted routine for the rest, and the function returns) */
        setup(), bend_recs(SIDE - 1), put32(REC + 32 * 3, 0x7f800000u), begin(), c = bend_ctx(SIDE - 1);
        f_81c7f880(&c);
        CHECK(body_runs == 0 && lifted_calls == 1 && lifted_x0 == SIDE - 1 - 3 && lifted_x3 == REC + 96);
        CHECK(c.x[0] == SIDE - 1 && c.x[3] == REC);
    }
    if (mode == CLOTH_CHECK) {
        cloth_flush();
        CHECK(cloth_total[K_BEND][S_CHECKED] == 1 && cloth_total[K_BEND][S_MISMATCH] == 0);
    }
    printf("PASS: the hooks as the lift lowers them (%s): the link loop's jump and way out, the bending routine's entry\n",
           mode == CLOTH_OFF ? "off" : mode == CLOTH_ON ? "on" : "check");
}

int main(int argc, char **argv) {
    const char *mode = argc > 1 ? argv[1] : "off";
    window = (uint8_t *)calloc(1, WINDOW), want_slots = (uint8_t *)calloc(1, SPAN), before = (uint8_t *)calloc(1, WINDOW);
    CHECK(window && want_slots && before);
    mode_is_check = cloth_mode_read() == CLOTH_CHECK;
    /* the start: check mode's eleven sites at the ways out, and no site in the other modes */
    hos_oot_cloth_init();
    static const char *const ends[11] = {"cloth.linksEnd", "cloth.bendLinksEnd", "cloth.tethersEnd", "cloth.boxesEnd",
                                         "cloth.cullEnd", "cloth.verletEnd", "cloth.capsuleRecordsEnd", "cloth.capsuleEnd",
                                         "cloth.sphereRecordsEnd", "cloth.skinEnd", "cloth.bonesEnd"};
    CHECK(site_sets == (mode_is_check ? 11 : 0) && hos_site_on_cloth_linksEnd == mode_is_check);
    for (int i = 0; i < 11; i++) CHECK(!site_fn(ends[i]) == !mode_is_check);
    if (mode_is_check) {   /* (a site the host has no line for: named in the start line, the others set as before) */
        site_sets = 0, site_missing = "cloth.cullEnd";
        hos_oot_cloth_init();
        CHECK(site_sets == 10 && !site_fn("cloth.cullEnd") && cloth_end_missing_n == 1 && !strcmp(cloth_end_missing, "cloth.cullEnd"));
        CHECK(cloth_mode() == CLOTH_CHECK && strstr(start_line, "the host has no site for 1 of check mode's compares (the first: "
                                                                "cloth.cullEnd)"));
    }
    printf("PASS: the start (%s): %s\n", mode, mode_is_check ? "the eleven cloth.*End sites set; one the host lacks named in the "
                                                               "start line" : "no site set");
    if (!strcmp(mode, "off")) run_off();
    else if (!strcmp(mode, "on")) run_on();
    else if (!strcmp(mode, "declines")) run_declines();
    else if (!strcmp(mode, "sites")) run_sites();
    else run_check();
    return 0;
}
'''


def lowered(fixture):
    """the fixture with the three sites' hooks as the lift lowers them (sitehooks.line) in its stand-in functions"""
    links = site("cloth.links", "f_81c86720", "jump")
    end = site("cloth.linksEnd", "f_81c86720", "before")
    bend = site("cloth.bend", "f_81c7f880", "entry", can_return=True)
    assert sitehooks.line(bend).count("return;") == 1 and sitehooks.GOTO in sitehooks.line(links)
    return (fixture.replace("@LINKS@", sitehooks.line(links).replace(sitehooks.GOTO, "goto way_out;"))
            .replace("@LINKS_END@", sitehooks.line(end)).replace("@BEND@", sitehooks.line(bend)))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cc", default=os.environ.get("CC", "clang"))
    p.add_argument("--cxx", default=os.environ.get("CXX", "c++"))
    p.add_argument("--cargo")
    a = p.parse_args()
    check_sites()
    cc = parse_compiler(a.cc)
    cxx = parse_compiler(a.cxx)
    if not cc or not shutil.which(cc[0]) or not cxx or not shutil.which(cxx[0]):
        print("SKIP: no C or C++ compiler")
        return
    sitehooks.use(cc[0], a.cargo)
    with tempfile.TemporaryDirectory(prefix="oot-cloth-") as tmp:
        tmp = Path(tmp)
        copy_glue(tmp)
        (tmp / "hos.h").write_text(HOS_H, encoding="utf-8")
        fixture = lowered(FIXTURE.replace("static int lifted_calls", "static int mode_is_check;\nstatic int lifted_calls")
                          + FIXTURE_MAIN)
        (tmp / "fixture.c").write_text(fixture, encoding="utf-8")
        native = ["-march=native"] if subprocess.run(cc + ["-march=native", "-x", "c", "-c", "-o", os.devnull, "-"], input="int x;\n",
                                                     capture_output=True, text=True).returncode == 0 else []
        objs = []
        for k in KERNELS:
            o = tmp / (k + ".o")
            subprocess.run(cxx + ["-std=c++17", "-fno-exceptions", "-fno-rtti", "-O2", "-ffp-contract=off", "-Wall", "-Wextra",
                            "-Werror"] + native + ["-c", str(tmp / (k + ".cpp")), "-o", str(o)], check=True)
            objs.append(str(o))
        fo = tmp / "fixture.o"
        # (oot_cloth.c is built inside the fixture: -include longshot_env.h as the build's host files get env_on)
        subprocess.run(cc + ["-std=gnu11", "-O1", "-Wall", "-Wextra", "-Werror", "-Wno-unused-function", "-ffp-contract=off",
                        "-include", str(ROOT / "gpu" / "vk" / "compat" / "longshot_env.h"), "-I", str(tmp)] + native +
                       ["-c", str(tmp / "fixture.c"), "-o", str(fo)], check=True)
        exe = tmp / ("fixture.exe" if os.name == "nt" else "fixture")
        subprocess.run(cxx + [str(fo)] + objs + sitehooks.objects(tmp, fixture) + ([] if os.name == "nt" else ["-pthread"]) +
                       ["-o", str(exe)], check=True)
        env = {k: v for k, v in os.environ.items() if k != "HOS_NATIVE_CLOTH" and not k.startswith("HOS_CLOTH_")}
        cap = tmp / "capture"
        for mode, value in (("on", None), ("off", "0"), ("off", "off"), ("on", "1"), ("check", "check"), ("declines", "1"),
                            ("sites", None), ("sites", "0"), ("sites", "check")):
            run_env = dict(env) if value is None else dict(env, HOS_NATIVE_CLOTH=value)
            if mode == "declines":
                run_env.update(HOS_CLOTH_DECLINES="1", HOS_CLOTH_CAPTURE=str(cap), HOS_CLOTH_CAPTURE_N="3")
            r = subprocess.run([str(exe), mode], env=run_env, capture_output=True, text=True)
            print("\n".join(line for line in r.stdout.splitlines() if line.startswith("PASS")))
            if r.returncode:
                raise SystemExit("FAIL (HOS_NATIVE_CLOTH=%s, %s):\n%s%s" % (value, mode, r.stdout, r.stderr))
            if mode == "declines":
                check_capture(cap, r.stdout)


def fnv(data):
    h = 1469598103934665603
    for b in data:
        h = ((h ^ b) * 1099511628211) & (2 ** 64 - 1)
    return h


def read_capture(path):
    """A HOS_CLOTH_CAPTURE file (game/oot_cloth.c, cloth_cap_write): (version, pass, name, cases)"""
    data = path.read_bytes()
    assert data[:8] == b"OOTCLCAP", path
    ver, pas = struct.unpack_from("<II", data, 8)
    name = data[16:40].rstrip(b"\0").decode()
    at, cases = 40, []
    while at < len(data):
        assert data[at:at + 4] == b"CASE", (path, at)
        nreg, n, done = struct.unpack_from("<3I", data, at + 4)
        meta = struct.unpack_from("<4Q", data, at + 16)
        why = data[at + 48:at + 144].rstrip(b"\0").decode()
        x = struct.unpack_from("<32Q", data, at + 144)
        nzcv, fpcr, fpsr, _ = struct.unpack_from("<4I", data, at + 912)
        at += 928
        regions = []
        for _ in range(nreg):
            a, ln, role = struct.unpack_from("<QII", data, at)
            regions.append((a, role, data[at + 16:at + 16 + ln]))
            at += 16 + ln
        cases.append(dict(n=n, done=done, meta=meta, why=why, x=x, nzcv=nzcv, fpcr=fpcr, fpsr=fpsr, regions=regions))
    return ver, pas, name, cases


def check_capture(cap, out):
    """The declines run's capture files against what the fixture did (its EXPECT lines)"""
    expect = dict((line.split()[1], line.split()[2:]) for line in out.splitlines() if line.startswith("EXPECT "))
    REC, OBJ, SLOTS = 0x10000, 0x1000, 0x100000
    files = sorted(p.name for p in cap.iterdir())
    assert files == ["cloth_81c7d2e0.bin", "cloth_81c7f880.bin", "cloth_81c86720.bin", "cloth_81c88940.bin"], files
    ver, pas, name, link = read_capture(cap / "cloth_81c86720.bin")
    assert (ver, pas, name) == (1, 1, "link f_81c86720") and len(link) == 2, (ver, pas, name, len(link))
    c = link[0]
    link_class = "a link value outside the exactness class (kernel)"
    assert (c["n"], c["done"], c["why"], c["x"][9], c["meta"][:2], c["fpsr"]) == (56, 5, link_class, 51, (REC + 60, 51), 0x10), c
    roles = {r: (a, b) for a, r, b in c["regions"]}
    assert sorted(roles) == [1, 2, 3, 4], roles.keys()
    assert roles[3][0] == REC + 60 and len(roles[3][1]) == 12 * 51
    assert roles[1][0] == SLOTS + 16 * 5 and len(roles[1][1]) == 16 * 59 and "%016x" % fnv(roles[1][1]) == expect["link"][1]
    assert roles[4] == (OBJ + 0x28, struct.pack("<Q", REC)), roles[4]
    assert link[1]["why"] == "an entry value outside what it takes (kernel)" and link[1]["done"] == 0, link[1]
    _, _, _, bend = read_capture(cap / "cloth_81c7f880.bin")
    b = bend[0]
    assert len(bend) == 1 and b["why"] == "a record value outside the exactness class (kernel)", b
    assert b["x"][0] == int(expect["bend"][1]) and b["x"][3] == int(expect["bend"][3])
    assert b["meta"][0] == REC + 96 and sorted(r for _, r, _ in b["regions"]) == [1, 2, 3, 5, 6, 7, 8]
    _, _, _, tether = read_capture(cap / "cloth_81c88940.bin")
    assert (len(tether), tether[0]["why"], tether[0]["x"][9]) == (1, "a value it cannot give the game's bits for (kernel)", 23)
    _, _, _, bl = read_capture(cap / "cloth_81c7d2e0.bin")
    assert (len(bl), bl[0]["why"], bl[0]["x"][9]) == (1, link_class, 38)
    assert sorted(r for _, r, _ in bl[0]["regions"]) == [1, 2, 3, 4, 7, 8]
    print("PASS: declines: the capture files (HOS_CLOTH_CAPTURE_N=3, a quarter a reason): each declined call from its declined "
          "record, its registers (x0/x3, x9 for the rest), regions and reason")


if __name__ == "__main__":
    main()
