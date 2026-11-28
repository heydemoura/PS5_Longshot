"""(the cloth tests' helper) game/oot_cloth.c built in a test's folder over the test's own stand-ins, and what the cloth
tests share.

copy_glue(folder): the glue, its kernels (game/cloth_*) and their headers copied into the folder, with the two headers the
glue includes standing in: game_host.h includes the test's own hos.h (its guest memory, Ctx, a64_lookup, the log and the
clock), and sites.h declares what the glue takes from game/sites.h: HosSiteFn, hos_site_set, and the engine callbacks
game/sites.def names for the cloth's sites (so a callback whose name or signature is not the table's does not build).
SITE_SET is C for a fixture, after the glue: hos_site_set as hos_oot_cloth_init uses it (each name and callback recorded,
answered 1, or 0 for a name in site_missing), and at_end(name, c), which calls a recorded callback as the way out's hook
does (nothing when none was set: the site dormant). supports_x86_64_v2(cxx, folder): whether this CPU runs x86-64-v2 code.
"""
import os
import subprocess
from pathlib import Path

from sitecheck import site_lines

ROOT = Path(__file__).resolve().parents[1]
KERNELS = ("cloth_bend", "cloth_link", "cloth_bend_link", "cloth_anchor", "cloth_tether", "cloth_aabb", "cloth_cull",
           "cloth_matrices", "cloth_verlet", "cloth_capsule", "cloth_sphere", "cloth_skin", "cloth_bones")
SHARED = ("cloth_common.hpp", "cloth_wlane.hpp", "cloth_why.h")


def cloth_sites():
    """[(ident, name, engine callback or None)] of game/sites.def's cloth lines the glue answers (cloth.solve and
    cloth.time are game/oot_sites.c's)"""
    return [s for s in site_lines() if s[1].startswith("cloth.") and s[1] not in ("cloth.solve", "cloth.time")]


def sites_h():
    return ("#pragma once\n/* (a stand-in for game/sites.h: what game/oot_cloth.c takes from it) */\n"
            "typedef int (*HosSiteFn)(Ctx *A64_NOESC c);\n"
            "int hos_site_set(const char *name, HosSiteFn fn);\n" +
            "".join("int %s(Ctx *A64_NOESC c);\n" % fn for _, _, fn in cloth_sites() if fn))


def copy_glue(folder):
    folder = Path(folder)
    for f in ("oot_cloth.c",) + SHARED + tuple(k + ext for k in KERNELS for ext in (".h", ".cpp")):
        (folder / f).write_text((ROOT / "game" / f).read_text(encoding="utf-8"), encoding="utf-8")
    (folder / "game_host.h").write_text('#include "hos.h"   /* (host/game_host.h: the test\'s hos.h) */\n', encoding="utf-8")
    (folder / "sites.h").write_text(sites_h(), encoding="utf-8")


SITE_SET = r'''
/* game/sites.c's hos_site_set, as hos_oot_cloth_init uses it: the name and the callback recorded, 1 answered (0 for the
   name in site_missing: a site the host has no line for); at_end calls the callback a site at a way out was given */
static struct { const char *name; HosSiteFn fn; } site_set[32];
static int site_sets;
static const char *site_missing = "";
int hos_site_set(const char *name, HosSiteFn fn) {
    if (!strcmp(name, site_missing)) return 0;
#ifdef SITE_SLOT
    SITE_SLOT(name, fn);   /* (a fixture whose lowered hooks read the site's flag and slot) */
#endif
    if (site_sets < 32) site_set[site_sets].name = name, site_set[site_sets].fn = fn, site_sets++;
    return 1;
}
static HosSiteFn site_fn(const char *name) {
    for (int i = site_sets - 1; i >= 0; i--)
        if (!strcmp(site_set[i].name, name)) return site_set[i].fn;
    return NULL;
}
static void at_end(const char *name, Ctx *c) {
    const HosSiteFn f = site_fn(name);
    if (f && f(c) != 0) { fprintf(stderr, "FAIL: %s answered non-zero\n", name); exit(1); }
}
'''


def supports_x86_64_v2(cxx, tmp):
    """Probe CPUID in a baseline executable; a compiler accepting -march does not imply CPU support."""
    source = Path(tmp) / "cloth_cpu.cpp"
    exe = Path(tmp) / ("cloth_cpu.exe" if os.name == "nt" else "cloth_cpu")
    source.write_text('''#include <cpuid.h>
int main() {
    unsigned a, b, c, d;
    const unsigned need = (1u<<0)|(1u<<9)|(1u<<13)|(1u<<19)|(1u<<20)|(1u<<23);
    if (!__get_cpuid(1, &a, &b, &c, &d) || (c & need) != need) return 1;
    if (!__get_cpuid(0x80000001u, &a, &b, &c, &d) || !(c & 1)) return 1;
    return 0;
}
''', encoding="utf-8")
    built = subprocess.run(cxx + ["-march=x86-64", str(source), "-o", str(exe)], capture_output=True)
    return built.returncode == 0 and subprocess.run([str(exe)], capture_output=True).returncode == 0
