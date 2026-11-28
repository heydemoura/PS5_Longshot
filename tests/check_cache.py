"""The checks' compiles cached by what they compile, as the build's objects are (nx2/object_store.py): many checks compile a
small program from the source at every run, in every worktree, and those compiles were a third of the checks' time (on
Windows, 2970 s of check time cold and 1886 s warm, 425 of 434 compiles from the cache). tests/run_all.py
starts each check through tests/check_cache_boot.py, which routes the check's compiler calls here; its own C++ checks come
here too.

A compile is keyed by:
- the invoked compiler, its target and its real path, size and time;
- its arguments, with the paths of its inputs, its output and its include folders left out;
- each source file as the preprocessor gives it (`-E -P` with the same flags), so a changed header anywhere is a new key, as
  in ccache's preprocessor mode;
- each other input file (an object, an archive) by its bytes;
- the environment variables a compiler reads paths from.

A compile it can't key that way goes to the compiler untouched: preprocessing diagnostics, compiler scripts and unknown
wrappers, named libraries, no `-o`, `-x`, `-E`, `-M*` dependency files, `-save-temps`,
profiling, coverage, response files (`@file`), standard input, an input that isn't there, `-L`, `-B`, a sysroot or a linker
named by its path (what they bring in, the key would not cover), a second output (`-gsplit-dwarf`, a linker's import
library, map or PDB). Only a compile that succeeds is kept, with what it printed. The cache is shared by every worktree on
the PC:
LONGSHOT_CHECK_CACHE=<dir> (default %LOCALAPPDATA%\\Longshot\\check-cache, else $XDG_CACHE_HOME/longshot-checks or
~/.cache/longshot-checks); LONGSHOT_CHECK_CACHE=0 turns it off. run_all.py lets entries unused for 14 days go, and the
oldest while the cache is over 4 GB. Standard library only.

    python -I -B tests/check_cache.py [--cache DIR] [--stats FILE] -- <compiler> <arguments...>    a compile through the cache
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

FOLDER = [None]   # (given by --cache: a check may start its compiler with an environment of its own)
STATS = [None]
VERSION = "2"   # (a change to how keys are made: every entry from before is a miss)
NAMESPACE = "longshot-check-cache-v2"
SOURCES = (".c", ".cc", ".cpp", ".cxx", ".c++", ".m", ".mm", ".S")
COMPILER = re.compile(r"^(?:.*-)?(?:gcc|g\+\+|cc|c\+\+|clang|clang\+\+)(?:-\d+(?:\.\d+)*)?(?:\.exe)?$", re.I)
# (flags whose value is the next argument)
TAKES = {"-o", "-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-L", "-x", "-MF", "-MT", "-MQ",
         "-Xlinker", "-Xclang", "-Xpreprocessor", "-Xassembler", "-mllvm", "-arch", "-target", "-D", "-U", "-T", "-e",
         "-u", "-z", "-B", "-l", "--sysroot", "-isysroot"}
PATH_VALUES = {"-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros"}   # (their content is in the -E output)
# (what makes a compile one this cache doesn't take)
REFUSE = re.compile(r"^(?:-x.*|-E|-M|-MD|-MMD|-MM|-MF|-MT|-MQ|-MG|-MP|-save-temps.*|-fprofile-.*|--coverage|-ftest-coverage|"
                    r"-fcoverage-.*|-ftime-trace.*|-gsplit-dwarf|-###|-v|--version|-print-.*|-dump.*|-|@.*|"
                    r"-Wl,.*(?i:implib|-map|pdb|--out).*)$")
ENV = ("CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH", "LIBRARY_PATH", "COMPILER_PATH",
       "GCC_EXEC_PREFIX", "SDKROOT", "MACOSX_DEPLOYMENT_TARGET")


def root():
    """The cache's folder, or None when LONGSHOT_CHECK_CACHE=0 turns it off"""
    if FOLDER[0] is not None:
        return FOLDER[0] or None
    v = os.environ.get("LONGSHOT_CHECK_CACHE")
    if v is not None and v.strip().lower() in ("0", "off", "false", "no"):
        return None
    if v and v.strip():
        return v
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return os.path.join(os.environ["LOCALAPPDATA"], "Longshot", "check-cache")
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "longshot-checks")


