/* host/ps5/nup_romfs.c on the PC against nx2/nup.py: ps5/tests/nup_romfs_check.sh <your game.nup> */
#include "nup_romfs.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
    if (!nup_romfs_open(argv[1])) return 1;
    for (int i = 2; i < argc; i++) {   /* each path: its size and the bytes to stdout files */
        char p[1024]; snprintf(p, sizeof p, NUP_ROMFS_ROOT "/%s", argv[i]);
        int64_t h = nup_romfs_open_file(p);
        if (h < 0) { printf("MISSING %s\n", argv[i]); continue; }
        uint64_t n = nup_romfs_size(h); char *b = malloc(n + 1);
        int64_t got = nup_romfs_pread(h, b, n, 0);
        char out[1100]; snprintf(out, sizeof out, "%s/out_%d", getenv("OUTDIR"), i);
        FILE *f = fopen(out, "wb"); fwrite(b, 1, got, f); fclose(f);
        printf("%s %llu %lld\n", argv[i], (unsigned long long)n, (long long)got);
        nup_romfs_close(h); free(b);
    }
    int d; uint64_t s; int64_t node = nup_romfs_find(NUP_ROMFS_ROOT "/Shader", &d, &s);
    uint32_t cur = 0; const char *nm; size_t nl; int isdir; int count = 0;
    while (nup_romfs_dir_next(node, &cur, &nm, &nl, &isdir, &s)) count++;
    printf("Shader/ entries %d (dir %d)\n", count, d);
    printf("nonexistent: %lld\n", (long long)nup_romfs_find(NUP_ROMFS_ROOT "/nope/x", 0, 0));
    int bad = 0;   /* each path again with every slash doubled, as the game names its voices (#10): the same node */
    for (int i = 2; i < argc; i++) {
        char p[1024], q[2100]; snprintf(p, sizeof p, NUP_ROMFS_ROOT "/%s", argv[i]);
        size_t o = snprintf(q, sizeof q, NUP_ROMFS_ROOT "/");
        for (const char *c = argv[i]; *c && o + 2 < sizeof q; c++) { if (*c == '/') q[o++] = '/'; q[o++] = *c; }
        q[o] = 0;
        const int64_t a = nup_romfs_find(p, 0, 0), b2 = nup_romfs_find(q, 0, 0);
        if (a != b2) { printf("DOUBLE SLASH %s: %lld, not %lld\n", q, (long long)b2, (long long)a); bad++; }
    }
    printf("double slashes: %d of %d paths differ\n", bad, argc - 2);
    return bad != 0;
}
