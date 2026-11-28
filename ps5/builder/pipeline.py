"""Longshot PS5 Builder: the title folder made from a game file and the kit, on Linux, macOS or Windows.

    python pipeline.py --game <game.nup | folder with exefs/> --out <folder> [--kit <kit>] [--work <folder>] [--jobs N]

The kit (ps5/builder/make_kit.py) holds everything of the title that is not made from the game, compiled already. This
adds the game's part, on this PC and from the player's own copy:

  1. the game's program is read out of the game file (its exefs: about 50 MB, not the game's 25 GB) and laid out as
     the image the title loads (longshot-builder image);
  2. it is translated to LLVM IR for the console's guest window (longshot-builder dev lift, the kit's LIFT options);
  3. the IR is compiled for the console (clang);
  4. it is linked with the kit's objects and libraries, the game's Rust among them (ld.lld), and made the console's
     executable (ps5-native-tool);
  5. the title folder is laid out: <out>/<title id>, to copy to /data/homebrew on the console.

Nothing made here may be shared: eboot.bin and image.bin are made from the game.

The tools: longshot-builder (upstream's builder, built for this PC), clang, ld.lld, llvm-nm and ps5-native-tool, looked for in --tools, in LONGSHOT_PS5_TOOLS, in a tools/bin folder
beside the kit, then on PATH. Lines starting with "@@" are for the window (ps5/builder/app.py): "@@step <n> <of> <name>",
"@@progress <done> <total>", "@@done <folder>", "@@error <text>".
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = ".exe" if os.name == "nt" else ""
# The console the title is made for. attribute3 (sce_sys/param.json) is read by the console's kernel at each start: the Pro's
# value asks for its High CPU Frequency mode and its larger memory (13,232 MiB of direct memory); the base value starts the
# title as a base-generation process (12,016 MiB), on a Pro too, where it was measured: Kokiri Forest plays with 1.9 GB free
# at the least, against the Pro mode's 3.2 GB. With that much less room the base profile keeps PS5_Hookshot's 512-texel
# textures (the Pro's 1024 take 135 MB more in the forest). The switches go in longshot_env.txt in the title folder, which
# the title reads at its start (host/hos_main.c); a line the player adds there afterwards is theirs.
MODELS = {
    "base": {"name": "PS5 / PS5 Slim", "attribute3": 0x140040, "env": {"RX_VK_TEX_MAX_DIM": "512"}},
    "pro": {"name": "PS5 Pro", "attribute3": 0x8540040, "env": {}},
}
STEPS = ["Reading the game's program", "Translating it", "Compiling for the console", "Linking", "Making the title folder",
         "Listing the game's shaders"]


class Stop(Exception):
    pass


def say(text):
    print(text, flush=True)


def step(n):
    say("@@step %d %d %s" % (n, len(STEPS), STEPS[n - 1]))


def find_kit(given):
    for d in (given, os.environ.get("LONGSHOT_PS5_KIT"), os.path.join(HERE, "kit"), os.path.join(HERE, "..", "kit"),
              os.path.join(HERE, "..", "..", "dist", "kit")):
        if d and os.path.exists(os.path.join(d, "kit.json")):
            return os.path.abspath(d)
    raise Stop("the kit was not found (kit.json): give its folder with --kit")


def find_tool(name, kit, given):
    dirs = [given, os.environ.get("LONGSHOT_PS5_TOOLS"), os.path.join(kit, "..", "tools", "bin"), os.path.join(kit, "tools", "bin")]
    for d in dirs:
        if d and os.path.exists(os.path.join(d, name + EXE)):
            return os.path.abspath(os.path.join(d, name + EXE))
    found = shutil.which(name)
    if found:
        return found
    raise Stop("%s was not found: it comes with the builder (tools/bin); --tools names another folder" % name)


def available_gb():
    try:
        if os.name == "nt":
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("n", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [(k, ctypes.c_ulonglong) for k in
                                                                               ("total", "avail", "a", "b", "c", "d", "e")]
            m = M()
            m.n = ctypes.sizeof(M)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.avail / 2**30
        if sys.platform == "darwin":
            pages = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout or 0)
            return pages / 2**30 / 2   # (no "available" figure there: half of what the Mac has)
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 2**20
    except Exception:
        pass
    return None


# The build's memory. The translation (longshot-builder dev lift) discovers the game's functions in one process and writes
# the units with --jobs threads; its need by the number of threads was not measured on a player's PC yet, so within
# --memory it is one thread for each 2 GB, one at the least. The compile: mem_need, Gate
MIN_MEMORY_GB = 2     # (--memory's least)
MIN_FREE_GB = 4       # (below this free at the start, a note)


def lift_processes(memory_gb):
    """The translation's threads within memory_gb (0: the cores less one)"""
    if not memory_gb:
        return max(1, (os.cpu_count() or 2) - 1)
    return max(1, min((os.cpu_count() or 2) - 1, int(memory_gb / 2)))


