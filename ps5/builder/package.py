"""Longshot PS5 Builder, packaged for players: nothing to install, its own Python, compiler and kit inside.

    python3 -I ps5/builder/package.py --system linux-x64     dist/builder/LongshotPS5Builder-<version>-x86_64.AppImage
    python3 -I ps5/builder/package.py --system windows-x64   dist/builder/LongshotPS5Builder-<version>-windows-x64.zip

Both are made on Linux (the Windows one is cross-built: no Windows PC is needed), from the kit (--kit, ps5/builder/
make_kit.py or build_kit.sh) and the PS5_Vulkan submodule's sources. What goes in:

    LongshotPS5Builder/
      LongshotPS5Builder.exe | longshot-ps5-builder    the start (Windows: launcher.c; Linux: a shell script, the AppImage's AppRun)
      app/                      the window and the build (app.py, pipeline.py)
      python/                   CPython (python-build-standalone) with PyQt6
      tools/                    clang, ld.lld and llvm-nm (llvm-mingw's LLVM, whose clang has the PS5's target), ps5-native-tool
                                and longshot-builder (upstream's builder: it reads the game file and translates it)
      kit/                      the title's ready-built part

Windows is a portable folder in a zip: unpack it anywhere and start the .exe; there is no installer. Linux is one
AppImage. Every download is checked against the SHA-256 named here.
"""
import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HERE = os.path.dirname(os.path.abspath(__file__))
LLVM = "https://github.com/mstorsjo/llvm-mingw/releases/download/20261104/"
PYTHON = "https://github.com/astral-sh/python-build-standalone/releases/download/20261110/"
DOWNLOADS = {   # name: (url, sha256)
    "llvm-linux": (LLVM + "llvm-mingw-20261104-ucrt-ubuntu-22.04-x86_64.tar.xz", "919380403de87dca3c581c2053230cd298af5e891286628ba0613b7fed370926"),
    "llvm-windows": (LLVM + "llvm-mingw-20261104-ucrt-x86_64.zip", "552191b5754c49a5a20723276a6fc56541ea1922c4c50b7b669022333fc1b40f"),
    "python-linux": (PYTHON + "cpython-3.13.18+20261110-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz", "f2d2199eb25880e07863b391cf7124e7ffa1338b0d65600dfcec117c4223a7fb"),
    "python-windows": (PYTHON + "cpython-3.13.18+20261110-x86_64-pc-windows-msvc-install_only_stripped.tar.gz", "2e0fed09d48989f64c9305da9ff2d37a41c0d13ab2aa6283405b2b620dfbf805"),
    "zlib": ("https://github.com/madler/zlib/releases/download/v1.4/zlib-1.4.tar.gz", "02dd159783677a749c04adbdd02b55b8a2bcc7e054f51b76b549199a916ad584"),
    "appimagetool": ("https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage", "ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"),
}
# (the window: the translation is longshot-builder's, which needs no Python)
PACKAGES = ["PyQt6==6.12.1"]
# (longshot-builder for Windows is cross-built for llvm-mingw's own target: rustup target add x86_64-pc-windows-gnullvm)
WINDOWS_RUST_TARGET = "x86_64-pc-windows-gnullvm"
LLVM_KEEP = {
    "linux-x64": ["bin/clang-23", "bin/clang", "bin/lld", "bin/ld.lld", "bin/llvm-nm", "lib/libclang-cpp.so.23.1",
                  "lib/libLLVM.so.23.1", "lib/clang/23/include", "LICENSE.TXT"],
    # (bin/clang.exe there is llvm-mingw's wrapper, which picks a MinGW target from its own name: clang-23.exe is the compiler)
    "windows-x64": ["bin/clang-23.exe", "bin/ld.lld.exe", "bin/llvm-nm.exe", "bin/libLLVM-23.dll", "bin/libclang-cpp.dll",
                    "bin/libc++.dll", "bin/libunwind.dll", "bin/libwinpthread-1.dll", "lib/clang/23/include", "LICENSE.TXT"],
}
NATIVE_SOURCES = ("native_app_builder.cpp", "self_container.cpp", "elf_object.cpp", "sce_module_writer.cpp")
ZLIB_SOURCES = ("adler32.c", "crc32.c", "inffast.c", "inflate.c", "inftrees.c", "zutil.c", "trees.c", "deflate.c", "compress.c", "uncompr.c")
README = """Longshot PS5 Builder %(version)s

Builds the PlayStation 5 title folder of Longshot from your own copy of the game.

1. Start %(start)s.
2. Choose your game file (.nup), the folder to build into and your console (PS5 / PS5 Slim, or PS5 Pro), then Build.
   The build takes 40 to 70 minutes and 12 GB of disk. On a computer with little memory free, set Memory to use
   (2 GB at the least): the build keeps within it, more slowly.
3. Copy the folder it makes (LGSH00001) to /data/homebrew on the console, or send it from the window over FTP.
   Your game file goes to /data/eglantineps5/roms on the console.

The folder it makes comes from your game: never share it. This builder holds no game files.
What the builder is made of, and the licences: kit/NOTICE.md and LICENSE.
Without a window: %(start)s --cli --game <file> --out <folder> --model base|pro
"""


