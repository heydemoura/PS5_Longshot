"""GetModelInfo puts the tag's model fields in the SDK's order, so the series is not read as the figure's type.

The tag's Amiibo Identification Block at 0x54 is, as it is on the tag, [character 2 bytes][variant][figure type]
[model number 2 bytes, big endian][series][format version] (3dbrew). nn::nfp::ModelInfo is [character id 3 bytes]
[series][numbering id u16, native][nfp type] (Switchbrew; Ryujinx lays it out the same way). host/nn_nfp.c copied
0x54..0x5A in tag order, so ModelInfo[3] held the figure's type and ModelInfo[6] the series: the game read the
figure's type as the series id and only the per-character amiibo settings matched, rejecting every other figure
(#2489).

The real HLE function is cut from host/nn_nfp.c and compiled with stand-ins for guest memory and locking. Three
synthetic models whose type and series differ, and whose number's bytes differ, are read from the tag's order and
must come out in the SDK's: the character id (three bytes), the series, the model number as a native u16, the
figure's type. No tag dumps, keys or game files are used.

    python -I -B tests/test_nfc_model_info_fields.py   (needs a C compiler; CC overrides it)
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from compiler_command import parse_compiler
from srccut import block

FIXTURE = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef struct { uint64_t x[8]; } Ctx;
#define HLE(name) static void name(Ctx *c)
#define ARG(n) (c->x[n])
#define GP(a) ((void *)(uintptr_t)(a))
#define RET(v) (c->x[0] = (uint64_t)(v))
#define ST_MOUNTED 4
#define NFP_WRONG_STATE (115u | (73u << 9))
static int nfc_lk, dev_state;
static uint8_t tag[540];
static void AcquireSRWLockShared(int *lock) { (void)lock; }
static void ReleaseSRWLockShared(int *lock) { (void)lock; }
/* CUT */
int main(void) {
    /* tag[0x54..0x5a] in the tag's order: character 2, variant, figure type, model number 2 (big endian), series */
    static const uint8_t tags[][7] = {
        {0x01, 0x00, 0x00, 0x00, 0x03, 0x53, 0x09},   /* Link (Archer): type 0x00, number 0x0353, series 0x09 */
        {0x01, 0x0c, 0x00, 0x00, 0xa6, 0x04, 0x09},   /* Yunobo:        type 0x00, number 0xa604, series 0x09 */
        {0x01, 0x41, 0x00, 0x01, 0x5c, 0x03, 0x09}    /* a card:        type 0x01, number 0x5c03, series 0x09 */
    };
    int fails = 0;
    for (unsigned i = 0; i < sizeof(tags) / sizeof(tags[0]); i++) {
        const uint8_t *t = tags[i];
        uint8_t guarded[0x50], original[540];
        uint8_t *m = guarded + 8;
        Ctx c = {{(uint64_t)(uintptr_t)m}};
        memset(tag, 0x5a, sizeof tag);
        memcpy(tag + 0x54, t, 7);
        memcpy(original, tag, sizeof tag);
        memset(guarded, 0xa5, sizeof guarded);
        dev_state = ST_MOUNTED;
        _ZN2nn3nfp12GetModelInfoEPNS0_9ModelInfoERKNS0_12DeviceHandleE(&c);
        printf("case %u: tag 0x54..0x5a %02x %02x %02x %02x %02x %02x %02x -> ModelInfo %02x %02x %02x %02x %02x %02x %02x\n",
               i, t[0], t[1], t[2], t[3], t[4], t[5], t[6], m[0], m[1], m[2], m[3], m[4], m[5], m[6]);
        if (c.x[0] != 0) { printf("FAIL case %u: result 0x%llx\n", i, (unsigned long long)c.x[0]); fails++; continue; }
        if (memcmp(m, t, 3)) {
            printf("FAIL case %u: character id %02x %02x %02x, expected %02x %02x %02x\n", i, m[0], m[1], m[2], t[0], t[1], t[2]);
            fails++;
        }
        if (m[3] != t[6]) {
            printf("FAIL case %u: ModelInfo[3] series id is 0x%02x (tag[0x57], the figure type), expected 0x%02x (tag[0x5a], the series)\n",
                   i, m[3], t[6]);
            fails++;
        }
        uint16_t number;
        memcpy(&number, m + 4, sizeof number);
        const unsigned expected = (unsigned)t[4] * 256 + t[5];
        if (number != expected) {
            printf("FAIL case %u: model number 0x%04x, expected 0x%04x\n", i, number, expected);
            fails++;
        }
        if (m[6] != t[3]) {
            printf("FAIL case %u: ModelInfo[6] nfp type is 0x%02x (tag[0x5a], the series), expected 0x%02x (tag[0x57], the figure type)\n",
                   i, m[6], t[3]);
            fails++;
        }
        for (unsigned k = 7; k < 0x40; k++) if (m[k]) { printf("FAIL case %u: reserved byte %u not zero\n", i, k); fails++; }
        for (unsigned k = 0; k < sizeof guarded; k++)
            if ((k < 8 || k >= 8 + 0x40) && guarded[k] != 0xa5) { printf("FAIL case %u: wrote outside ModelInfo at %u\n", i, k); fails++; }
        if (memcmp(tag, original, sizeof tag)) { printf("FAIL case %u: the tag was changed\n", i); fails++; }
    }
    printf("%s: 3 synthetic models in the tag's order read in the SDK's (%u failed checks)\n", fails ? "FAIL" : "PASS", (unsigned)fails);
    return fails != 0;
}
"""


class NfcModelInfoFields(unittest.TestCase):
    def test_each_field_lands_where_the_sdk_puts_it(self):
        compiler = parse_compiler(os.environ.get("CC", ""))
        if not compiler:
            found = shutil.which("gcc") or shutil.which("clang")
            if not found:
                self.skipTest("no C compiler")
            compiler = [found]
        source = (ROOT / "host" / "nn_nfp.c").read_text(encoding="utf-8")
        function = block(source, "HLE(_ZN2nn3nfp12GetModelInfoEPNS0_9ModelInfoERKNS0_12DeviceHandleE)",
                         where="host/nn_nfp.c")
        scratch = ROOT / "work"
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="nfc-fields-", dir=scratch) as folder:
            folder = Path(folder)
            test = folder / "test.c"
            test.write_text(FIXTURE.replace("/* CUT */", function), encoding="utf-8")
            binary = folder / ("test.exe" if sys.platform == "win32" else "test")
            built = subprocess.run(compiler + ["-std=c11", "-Wall", "-Wextra", "-Werror", str(test), "-o", str(binary)],
                                   text=True, capture_output=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            run = subprocess.run([str(binary)], text=True, capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