def is_compiler(arg):
    return bool(arg) and bool(COMPILER.match(os.path.basename(str(arg))))


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def plan(argv, cwd):
    """(key parts, sources, the output path, the arguments for preprocessing) of a compile this cache takes, else None"""
    compiler, args = argv[0], argv[1:]
    real = shutil.which(compiler) or compiler
    if not os.path.isfile(real) or not is_compiler(os.path.realpath(real)):
        return None
    try:
        st = os.stat(real)
        # Scripts may dispatch to a different compiler without changing their own fingerprint.
        with open(real, "rb") as f:
            if f.read(2) == b"#!":
                return None
        target = subprocess.run([real, "-dumpmachine"], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL)
        if target.returncode or not target.stdout.strip() or target.stderr:
            return None
    except OSError:
        return None
    parts = ["v" + VERSION, os.path.normcase(os.path.abspath(real)), os.path.normcase(os.path.realpath(real)),
             str(st.st_size), str(st.st_mtime_ns), target.stdout.decode("utf-8", "surrogateescape").strip()]
    parts += ["%s=%s" % (k, os.environ.get(k, "")) for k in ENV]
    sources, out, pre = [], None, []
    i = 0
    while i < len(args):
        a = args[i]
        if REFUSE.match(a):
            return None
        if a in TAKES and i + 1 < len(args):
            v = args[i + 1]
            i += 2
            if a == "-o":
                if out is not None:
                    return None
                out = os.path.join(cwd, v)
                parts.append("-o<out%s>" % os.path.splitext(v)[1].lower())
                continue
            if a in ("-L", "-B", "--sysroot", "-isysroot", "-l", "-Xlinker"):
                return None   # (libraries, tools or a whole tree the key would not cover)
            if a == "-T" and os.path.isfile(os.path.join(cwd, v)):
                parts.append("-T<%s>" % file_hash(os.path.join(cwd, v)))
                pre += [a, v]
                continue
            parts.append(a + ("<path>" if a in PATH_VALUES else v))
            pre += [a, v]
            continue
        i += 1
        if a.startswith("-"):
            for flag in PATH_VALUES:   # (a joined value: -Ipath)
                if a.startswith(flag) and len(a) > len(flag) and flag != "-include":
                    parts.append(flag + "<path>")
                    pre.append(a)
                    break
            else:
                if a.startswith(("-L", "-B", "-l", "--sysroot=", "-isysroot")) or (
                        a.startswith("-fuse-ld=") and ("/" in a or "\\" in a)):
                    return None   # (libraries, tools or a whole tree the key would not cover)
                if a.startswith("-Wl,") and any(word.startswith(("-l", "--library"))
                                                 for word in a[4:].split(",")):
                    return None
                parts.append(a)
                if a.startswith("-Wl,"):   # (a file the linker reads, as a version script: its bytes)
                    for word in re.split(r"[,=]", a[4:]):
                        if word and os.path.isfile(os.path.join(cwd, word)):
                            parts.append("<wl:%s>" % file_hash(os.path.join(cwd, word)))
                pre.append(a)
            continue
        path = os.path.join(cwd, a)
        if not os.path.isfile(path):
            return None
        if os.path.splitext(a)[1] in SOURCES:
            sources.append(a)   # (as given: __FILE__ is spelled so)
            parts.append("<src%d>" % len(sources))
        else:
            parts.append("<in:%s>" % file_hash(path))
    if out is None or not sources or not os.path.isdir(os.path.dirname(os.path.abspath(out))):
        return None
    return parts, sources, out, [real] + [p for p in pre if p not in ("-c",)]


def key_of(argv, cwd):
    """The compile's key and output path, or None when it is not one this cache takes (or its preprocessing failed)"""
    p = plan(argv, cwd)
    if p is None:
        return None
    parts, sources, out, pre = p
    h = hashlib.sha256("\0".join(parts).encode("utf-8", "surrogateescape"))
    for src in sources:
        try:
            r = subprocess.run(pre + ["-E", "-P", src], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL)
        except OSError:
            return None
        if r.returncode or r.stderr:
            return None
        h.update(b"\0src\0" + hashlib.sha256(r.stdout).digest())
    return h.hexdigest(), out


def note(what):
    """A hit, a miss or a pass-through, for run_all.py's summary (LONGSHOT_CHECK_CACHE_STATS: a file one word a line)"""
    path = STATS[0] if STATS[0] is not None else os.environ.get("LONGSHOT_CHECK_CACHE_STATS")
    if path:
        try:
            with open(path, "a", encoding="ascii") as f:
                f.write(what + "\n")
        except OSError:
            pass


def written(out):
    """The file a compile with `-o out` made, or None: on Windows a program named with no extension is written as .exe"""
    if os.path.isfile(out):
        return out
    if os.name == "nt" and not os.path.splitext(out)[1] and os.path.isfile(out + ".exe"):
        return out + ".exe"
    return None