def default_jobs():
    """Compiles at once at the most: the cores less one (how many run is the memory's call: Gate)"""
    return max(1, (os.cpu_count() or 2) - 1)


def run(cmd, env=None, cwd=None, what=None):
    """the command's output goes on as it comes; a failure stops the build with its last lines"""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", env=env, cwd=cwd)
    tail = []
    for line in p.stdout:
        line = line.rstrip("\n")
        tail = (tail + [line])[-25:]
        say(line)
    if p.wait():
        raise Stop("%s failed (exit %d):\n%s" % (what or os.path.basename(cmd[0]), p.returncode, "\n".join(tail[-12:])))


def builder_env():
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    return env


def read_game(kit, work, game, builder):
    """The game's program and its image, by upstream's builder: <work>/lift/exefs and <work>/lift/image"""
    step(1)
    lift_dir = os.path.join(work, "lift")
    if not os.path.exists(game):
        raise Stop("the game file was not found: %s" % game)
    if os.path.isdir(game) and not os.path.exists(os.path.join(game, "exefs", "main")):
        raise Stop("no exefs (a file named main) in %s: give the .nup, or a dump's folder with exefs/ and romfs/" % game)
    stamp = "%s %d %d" % (os.path.abspath(game), os.path.getsize(game) if os.path.isfile(game) else 0, int(os.path.getmtime(game)))
    mark = os.path.join(lift_dir, ".from")
    image = os.path.join(lift_dir, "image", "image.bin")
    if os.path.exists(image) and os.path.exists(os.path.join(lift_dir, "exefs", "main")) and os.path.exists(mark) and \
            open(mark).read() == stamp:
        say("read already, from this file")
        return lift_dir
    os.makedirs(lift_dir, exist_ok=True)
    # (--out: the game folder a PC build would make; here only where the builder keeps its lock)
    run([builder, "image", "--game", game, "--work", lift_dir, "--out", os.path.join(work, "pc-out"), "--root",
         os.path.join(kit, "src")], env=builder_env(), what="reading the game file")
    if not os.path.exists(image) or not os.path.exists(os.path.join(lift_dir, "exefs", "main")):
        raise Stop("the game file gave no program (exefs/main) or no image: is it the supported game and version?")
    with open(mark, "w") as f:
        f.write(stamp)
    return lift_dir


def tree_key(*dirs):
    h = hashlib.sha1()
    for d in dirs:
        for base, _, files in sorted(os.walk(d)):
            for f in sorted(files):
                p = os.path.join(base, f)
                h.update(os.path.relpath(p, d).encode() + b"%d" % os.path.getsize(p))
    return h.hexdigest()


def lift(kit, info, work, lift_dir, builder, lift_jobs):
    """The translation: <work>/lift/gen (the units of LLVM IR, table.c, imports.c, image.c and the runtime in rt/). The
    builder keeps its own key (gen/lift.key): a second build with the same game and kit writes nothing"""
    step(2)
    gen = os.path.join(lift_dir, "gen")
    run([builder, "dev", "lift", "--work", lift_dir, "--repo", os.path.join(kit, "src"), "--exefs", os.path.join(lift_dir, "exefs")] +
        info["lift_options"] + ["--jobs", str(lift_jobs)], env=builder_env(), what="the translation")
    if not os.path.exists(os.path.join(gen, "table.c")) or not any(f.endswith(".ll") for f in os.listdir(gen)):
        raise Stop("the translation ended without its files: if it was stopped for want of memory, close other programs "
                   "and build again")
    # (the kit's objects were compiled against this file, with no game: a lift that chose otherwise would link and not run)
    mine, kits = os.path.join(gen, "rt", "a64_config.h"), os.path.join(kit, "rt", "a64_config.h")
    if open(mine, "rb").read() != open(kits, "rb").read():
        raise Stop("the translation's choices (gen/rt/a64_config.h) are not the kit's: the builder and the kit are of "
                   "different versions")
    return gen


