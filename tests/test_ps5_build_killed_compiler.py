"""ps5/build.py: a compiler that ends without a word is a failed compile.

A compiler the system kills for want of memory (three of the game's large files at once in a small container, or on a
small PC) writes nothing. compile_one gave back its output as the error, an empty text, and the build's loop took the
empty text for success: "0 failed", the object of an earlier build left in place, and a link that failed far from the
cause (duplicate symbols from an object of another translation) or, worse, did not fail.

compile_one is run as it is in the source, with a compiler that kills itself, one that fails with a message and one
that succeeds. No game, no SDK.

python -I -B tests/test_ps5_build_killed_compiler.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FAKE = """import os, signal, sys
how = os.environ["FAKE_CC"]
if how == "killed":
    os.kill(os.getpid(), signal.SIGKILL)
if how == "says":
    sys.stderr.write("x.c:1:1: error: nothing here\\n")
    sys.exit(1)
open(sys.argv[sys.argv.index("-o") + 1], "wb").write(b"object")
"""


def main():
    source = (ROOT / "ps5" / "build.py").read_text(encoding="utf-8")
    start = source.index("def compile_one(job):")
    function = source[start:source.index("\ndef main():", start)]
    failures = []
    with tempfile.TemporaryDirectory(prefix="ps5-build-killed-") as tmp:
        tmp = Path(tmp)
        fake = tmp / "cc.py"
        fake.write_text(FAKE, encoding="utf-8")
        src = tmp / "x.c"
        src.write_text("int x;\n", encoding="utf-8")
        names = {"os": os, "subprocess": subprocess, "shutil": shutil, "time": time, "SDK": str(tmp),
                 "key_of": lambda src, flags, digest: "key", "cc_wrapper": lambda cxx=False: [sys.executable, str(fake)]}
        exec(function, names)
        for how, failed in (("killed", True), ("says", True), ("works", False)):
            obj = tmp / (how + ".o")
            os.environ["FAKE_CC"] = how
            _, err, _ = names["compile_one"]((str(src), str(obj), [], ""))
            # (the build's loop: "if err:" is a failed file, anything else a compiled one)
            if bool(err) != failed:
                failures.append("a compiler that %s: the build takes it for %s (error text %r)"
                                % (how, "a compiled file" if failed else "a failed one", err))
            if failed and os.path.exists(str(obj) + ".key"):
                failures.append("a compiler that %s: the object is marked as built" % how)
            if not failed and not os.path.exists(str(obj) + ".key"):
                failures.append("a compiler that works: the object is not marked as built")
    for f in failures:
        print("FAIL:", f)
    if failures:
        return 1
    print("PASS: a compiler that ends without a word is a failed compile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