def compile_cached(argv, cwd=None):
    """A compile through the cache: the compiler's exit code, its output printed as the compiler printed it"""
    cwd = cwd or os.getcwd()
    base = root()
    k = key_of(argv, cwd) if base else None
    if k is None:
        note("pass")
        return subprocess.run(argv, cwd=cwd).returncode
    key, out = k
    entry = os.path.join(base, NAMESPACE, key[:2], key)
    kept = [n for n in ("out", "out.exe") if os.path.isfile(os.path.join(entry, n))]
    if kept:
        try:
            made = out + kept[0][3:]
            shutil.copyfile(os.path.join(entry, kept[0]), made)   # (a new file's time, as the compiler's would have)
            shutil.copymode(os.path.join(entry, kept[0]), made)
            for name, stream in (("stdout", sys.stdout), ("stderr", sys.stderr)):
                with open(os.path.join(entry, name), "rb") as f:
                    stream.buffer.write(f.read())
                stream.flush()
            os.utime(entry)
            note("hit")
            return 0
        except OSError:
            pass   # (a cache entry being written or removed: compile)
    r = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sys.stdout.buffer.write(r.stdout)
    sys.stdout.flush()
    sys.stderr.buffer.write(r.stderr)
    sys.stderr.flush()
    note("miss")
    made = written(out)
    if r.returncode == 0 and made:
        try:
            os.makedirs(os.path.dirname(entry), exist_ok=True)
            part = tempfile.mkdtemp(prefix=key[:8] + ".", dir=os.path.dirname(entry))
            with open(os.path.join(part, "meta.json"), "w", encoding="utf-8") as f:
                json.dump({"key": key, "version": VERSION}, f)
            shutil.copy2(made, os.path.join(part, "out" + made[len(out):]))
            for name, data in (("stdout", r.stdout), ("stderr", r.stderr)):
                with open(os.path.join(part, name), "wb") as f:
                    f.write(data)
            try:
                os.replace(part, entry)   # (whole or not at all; another check made the same entry first: theirs stays)
            except OSError:
                shutil.rmtree(part, ignore_errors=True)
        except OSError:
            pass
    return r.returncode


def install():
    """In a check's own process (check_cache_boot.py): its compiler calls go through compile_cached. Only a command given as
    a list whose first word is a C or C++ compiler; anything else runs as it would"""
    base = root()
    if base is None:
        return
    real = subprocess.Popen
    head = [sys.executable, "-I", "-B", os.path.abspath(__file__), "--cache", base,
            "--stats", os.environ.get("LONGSHOT_CHECK_CACHE_STATS", ""), "--"]

    class Popen(real):
        def __init__(self, args, *rest, **kw):
            if not kw.get("shell") and isinstance(args, (list, tuple)) and args and is_compiler(args[0]):
                args = head + [os.fspath(a) for a in args]
            super().__init__(args, *rest, **kw)

    subprocess.Popen = Popen


def prune(max_bytes=4 << 30, max_days=14):
    """Entries unused for max_days go, then the oldest while the cache is over max_bytes. Returns (entries, bytes) left"""
    base = root()
    if not base:
        return 0, 0
    base = os.path.join(base, NAMESPACE)
    if os.path.islink(base):
        return 0, 0
    entries = []
    now = time.time()
    try:
        dirs = os.listdir(base)
    except OSError:
        return 0, 0
    for d in dirs:
        if not re.fullmatch(r"[0-9a-f]{2}", d):
            continue
        sub = os.path.join(base, d)
        if os.path.islink(sub) or not os.path.isdir(sub):
            continue
        try:
            names = os.listdir(sub)
        except OSError:
            continue
        for e in names:
            path = os.path.join(sub, e)
            try:
                if os.path.islink(path) or not os.path.isdir(path):
                    continue
                with open(os.path.join(path, "meta.json"), encoding="utf-8") as f:
                    meta = json.load(f)
                key = meta.get("key", "")
                if (meta.get("version") != VERSION or not isinstance(key, str) or
                        not re.fullmatch(r"[0-9a-f]{64}", key) or key[:2] != d or
                        not (e == key or re.fullmatch(re.escape(key[:8]) + r"\.[a-zA-Z0-9_]+", e))):
                    continue
                used = os.stat(path).st_mtime
                size = sum(os.path.getsize(os.path.join(path, f)) for f in os.listdir(path))
            except (OSError, ValueError, AttributeError):
                continue
            if "." in e and now - used > 3600:   # (a write that stopped half way)
                shutil.rmtree(path, ignore_errors=True)
            elif now - used > max_days * 86400:
                shutil.rmtree(path, ignore_errors=True)
            elif "." not in e:
                entries.append((used, size, path))
    entries.sort()
    total = sum(s for _, s, _ in entries)
    while entries and total > max_bytes:
        _, size, path = entries.pop(0)
        shutil.rmtree(path, ignore_errors=True)
        total -= size
    return len(entries), total


def main(argv):
    while argv and argv[0] in ("--cache", "--stats") and len(argv) > 1:
        (FOLDER if argv[0] == "--cache" else STATS)[0] = argv[1]
        argv = argv[2:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    return compile_cached(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