# The compile's memory. A unit of the lift is LLVM IR already cut by the lifter to a cost (a few MB to ~26 MB of text):
# what one takes at its peak is counted as 400 MB + 60 MB per MB of IR, a little over what the largest took here
# (pipeline.py --check does not measure it; README.md has the figures). A compile starts only when its need fits: within
# --memory with those running, else in the memory free now less what the running ones may still take and RESERVE_MB (one
# always runs).
RESERVE_MB = 1536
SELF_MB = 400       # (this process, and the estimates' slack)
COMPILED = "game_%s.o"


def mem_need(path):
    """A file's compile peak in MB: a unit of IR by its size, the lift's C the same way (table.c, 16 MB, took 589 MB)"""
    mb = os.path.getsize(path) / (1 << 20)
    return int(400 + (60 if path.endswith(".ll") else 15) * mb)


class Gate:
    """Compiles started by memory: within limit_mb with those running (--memory), else by the memory free now"""

    def __init__(self, limit_mb):
        import threading
        self.cv = threading.Condition()
        self.limit = limit_mb
        self.running = {}   # (lease: [need, Popen or None])
        self.queue = []     # (the compiles waiting, in the order they came: the largest first, none passed by smaller ones)

    def fits(self, need):
        if self.limit:
            return sum(n for n, _ in self.running.values()) + need <= self.limit
        free = available_gb()
        if free is None:
            return True
        owed = sum(max(0, n - rss_mb(p)) for n, p in self.running.values())
        return free * 1024 - owed - RESERVE_MB >= need

    def enter(self, need):
        with self.cv:
            lease = object()
            self.queue.append(lease)
            while self.queue[0] is not lease or (self.running and not self.fits(need)):
                self.cv.wait(1.0)
            self.queue.pop(0)
            self.running[lease] = [need, None]
            self.cv.notify_all()
            return lease

    def started(self, lease, proc):
        with self.cv:
            self.running[lease][1] = proc

    def leave(self, lease):
        with self.cv:
            self.running.pop(lease, None)
            self.cv.notify_all()


