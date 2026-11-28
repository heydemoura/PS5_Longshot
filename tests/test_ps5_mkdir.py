"""The console's folders are made for every user (host/ps5/longshot_ps5.c hos_ps5_mkdir, ps5/build.py MKDIR).

The title runs as a user of its own there. A folder it made 0755 (CreateDirectoryA) or 0700 (the bug reports, the crash
record) could be listed and not emptied by the FTP server or a file manager: a player could not delete a cache.

python -I -B tests/test_ps5_mkdir.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    build = (ROOT / "ps5" / "build.py").read_text(encoding="utf-8")
    glue = (ROOT / "host" / "ps5" / "longshot_ps5.c").read_text(encoding="utf-8")
    at = glue.find("int hos_ps5_mkdir(const char *path, mode_t mode) {")
    body = glue[at:glue.find("\n}\n", at)] if at >= 0 else ""
    checks = [
        ('MKDIR = "-Dmkdir=hos_ps5_mkdir"' in build, "the build does not send mkdir to the console's own"),
        (bool(re.search(r"host_fl = COMMON \+ (?:DIRTY_PAGES \+ )?\[MKDIR,", build)), "the host's sources do not take it"),
        (bool(re.search(r"vfl = COMMON \+ \[MKDIR,", build)), "the renderer's sources do not take it"),
        (not re.search(r"^COMMON = .*MKDIR", build, re.M), "the game's own code would be built again for it"),
        ("mkdir(path, 0777)" in body, "the folder is not made for every user"),
        ("errno == EEXIST" in body and "chmod(path, 0777)" in body, "a folder made before is not opened"),
        (0 <= glue.find("#undef mkdir") < at, "the console's mkdir would call itself"),
    ]
    bad = [why for ok, why in checks if not ok]
    if bad:
        raise SystemExit("FAIL: " + "; ".join(bad))
    print("PASS: the console's folders are made, and opened, for every user")


if __name__ == "__main__":
    main()