def say(text):
    print("package: " + text, flush=True)


def fetch(name, cache):
    url, want = DOWNLOADS[name]
    os.makedirs(cache, exist_ok=True)
    path = os.path.join(cache, url.rsplit("/", 1)[1])
    if not os.path.exists(path):
        say("downloading " + url)
        urllib.request.urlretrieve(url, path + ".part")
        os.replace(path + ".part", path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    if h.hexdigest() != want:
        sys.exit("package: %s has SHA-256 %s, not the %s named in package.py: not used" % (path, h.hexdigest(), want))
    return path


def unpack(archive, to):
    """the archive's one top folder's contents into `to`, once"""
    if os.path.exists(os.path.join(to, ".unpacked")):
        return to
    shutil.rmtree(to, ignore_errors=True)
    tmp = to + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp)
    else:
        with tarfile.open(archive) as t:
            t.extractall(tmp, filter="tar")
    inner = os.listdir(tmp)
    os.replace(os.path.join(tmp, inner[0]) if len(inner) == 1 else tmp, to)
    shutil.rmtree(tmp, ignore_errors=True)
    open(os.path.join(to, ".unpacked"), "w").close()
    return to


def run(cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def version():
    """BUILDER_VERSION (the release's tag, from the workflow), else git's description of this checkout"""
    if os.environ.get("BUILDER_VERSION"):
        return os.environ["BUILDER_VERSION"]
    return subprocess.run(["git", "-C", ROOT, "describe", "--tags", "--always"], capture_output=True, text=True).stdout.strip() or "dev"


def native_parts(system, vulkan, cache, top, kit):
    """ps5-native-tool (PS5_Vulkan tooling/native: the ELF made the console's executable, and signed), its runtime linked
    in, and longshot-builder (upstream's builder, Rust: it reads the game file, ZBIC-compressed program and all, and
    translates it), each for the system, built here by cargo: Linux for this PC, Windows cross-built for
    WINDOWS_RUST_TARGET with llvm-mingw's linker (not run on a Windows PC yet)"""
    native = os.path.join(vulkan, "tooling", "native")
    if not os.path.exists(os.path.join(native, NATIVE_SOURCES[0])):
        sys.exit("package: no PS5_Vulkan sources at %s (git submodule update --init ps5/deps/PS5_Vulkan, or PS5_VULKAN)" % vulkan)
    zlib = unpack(fetch("zlib", cache), os.path.join(cache, "zlib"))
    build = os.path.join(cache, "build-" + system)
    shutil.rmtree(build, ignore_errors=True)
    os.makedirs(build)
    if system == "windows-x64":
        mingw = unpack(fetch("llvm-linux", cache), os.path.join(cache, "llvm-linux"))
        cc, cxx = (os.path.join(mingw, "bin", "x86_64-w64-mingw32-" + n) for n in ("clang", "clang++"))
        tool, extra = os.path.join(top, "tools", "bin", "ps5-native-tool.exe"), ["-static"]
    else:
        cc, cxx = "gcc", "g++"
        tool, extra = os.path.join(top, "tools", "bin", "ps5-native-tool"), ["-static"]
    objs = []
    for f in ZLIB_SOURCES:
        o = os.path.join(build, f[:-2] + ".o")
        run([cc, "-O2", "-w", "-c", os.path.join(zlib, f), "-o", o])
        objs.append(o)
    os.makedirs(os.path.dirname(tool), exist_ok=True)
    run([cxx, "-std=c++20", "-O2", "-w", "-I", zlib] + [os.path.join(native, f) for f in NATIVE_SOURCES] + objs + extra + ["-o", tool])
    # (the kit's own copy is the build PC's: each package has its system's in tools/bin, where pipeline.py looks first)
    kits = os.path.join(kit, "tools", "bin", "longshot-builder")
    if system == "windows-x64":
        env = dict(os.environ)
        env["CARGO_TARGET_%s_LINKER" % WINDOWS_RUST_TARGET.upper().replace("-", "_")] = cc
        run(["cargo", "build", "--release", "-p", "longshot-builder", "--target", WINDOWS_RUST_TARGET], cwd=ROOT, env=env)
        shutil.copy2(os.path.join(ROOT, "target", WINDOWS_RUST_TARGET, "release", "longshot-builder.exe"),
                     os.path.join(top, "tools", "bin", "longshot-builder.exe"))
    else:
        # (built here, not taken from the kit: the kit's was linked on the PC that made the kit, whose C library may be
        # newer than a player's; the packaging PC is the older system on purpose)
        run(["cargo", "build", "--release", "-p", "longshot-builder"], cwd=ROOT)
        shutil.copy2(os.path.join(ROOT, "target", "release", "longshot-builder"), os.path.join(top, "tools", "bin", "longshot-builder"))
    if os.path.exists(kits):
        os.remove(kits)
    if system == "windows-x64":
        run([cc, "-O2", "-municode", "-mwindows", "-static", os.path.join(HERE, "launcher.c"), "-o", os.path.join(top, "LongshotPS5Builder.exe")])
        # (the build without the window, for a terminal: a batch file runs in the console it is started from and keeps its
        # redirects. LongshotPS5Builder.exe --cli does not: a windowed program is given no redirected output, and
        # "--cli ... > log.txt" kept nothing, measured on Windows 11)
        with open(os.path.join(top, "LongshotPS5Builder-cli.cmd"), "w", newline="\r\n") as f:
            f.write('@echo off\nrem Longshot PS5 Builder without the window: "LongshotPS5Builder-cli.cmd --game <file> --out <folder> '
                    '--model base|pro", or --check. Its output goes to this console, or where it is redirected\n'
                    '"%~dp0python\\python.exe" -I -u "%~dp0app\\pipeline.py" %*\n')


def stage(system, a):
    """the folder: top"""
    linux = system == "linux-x64"
    top = os.path.join(a.out, system, "LongshotPS5Builder")
    shutil.rmtree(top, ignore_errors=True)
    os.makedirs(top)
    vulkan = os.environ.get("PS5_VULKAN") or os.path.join(ROOT, "ps5", "deps", "PS5_Vulkan")

    # ---- Python and its packages ----
    with tarfile.open(fetch("python-linux" if linux else "python-windows", a.cache)) as t:
        t.extractall(top, filter="tar")
    host_py = os.path.join(unpack(fetch("python-linux", a.cache), os.path.join(a.cache, "python-linux")), "bin", "python3")
    if linux:
        run([os.path.join(top, "python", "bin", "python3"), "-I", "-m", "pip", "install", "--no-warn-script-location",
             "--disable-pip-version-check", "--no-compile", "-q"] + PACKAGES)
        shutil.rmtree(os.path.join(top, "python", "share"), ignore_errors=True)
        qt_lib = os.path.join(top, "python", "lib", "python3.13", "site-packages", "PyQt6", "Qt6", "lib")
        # (Qt's xcb platform plugin asks for libxcb-cursor, which a desktop does not always have: the build PC's goes along)
        cursor = next((p for p in ("/usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0", "/usr/lib64/libxcb-cursor.so.0") if os.path.exists(p)), None)
        if cursor:
            shutil.copy2(os.path.realpath(cursor), os.path.join(qt_lib, "libxcb-cursor.so.0"))
        else:
            say("no libxcb-cursor.so.0 on this PC to take along (apt install libxcb-cursor0): the window will ask the player's")
    else:
        # (Windows' wheels, fetched and unpacked by this PC's Python: a wheel is a zip of what goes in site-packages)
        wheels = os.path.join(a.cache, "wheels-windows")
        shutil.rmtree(wheels, ignore_errors=True)
        run([host_py, "-I", "-m", "pip", "download", "-q", "--disable-pip-version-check", "--only-binary=:all:", "--platform", "win_amd64",
             "--python-version", "3.13", "--implementation", "cp", "-d", wheels] + PACKAGES)
        site = os.path.join(top, "python", "Lib", "site-packages")
        for w in sorted(os.listdir(wheels)):
            with zipfile.ZipFile(os.path.join(wheels, w)) as z:
                z.extractall(site)

    # ---- the compiler ----
    llvm = unpack(fetch("llvm-linux" if linux else "llvm-windows", a.cache), os.path.join(a.cache, "llvm-linux" if linux else "llvm-windows"))
    for rel in LLVM_KEEP[system]:
        src, dst = os.path.join(llvm, rel), os.path.join(top, "tools", rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.isdir(src):
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    if not linux:
        shutil.copy2(os.path.join(top, "tools", "bin", "clang-23.exe"), os.path.join(top, "tools", "bin", "clang.exe"))

    # ---- the window, the build, the kit, the system's own programs ----
    os.makedirs(os.path.join(top, "app"))
    for f in ("app.py", "pipeline.py", "shader_lists.py"):
        shutil.copy2(os.path.join(HERE, f), os.path.join(top, "app", f))
    shutil.copy2(os.path.join(ROOT, "ps5", "sce_sys", "icon0.png"), os.path.join(top, "app", "longshot.png"))
    shutil.copytree(a.kit, os.path.join(top, "kit"))
    native_parts(system, vulkan, a.cache, top, os.path.join(top, "kit"))
    shutil.copy2(os.path.join(ROOT, "LICENSE"), os.path.join(top, "LICENSE"))
    ver = version()
    open(os.path.join(top, "VERSION"), "w").write(ver + "\n")
    if linux:
        launcher = os.path.join(top, "longshot-ps5-builder")
        with open(launcher, "w") as f:
            f.write('#!/bin/sh\n# Longshot PS5 Builder: the window. "--cli ..." runs the build without it (app/pipeline.py\'s options).\n'
                    'here=$(cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)\n'
                    'export PYTHONDONTWRITEBYTECODE=1\n'
                    'if [ "${1:-}" = "--cli" ]; then shift; exec "$here/python/bin/python3" -I -u "$here/app/pipeline.py" "$@"; fi\n'
                    'exec "$here/python/bin/python3" -I "$here/app/app.py" "$@"\n')
        os.chmod(launcher, os.stat(launcher).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    with open(os.path.join(top, "README.txt"), "w", newline="\r\n" if not linux else "\n") as f:
        f.write(README % {"version": ver, "start": "the AppImage" if linux else "LongshotPS5Builder.exe"})
    return top, ver


def appimage(top, ver, a):
    appdir = os.path.join(a.out, "linux-x64", "LongshotPS5Builder.AppDir")
    shutil.rmtree(appdir, ignore_errors=True)
    os.makedirs(os.path.join(appdir, "usr"))
    os.replace(top, os.path.join(appdir, "usr", "LongshotPS5Builder"))
    with open(os.path.join(appdir, "AppRun"), "w") as f:
        f.write('#!/bin/sh\nexec "$(dirname -- "$(readlink -f -- "$0")")/usr/LongshotPS5Builder/longshot-ps5-builder" "$@"\n')
    os.chmod(os.path.join(appdir, "AppRun"), 0o755)
    with open(os.path.join(appdir, "longshot-ps5-builder.desktop"), "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Longshot PS5 Builder\nComment=Builds the PS5 title folder of Longshot from "
                "your own copy of the game\nExec=longshot-ps5-builder\nIcon=longshot-ps5-builder\nCategories=Game;Utility;\nTerminal=false\n")
    shutil.copy2(os.path.join(ROOT, "ps5", "sce_sys", "icon0.png"), os.path.join(appdir, "longshot-ps5-builder.png"))
    shutil.copy2(os.path.join(appdir, "longshot-ps5-builder.png"), os.path.join(appdir, ".DirIcon"))
    tool = fetch("appimagetool", a.cache)
    os.chmod(tool, 0o755)
    out = os.path.join(a.out, "LongshotPS5Builder-%s-x86_64.AppImage" % ver)
    # (--appimage-extract-and-run: no FUSE in a build container)
    run([tool, "--appimage-extract-and-run", "--no-appstream", appdir, out], env=dict(os.environ, ARCH="x86_64", VERSION=ver))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--system", required=True, choices=sorted(LLVM_KEEP))
    ap.add_argument("--kit", default=os.path.join(ROOT, "dist", "kit"))
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "builder"))
    ap.add_argument("--cache", default=os.path.join(ROOT, ".local", "builder-downloads"))
    ap.add_argument("--folder-only", action="store_true", help="stop at the folder (dist/builder/<system>/LongshotPS5Builder)")
    a = ap.parse_args()
    a.out, a.cache, a.kit = (os.path.abspath(p) for p in (a.out, a.cache, a.kit))
    if not os.path.exists(os.path.join(a.kit, "kit.json")):
        sys.exit("package: no kit at %s (ps5/builder/build_kit.sh)" % a.kit)
    top, ver = stage(a.system, a)
    total = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(top) for f in fs if not os.path.islink(os.path.join(d, f)))
    say("%s (%d MB)" % (top, total >> 20))
    if a.folder_only:
        return 0
    if a.system == "linux-x64":
        out = appimage(top, ver, a)
    else:
        out = os.path.join(a.out, "LongshotPS5Builder-%s-windows-x64.zip" % ver)
        if os.path.exists(out):
            os.remove(out)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for d, _, fs in sorted(os.walk(top)):
                for f in sorted(fs):
                    p = os.path.join(d, f)
                    z.write(p, os.path.relpath(p, os.path.dirname(top)))
    say("%s (%d MB)" % (out, os.path.getsize(out) >> 20))
    with open(out + ".sha256", "w") as f:
        h = hashlib.sha256()
        with open(out, "rb") as g:
            for block in iter(lambda: g.read(1 << 20), b""):
                h.update(block)
        f.write("%s  %s\n" % (h.hexdigest(), os.path.basename(out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