def rss_mb(proc):
    """a running compiler's resident memory in MB (0: not known; Linux only: elsewhere its whole need is counted as owed)"""
    if proc is None or proc.poll() is not None:
        return 0
    try:
        with open("/proc/%d/statm" % proc.pid) as f:
            return (int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")) >> 20
    except (OSError, ValueError, AttributeError):
        return 0


def compile_game(kit, info, work, gen, clang, jobs, memory_gb=0):
    step(3)
    place = lambda a: a.replace("{kit}", kit).replace("{gen}", gen)
    target = [place(a) for a in info["target_flags"]]
    c_flags = target + [place(a) for a in info["game_flags"]]
    ir_flags = target + info["ir_flags"]
    hot_flags = target + [info["hot_opt"] if a == "-O1" else a for a in info["ir_flags"]]
    units = sorted(f for f in os.listdir(gen) if f.startswith("lifted_") and f.endswith(".ll"))
    obj_dir = os.path.join(work, "obj")
    os.makedirs(obj_dir, exist_ok=True)
    version = subprocess.run([clang, "--version"], capture_output=True, text=True).stdout.splitlines()[0]
    limit = max(900, memory_gb * 1024 - SELF_MB) if memory_gb else 0
    heads = hashlib.sha1()
    for f in sorted(os.listdir(os.path.join(gen, "rt"))):
        if f.endswith(".h"):
            heads.update(f.encode() + open(os.path.join(gen, "rt", f), "rb").read())
    base = hashlib.sha1((" ".join(info["target_flags"] + info["game_flags"] + info["ir_flags"] + [info["hot_opt"]]) + version).encode() +
                        heads.digest()).hexdigest()

    # (each job: (name, its file, its flags, its need))
    jobs_list = [(u[:-3], os.path.join(gen, u), hot_flags if u.startswith("lifted_hot_") else ir_flags,
                  mem_need(os.path.join(gen, u))) for u in units]
    for rel in info["game_files"]:
        path = os.path.join(gen, *rel.split("/"))
        jobs_list.append((os.path.basename(rel)[:-2], path, c_flags, mem_need(path)))
    biggest = max(n for _, _, _, n in jobs_list)
    say("compiler: %s; %d files (%d units of the translation), up to %d at once%s; the largest compile takes ~%.1f GB" % (
        version, len(jobs_list), len(units), jobs, " within %.1f GB" % memory_gb if memory_gb else " as the free memory allows",
        biggest / 1024))
    gate = Gate(limit)
    for f in os.listdir(obj_dir):   # (objects of an earlier build's other units: not this build's)
        if f.endswith(".o") and f[:-2] not in {COMPILED[:-2] % n for n, _, _, _ in jobs_list}:
            for g in (f, f + ".key"):
                if os.path.exists(os.path.join(obj_dir, g)):
                    os.remove(os.path.join(obj_dir, g))

    def one(job):
        name, path, flags, need = job
        obj = os.path.join(obj_dir, COMPILED % name)
        h = hashlib.sha1(base.encode())
        h.update(" ".join(flags).encode())
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        key, stamp = h.hexdigest(), obj + ".key"
        if os.path.exists(obj) and os.path.exists(stamp) and open(stamp).read() == key:
            return obj, None
        lease = gate.enter(need)
        try:
            p = subprocess.Popen([clang] + flags + ["-c", path, "-o", obj], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace")
            gate.started(lease, p)
            out, _ = p.communicate()
        finally:
            gate.leave(lease)
        if p.returncode:
            return obj, "%s: exit %d\n%s" % (name, p.returncode, out[-3000:])
        open(stamp, "w").write(key)
        return obj, None

    jobs_list.sort(key=lambda j: -j[3])   # (the largest first, so none is left alone at the end)
    objects, failed, done, t0 = [], [], 0, time.time()
    say("@@progress 0 %d" % len(jobs_list))
    with concurrent.futures.ThreadPoolExecutor(jobs) as pool:
        for future in concurrent.futures.as_completed([pool.submit(one, j) for j in jobs_list]):
            obj, err = future.result()
            done += 1
            objects.append(obj)
            if err:
                failed.append(err)
                say(err)
            say("@@progress %d %d" % (done, len(jobs_list)))
    if failed:
        hint = (" A compiler stopped by the system (exit -9 or 137) wants less memory: set Memory to use lower (--memory)."
                if any("exit -9" in e or "exit 137" in e for e in failed) else "")
        raise Stop("%d of %d files did not compile.%s" % (len(failed), len(jobs_list), hint))
    say("compiled in %.0f s" % (time.time() - t0))
    return objects


def rsp(path, args):
    """a response file: one argument a line, quoted for lld"""
    with open(path, "w", encoding="utf-8") as f:
        for a in args:
            f.write('"%s"\n' % a.replace("\\", "/").replace('"', '\\"'))
    return "@" + path


def link(kit, info, work, objects, lld, nm):
    step(4)
    out = os.path.join(work, "link")
    os.makedirs(out, exist_ok=True)
    place = lambda a: a.replace("{kit}", kit)
    lk = info["link"]
    # (the libc calls host/ps5 defines because no module a title loads exports them: kept inside the executable)
    with open(os.path.join(out, "local.map"), "w") as f:
        f.write("{\n    local:\n" + "".join("        %s;\n" % n for n in info.get("local_symbols", [
            "fnmatch", "isatty", "pthread_setname_np", "timegm", "syscall", "pipe2", "accept4", "posix_fadvise", "getpwuid",
            "umask"])) + "};\n")
    elf = os.path.join(out, "llvm-pie.elf")
    stubs = [os.path.join(kit, "lib", "stubs", n) for n in ("libSceAgc.so", "libSceAgcDriver.so")]
    sdk_lib = os.path.join(kit, "sdk", "target", "lib")
    args = (["-m", "elf_x86_64", "-pie", "--eh-frame-hdr", "-z", "max-page-size=0x4000", "-mllvm", "-emulated-tls"] +
            [place(a) for a in lk["script"]] + [place(a) for a in lk["flags"]] +
            ["--version-script", os.path.join(kit, "link", "native", "app-symbols.map"),
             "--version-script", os.path.join(out, "local.map"), "--exclude-libs=ALL", "--gc-sections", "-e", "_start",
             "-Map=" + os.path.join(out, "longshot.map"), "-o", elf, os.path.join(kit, "obj", "app_crt.o")] +
            [os.path.join(kit, "obj", n) for n in info["objects"]] + sorted(objects) +
            # (the game's Rust, longshot-rs, built for the console with emulated thread-local storage: the kit's)
            [os.path.join(kit, "lib", "liblongshot_rs.a")] +
            [os.path.join(kit, "lib", n) for n in ("libavcodec.a", "libswscale.a", "libavutil.a", "libdav1d.a", "libzstd.a")] +
            stubs + [place(a) for a in lk["inputs"]] + ["--as-needed"] +
            sorted(os.path.join(sdk_lib, f) for f in os.listdir(sdk_lib) if f.endswith(".so")) + ["--hash-style=gnu"])
    t0 = time.time()
    run([lld, rsp(os.path.join(out, "link.rsp"), args)], what="the link")
    exported = set(open(os.path.join(kit, "link", "exported.txt")).read().split())

    def undefined(kinds):
        r = subprocess.run([nm, "-D", "--undefined-only", elf], capture_output=True, text=True, check=True).stdout
        return sorted({l.split()[1].split("@")[0] for l in r.splitlines() if len(l.split()) == 2 and l.split()[0] in kinds})
    # (a weak reference nothing defines reads as null: bound to 0 here, or the console's loader takes it for an import)
    weak = [s for s in undefined(("w", "v")) if s not in exported]
    if weak:
        run([lld, rsp(os.path.join(out, "link.rsp"), args + ["--defsym=%s=0" % s for s in weak])], what="the link")
    null = [s for s in undefined(("U",)) if s not in exported]
    if null:
        raise Stop("the link has imports that no module a title loads exports (null at run time): " + " ".join(null[:20]))
    say("linked: %d MB (%.0f s)" % (os.path.getsize(elf) >> 20, time.time() - t0))
    return elf


def title(kit, info, work, elf, native, out, model):
    step(5)
    app = os.path.join(out, info["title_id"])
    if os.path.exists(app):
        for keep in ("eboot.bin", "image.bin"):   # (a folder built before: its executable and image are replaced)
            if os.path.exists(os.path.join(app, keep)):
                os.remove(os.path.join(app, keep))
    os.makedirs(app, exist_ok=True)
    stub_dir = os.path.join(kit, "sdk", "target", "lib")
    new = os.path.join(work, "eboot.elf.new")
    run([native, "link", "--in", elf, "--out", new, "--stub-dir", stub_dir,
         "--stub", os.path.join(kit, "lib", "stubs", "libSceAgc.so"), "--stub", os.path.join(kit, "lib", "stubs", "libSceAgcDriver.so"),
         "--module-sdk", "0x02000009", "--companion-sdk", "0x08050001", "--file-name", "eboot.elf"], what="the console's executable")
    run([native, "self", "--sign", "--in", new, "--out", os.path.join(app, "eboot.bin"), "--magic", "0x1D3D154F"], what="signing")
    run([native, "self", "--inspect", "--file", os.path.join(app, "eboot.bin")], what="the check of eboot.bin")
    shutil.copytree(os.path.join(kit, "title"), app, dirs_exist_ok=True)
    shutil.copy2(os.path.join(work, "lift", "image", "image.bin"), os.path.join(app, "image.bin"))
    # ---- the console's profile ----
    profile = MODELS[model]
    param = os.path.join(app, "sce_sys", "param.json")
    j = json.load(open(param, encoding="utf-8"))
    j["attribute3"] = profile["attribute3"]
    with open(param, "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(j, indent=2) + "\n")
    with open(os.path.join(app, "longshot_env.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("# Longshot on the PS5: the switches of the profile this folder was built for (%s).\n"
                "# NAME=VALUE lines; a line starting with # is a note.\n" % profile["name"])
        f.write("".join("%s=%s\n" % kv for kv in sorted(profile["env"].items())))
    say("profile: %s (attribute3 %#x%s)" % (profile["name"], profile["attribute3"],
                                           "".join("; %s=%s" % kv for kv in sorted(profile["env"].items()))))
    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(app) for f in fs)
    say("title: %s (%d MB)" % (app, size >> 20))
    return app


def check(a):
    """--check: the builder's own parts on this PC, with no game: the kit, the tools, the lifter's packages and its helper
    library, and one small file compiled for the console. What the release workflow runs on each system's package."""
    kit = find_kit(a.kit)
    info = json.load(open(os.path.join(kit, "kit.json")))
    say("kit: %s (%s), %d objects" % (info["longshot"], info["title_id"], len(info["objects"])))
    missing = [n for n in info["objects"] if not os.path.exists(os.path.join(kit, "obj", n))]
    if missing:
        raise Stop("the kit lacks %d of its objects (%s)" % (len(missing), missing[0]))
    builder, clang, lld, nm, native = (find_tool(n, kit, a.tools) for n in
                                       ("longshot-builder", "clang", "ld.lld", "llvm-nm", "ps5-native-tool"))
    for tool, args in ((clang, ["--version"]), (lld, ["--version"]), (nm, ["--version"])):
        r = subprocess.run([tool] + args, capture_output=True, text=True)
        if r.returncode:
            raise Stop("%s does not run (exit %d): %s" % (tool, r.returncode, (r.stdout + r.stderr)[-300:]))
        say("%s: %s" % (os.path.basename(tool), (r.stdout + r.stderr).splitlines()[0]))
    r = subprocess.run([native], capture_output=True, text=True)
    if "ps5-native-tool" not in r.stdout + r.stderr:
        raise Stop("%s does not run: %s" % (native, (r.stdout + r.stderr)[-300:]))
    say("ps5-native-tool: runs")
    # (the translator: upstream's builder, built for this PC; one instruction decoded says it runs here)
    r = subprocess.run([builder, "dev", "decode", "91000420"], capture_output=True, text=True, env=builder_env())
    if r.returncode or "91000420" not in r.stdout + r.stderr:
        raise Stop("%s does not run (exit %d): %s" % (builder, r.returncode, (r.stdout + r.stderr)[-300:]))
    say("longshot-builder: runs; " + (r.stdout + r.stderr).strip().splitlines()[0])
    for need in (os.path.join("lib", "liblongshot_rs.a"), os.path.join("rt", "a64_config.h"), os.path.join("src", "host"),
                 os.path.join("src", "game")):
        if not os.path.exists(os.path.join(kit, need)):
            raise Stop("the kit lacks %s" % need)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        c = os.path.join(tmp, "check.c")
        with open(c, "w") as f:
            f.write("#include <stdint.h>\n#include <string.h>\nuint64_t check(const char *s) { return strlen(s) + UINT64_C(1); }\n")
        place = lambda x: x.replace("{kit}", kit)
        flags = [place(x) for x in info["target_flags"]] + ["-march=znver2", "-O1"]
        r = subprocess.run([clang] + flags + ["-c", c, "-o", os.path.join(tmp, "check.o")], capture_output=True, text=True)
        if r.returncode:
            raise Stop("clang did not compile for the console: %s" % (r.stdout + r.stderr)[-600:])
        out = subprocess.run([nm, os.path.join(tmp, "check.o")], capture_output=True, text=True).stdout
        if " T check" not in out:
            raise Stop("the compiled check has no function: %s" % out)
    say("clang: compiles for the console (x86_64-sie-ps5, the kit's SDK)")
    say("@@done check")


def shader_lists(kit, work, game, app, builder, jobs):
    """(ps5/builder/shader_lists.py) the game's shader list and the pipeline state list in the title folder: the console's first
    start builds the pipelines before play, in a launch of its own, so play does not compile them (each compile leaves memory
    the base console runs out of, issues #4 and #24)"""
    step(6)
    run([sys.executable, "-I", "-u", os.path.join(HERE, "shader_lists.py"), "--builder", builder, "--src", os.path.join(kit, "src"),
         "--game", game, "--work", work, "--out", app, "--jobs", str(jobs)], env=builder_env(), what="listing the game's shaders")


def build(a):
    kit = find_kit(a.kit)
    info = json.load(open(os.path.join(kit, "kit.json")))
    builder, clang, lld, nm, native = (find_tool(n, kit, a.tools) for n in
                                       ("longshot-builder", "clang", "ld.lld", "llvm-nm", "ps5-native-tool"))
    out = os.path.abspath(a.out)
    work = os.path.abspath(a.work or os.path.join(out, "longshot-ps5-work"))
    os.makedirs(work, exist_ok=True)
    free = shutil.disk_usage(work).free / 2**30
    say("Longshot PS5 Builder: kit %s (%s); work folder %s (%.0f GB free)" % (info["longshot"], info["title_id"], work, free))
    if free < 12:   # (the translation's units are ~6 GB of text, their objects ~1.5 GB)
        raise Stop("the work folder's disk has %.1f GB free: the build wants 12 GB" % free)
    gb = available_gb()
    if a.memory:
        say("memory: the build keeps to about %.1f GB%s" % (a.memory, "" if gb is None else " (%.1f GB is free now)" % gb))
    elif gb is not None and gb < MIN_FREE_GB:
        say("note: %.1f GB of memory is free; the build wants about %.0f GB at the least. Close other programs if it stops."
            % (gb, MIN_FREE_GB))
    game = os.path.abspath(a.game)
    lift_dir = read_game(kit, work, game, builder)
    gen = lift(kit, info, work, lift_dir, builder, a.lift_jobs or lift_processes(a.memory))
    objects = compile_game(kit, info, work, gen, clang, a.jobs or default_jobs(), a.memory)
    elf = link(kit, info, work, objects, lld, nm)
    app = title(kit, info, work, elf, native, out, a.model)
    shader_lists(kit, work, game, app, builder, a.jobs or default_jobs())
    say("@@done " + app)
    return app


def main():
    ap = argparse.ArgumentParser(description="Longshot PS5 Builder: the PS5 title folder from your own game file")
    ap.add_argument("--check", action="store_true", help="check the builder's own parts on this PC (no game is needed), and stop")
    ap.add_argument("--game", help="the game: a .nup file, or an unpacked dump's folder (exefs/ and romfs/)")
    ap.add_argument("--out", help="where the title folder goes")
    ap.add_argument("--model", choices=sorted(MODELS),
                    help="the console the folder is for: base (PS5 and PS5 Slim) or pro (PS5 Pro)")
    ap.add_argument("--kit")
    ap.add_argument("--tools")
    ap.add_argument("--work", help="the build's own files (default: <out>/longshot-ps5-work); kept, so a second build is short")
    ap.add_argument("--jobs", type=int, default=0, help="compiles at once (default: by the cores and the memory free)")
    ap.add_argument("--lift-jobs", type=int, default=0, help="the translation's threads (default: the cores less one)")
    ap.add_argument("--memory", type=float, default=0,
                    help="GB of memory the build may use (default: as much as is free). Lower is slower; %.0f at the least"
                    % MIN_MEMORY_GB)
    a = ap.parse_args()
    if not a.check and not (a.game and a.out and a.model):
        ap.error("--game, --out and --model are needed (or --check)")
    if a.memory and a.memory < MIN_MEMORY_GB:
        ap.error("--memory: %.0f GB at the least" % MIN_MEMORY_GB)
    try:
        check(a) if a.check else build(a)
    except Stop as e:
        say("@@error " + str(e).replace("\n", " | "))
        print(str(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
