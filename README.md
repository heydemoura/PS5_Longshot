<h1 align="center">
  <br>
  <a>
  <img src="ps5/sce_sys/icon0.png" alt="Longshot" width="200">
</a>

  <br>
  <b>Longshot</b>
  <br>
</h1>

<h4 align="center">Longshot for the PlayStation 5: a supported console game running natively on the PS5, without emulation.
</h4>

<p align="center">
  <a href="ps5/builder/README.md"><b>The PS5 Builder</b></a> |
  <a href="ps5/README.md"><b>The port</b></a> |
  <a href="https://longshot.magicbean.dev/">Longshot for PC (upstream)</a>
</p>

> [!IMPORTANT]
> **This repository is the PlayStation 5 version of Longshot.** It is a port of
> [Longshot](https://git.magicbean.dev/MagicBeanDev/Longshot) to the PS5, with
> [PS5_Vulkan](https://github.com/mihawk-99/PS5_Vulkan) as its Vulkan driver, and it builds one thing: the PS5 title.
>
> **No PC builds of Longshot are included or released here.** There is no Windows or Linux game, launcher or builder
> in this repository's releases. For Longshot on a PC, go to [Longshot itself](https://longshot.magicbean.dev/).
>
> What the releases here hold is the **Longshot PS5 Builder**: a desktop program for Linux (an AppImage) and Windows
> (a portable folder) that makes the PS5 title folder from your own copy of the game, on your own computer. It holds
> no game files.
>
> - Using it, and what it does: [ps5/builder/README.md](ps5/builder/README.md)
> - The port: what runs, how it was measured, the console's profiles: [ps5/README.md](ps5/README.md)
>
> It runs on the **PS5 Pro** and on the **base PS5** (PS5 and PS5 Slim): the builder asks which one the folder is for.
>
> A jailbroken PS5 with a homebrew environment is needed to run the title. The game is not included, and nothing here
> explains how to get it.

**[Install and play](#install-and-play)** and **[In-game menu](#in-game-menu)** below are this port's. The other
sections are **Longshot's own README, for its PC versions**, kept as they are for reference: the status table, mods,
building from source and the rest describe the PC game, not this port.

---

Longshot is a static recompilation of a supported console game. The game's code is translated to C and compiled into a Windows or Linux program, rebuilding the graphics on Vulkan, with the console's system software reimplemented. All of this happens **on your PC, from your own copy of the game**.

Longshot is a continuation of [Hookshot](https://github.com/NotRexed/Hookshot). The top maintainers have moved to this project.

**[Join us on Discord](https://discord.gg/magicbean)!**
<br>

> [!IMPORTANT]
> This repository **does not contain the game, keys or an executable**, and it does not explain how to get them. You
> need your own copy. Everything the build creates (`longshot.exe`, the generated code, the shader caches and your
> saves) comes from your game and stays on your PC. Never share those files.

## Project status

Experimental. Players have reported on many PCs, from Kokiri Forest to the dungeons: the table says what works and
what does not yet. Open problems are collected in tracking issues, linked below.

| What | Status |
|---|---|
| Kokiri Forest: reaching gameplay and walking around | ✅ Works |
| Sound, controller, keyboard and saves | ✅ Works |
| In-game menu, frame generation and upscaling | ✅ Works |
| Flicker (the picture flashing too bright while moving) | ✅ Fixed in v2.0.2 ([#645](https://github.com/NotRexed/Hookshot/pull/645)) |
| Ground fog vanishing when looking down | ✅ Fixed in v2.0.2 ([#638](https://github.com/NotRexed/Hookshot/pull/638)) |
| Models with missing pieces (doors, fences, parts of trees) | ✅ Fixed where tested, more play needed |
| Stutter when entering new areas | ⚠️ Still happens the first time you visit a place. The next visits are smooth |
| Performance | ⚠️ Depends on the PC: some reach a steady 60 fps, others stay below it, more so in Hyrule Field |
| Hyrule Field, Castle Town, Kakariko and the dungeons | ⚠️ Players got there, with open bugs: lighting and shadows ([#1004](https://github.com/NotRexed/Hookshot/issues/1004)), crashes and freezes ([#2507](https://github.com/NotRexed/Hookshot/issues/2507)) |
| Ultrawide and 4:3 | ✅ Works. The Aspect ratio row's default, Monitor's shape, widens the 3D to the screen and keeps the HUD and movies in the middle 16:9 ([#2119](https://github.com/NotRexed/Hookshot/pull/2119)) |
| NVIDIA cards | ✅ Works. The most tested |
| AMD cards | ⚠️ Many tested: reports from the RX 6600, RX 6800 XT, RX 7800 XT, RX 7900 XTX and RX 9070 XT. On Windows, the RX 6600 to RX 6950 XT and their mobile and Pro models (Navi 21, 22 and 23) draw task and mesh shaders through the compute fallback by default: the driver lost the GPU on their own mesh shaders on an RX 6700 XT, an RX 6800 XT and, in one report in Mako, an RX 6600 ([#1747](https://github.com/NotRexed/Hookshot/pull/1747), [#1992](https://github.com/NotRexed/Hookshot/pull/1992), [#2215](https://github.com/NotRexed/Hookshot/pull/2215)); another RX 6600 played 15-20 minutes on them ([Longshot #130](https://git.magicbean.dev/MagicBeanDev/Longshot/issues/130)). The RX 6400 and 6500 XT (Navi 24) keep their own mesh shaders, as no report has them losing the GPU; `RX_VK_TASK_MESH_FALLBACK=1` tries the fallback. Driver crashes and device lost are still reported ([#996](https://github.com/NotRexed/Hookshot/issues/996)) |
| Intel cards | ❓ Few reports, many of them black screens or crashes ([#999](https://github.com/NotRexed/Hookshot/issues/999)). On Windows, Intel's shader compiler is entered by one thread, and a shader it faults on is left out instead of closing the game, on later starts too ([#252](https://git.magicbean.dev/MagicBeanDev/Longshot/pulls/252)). Made for an Intel UHD laptop with 4 threads and 16 GB that crashed in `igvk64.dll` (see [Intel laptops](https://git.magicbean.dev/MagicBeanDev/Longshot#intel-laptops)): not yet tested on an Intel GPU |
| Pre-rendered movies on a slow CPU | ⚠️ Shown rather than skipped when the decoder is slower than the movie, so the picture can fall behind the sound. With 4 logical processors or fewer the decoder uses 2 threads, leaving the game its own. The first picture can still be black on some PCs |
| Cards without mesh shaders (such as NVIDIA GTX 10 and AMD RX 5000) | ⚠️ Run through the compute fallback (since v2.0.6), which runs the mesh programs as compute shaders on cards with buffer device addresses: slower, and it leaves out the mesh draws that take their count from a buffer ([#640](https://github.com/NotRexed/Hookshot/issues/640)) |
| Linux | ✅ Works with the AppImage. Tested on several distributions (CachyOS, Fedora, Bazzite, SteamOS) and cards, with open problems on some AMD cards ([#1000](https://github.com/NotRexed/Hookshot/issues/1000)) |

## Install and play

Longshot for the PS5 does not come ready to play. The title is built **on your computer, from your own copy of the
game**, by the **Longshot PS5 Builder**, then copied to the console.

### [Download the Longshot PS5 Builder](https://github.com/heydemoura/PS5_Longshot/releases/latest)

| Your computer | File |
|---|---|
| Linux | `LongshotPS5Builder-<version>-x86_64.AppImage` |
| Windows | `LongshotPS5Builder-<version>-windows-x64.zip` (a portable folder: no installer) |

Each file has a `.sha256` beside it. There is no macOS builder, and no PC version of the game here.

### What you need

**Your game copy:** program `0400486024DEC000` · update **v131072** · build `e0f3ccfa`, as a `.nup` file. Other
versions are not supported. The same file is used twice: the builder reads the game's program from it on your
computer, and the console reads the game's data from it while you play.

**A PlayStation 5 that runs homebrew:** a jailbroken console with a homebrew environment that starts titles from
`/data/homebrew`, and an FTP server on it to copy files with (the builder's Send to PS5 uses port 2121 unless you
change it). This repository does not explain how to set that up.

**A computer to build on:**

| | |
|---|---|
| **System** | 64-bit Windows 10 or 11, or 64-bit Linux with glibc 2.35 or newer (Ubuntu 22.04, Debian 12, Fedora, Arch, SteamOS). |
| **Memory** | About 10 GB free while it builds. The translation alone holds about 6 GB. |
| **Storage** | About 6 GB free where you build, and 1 GB for the builder itself. |
| **Time** | 40 to 70 minutes on a 6-core computer, most of it the compile step. A second build for the other console model takes seconds. |
| **Internet** | Only to download the builder. The build itself downloads nothing. |

The builder carries its own compiler, Python and the ready-built parts of the title: there is nothing else to install.

### Build the title

1. **Start the builder.**
   - Linux: make the AppImage executable (`chmod +x LongshotPS5Builder-*.AppImage`) and start it.
   - Windows: unpack the zip anywhere, open the `LongshotPS5Builder` folder and start `LongshotPS5Builder.exe`.
     Keep the folder whole: the program needs what is beside it.
2. **Game file:** choose your `.nup`.
3. **Build into:** choose a folder on your computer.
4. **Console:** choose your model. The folder is built for one of them.
   - **PS5 / PS5 Slim:** the base console's memory, textures of at most 512 texels.
   - **PS5 Pro:** its faster processor mode and larger memory, textures of at most 1024 texels.

   A folder built for the base console also runs on a Pro, in the base console's mode.
5. Press **Build**. The window shows the five steps and, under Show details, the log. Stop keeps what is done: the
   next Build goes on from there.

When it ends you have a folder named **`PPSA99025`** in the folder you chose. It is made from your game: **never
share it.**

### Put it on the console

1. Copy the whole **`PPSA99025`** folder to **`/data/homebrew/`** on the console, so that it is
   `/data/homebrew/PPSA99025`. Either type the console's address in the builder and press **Send to PS5**, or copy it
   with any FTP program.
2. Copy your game file to the console as **`/data/eglantineps5/roms/oot.nup`** (make the folders if they are not
   there). It is 25 GB: this is the long copy. A file already there for another program, under
   `/data/lichii/roms/oot/oot.nup`, is found too.
3. Start **Longshot** from the console's home screen, or from your homebrew launcher.

The first visit to each place stutters while its shaders are made; they are kept, and later visits are smooth.

### What to expect

Measured on a PS5 Pro, in Kokiri Forest, at 1920x1080:

- The game itself runs at 31 to 34 frames a second there (the processor is the limit), 60 at the title screen and in
  movies. Frame generation is on from the start and shows about twice that: 63 to 68 a second in the forest.
- Played so far: a new game from the title screen through the opening to Link's house, a later start from that
  autosave, and Kokiri Forest. **Nothing beyond the forest has been played**, and sound and a real controller were
  not checked by a person: the test runs used scripted input.
- **It also runs on a base PS5** (PS5 and PS5 Slim), built with the builder's PS5 / PS5 Slim profile, at **about 20
  frames a second less than a Pro, with frame generation on**: about 45 shown in Kokiri Forest where a Pro shows 63 to
  68. That figure is from play on a base console, not from a logged run; the other numbers here are a Pro's. The base console's memory was measured, on a Pro started in the base console's mode
  (12 GB): the forest plays with about 2 GB free.

[ps5/README.md](ps5/README.md) has the measurements.

### Language

The game starts in the console's language (Settings > System > Language) when the game has it: English (US and UK),
French (France and Canada), German, Italian, Spanish (Spain and Latin America), Portuguese (Brazil, also given to a
console set to Portuguese (Portugal)), Dutch, Polish, Russian, Japanese, Korean and Chinese (Simplified and
Traditional). Any other language starts it in English (US). The menu's **Game language** row (Gameplay tab) has
**Follow the console** first, then each of the game's languages: pick one to keep it whatever the console's is, or
Follow the console to go back. It applies after a restart, which the menu offers when you close it.

### Updating

Download the new builder, build again and copy the new `PPSA99025` folder over the one on the console. Your saves
and settings are in that folder on the console and are not in the one you copy: they stay.

### Where the files go

On the console, everything of Longshot's is in `/data/homebrew/PPSA99025`:

| | |
|---|---|
| `eboot.bin`, `image.bin` | The title, made from your game. |
| `user/` | Your saves. |
| `longshot_options.ini` | Your settings. |
| `language.txt` | The menu's Game language: a language's code, or no code line for Follow the console. |
| `longshot_env.txt` | The switches of the profile the folder was built for. A line you add is kept until you copy a new folder over it. |
| `cache/`, `radv-shader-cache/` | Shaders made as you play. Safe to delete: they are made again. |
| `logs/`, `bug-reports/` | What the title wrote about its runs. |

On your computer, the folder you built into holds `PPSA99025` and `longshot-ps5-work`, the build's own files (about
3 GB). Keep the second one and the next build is short; delete it and nothing is lost but time.

### What it connects to

The builder connects to nothing on its own. **Send to PS5** connects to the address you type, over FTP, on your
network. The title on the console does not look for updates.

### If something goes wrong

- **The build stops in "Translating it to C" or "Compiling for the console" with no clear error:** it ran out of
  memory. Close other programs; for the compile step, lower **Compile at once** (each file takes up to 1.4 GB).
- **"the game file gave no program":** the file is not the supported game and version, or is not a `.nup`.
- **Linux, the AppImage does not start:** it needs FUSE (`libfuse2` on Ubuntu and Debian), or start it with
  `--appimage-extract-and-run`.
- **Windows shows a warning when the builder starts:** it is not code-signed.
- **Send to PS5 fails:** the console has to be on, with its FTP server running, at that address and port.
- **Longshot is not on the home screen:** the folder has to be `/data/homebrew/PPSA99025` itself, with `eboot.bin`
  directly inside it, and your homebrew environment has to list titles from there.
- **The title does not reach the game's logos:** check the game file's name and place in "Put it on the console"
  first. What happens without the file was not tried.
- **Without the window:** `--cli --game <file> --out <folder> --model base|pro` after the AppImage's name, or after
  `LongshotPS5Builder.exe`. `--cli --check` checks the builder's own parts.

## In-game menu

Longshot's own menu (resolution, frame generation, sound, controls) is in the PS5 title too, opened and moved in with
the controller.

**Opening it:** press the **touch pad's click and Options together**. They are the Switch's − and +, and the menu
opens on the two pressed together. The PS button stays the console's own. There is no keyboard or mouse on the
console, so the menu's <kbd>Insert</kbd>, <kbd>F1</kbd> and <kbd>Esc</kbd> keys do not apply.

**Moving in it:** L1 and R1 change tab, the D-pad chooses a row and changes it, Circle (the Switch's A) changes it too
or opens a page, Cross (the Switch's B) goes back or closes the menu.

**What is worth changing on a PS5:**

| Row | On the console |
|---|---|
| **Output resolution** (Graphics) | 1920x1080 by default. Choosing 2560x1440 switches the console to that display mode **at the next start**. Measured in Kokiri Forest on a Pro: 31 to 32 game frames a second at 1920x1080, 29 at 2560x1440. 3840x2160 works on a Pro too: played there, about 40 frames a second shown with frame generation on (seen in play, not from the log). |
| **Quality preset** (Graphics) | "Auto" picks Balanced on the console: the full picture, not a reduced one. |
| **Upscaler** (Graphics) | AMD FSR 3.1 from the next release on, where "Auto" chooses. The game's own network is still on the row. |
| **Frame generation** (Graphics) | On, 2x, from the start: PS5_MihawkGen's network makes one frame between each two of the game's. |
| **Volume**, **Text speed**, **Field of view**, **Camera** rows | The game's own; nothing console-specific. |

**What does not apply:** NVIDIA DLSS, AMD FSR 4, Intel XeSS, NVIDIA Reflex and AMD Anti-Lag (PC graphics cards'),
Fullscreen and V-Sync (the console has one display mode at a time), the keyboard and mouse rows, the microphone, motion
controls (the DualSense's sensors are not read yet) and the Discord status.

Your choices are saved in `longshot_options.ini` in the title's folder on the console
([Where the files go](#where-the-files-go)). To go back to the defaults, delete that file.

Every row of the menu is described in
[Longshot's own README](https://git.magicbean.dev/MagicBeanDev/Longshot#in-game-menu), for the PC game.

### Controls

The DualSense of the user who started the title. By default the buttons sit where the Switch's do:

| Switch | DualSense |
|---|---|
| A | Circle |
| B | Cross |
| X | Triangle |
| Y | Square |
| L, R | L1, R1 |
| ZL, ZR | L2, R2 |
| − | Touch pad's click |
| + | Options |
| Sticks, stick clicks, D-pad | The same |

Rumble goes to the controller. Motion controls, the touch pad's surface and the microphone are not read. The menu's
Controls tab can move the buttons.

## Mods

Put mods in a `mods` folder in the game folder, beside `longshot.exe` (or point `HOS_MODS` at another folder), in the
layout emulators use: `mods/<name>/romfs/...` holds files that replace or add to the game's own. Deeper layouts such as
`mods/contents/<title id>/<name>/romfs` work too. A folder whose name starts with `-` or `_` is off. When two mods
have the same file, the one whose whole path relative to `mods` sorts last (ignoring case) wins. Use `01-...`, `02-...`
at the same level to set the order. Stop the game before changing mods, then restart.

One of the game's texts names the console: the hint for playing the ocarina by microphone says to use its built-in one.
The build rewords it for a PC ("your microphone") in your own text files, in each of the game's languages, into the game
folder's `texts` folder, and the game reads those instead. A mod's own text file wins, and `HOS_TEXTS=0` in
`longshot_env.txt` shows the game's own.

Only RomFS mods are loaded. `exefs` patches (`.pchtxt`, `.ips`) are left out: Longshot compiles the game's code ahead
of time, so patching its instructions would need a rebuild. The log (`longshot_log.txt`) names each mod and the files
read from it.

A mod file larger than the game's own does not load unless the resource size table allows for it:
`System/Resource/ResourceSizeTable.Product.111.Nin_Ounce_NVN2.rsizetable.zs` gives each file the memory the game sets
aside for it (by the CRC32 of its path without `.zs`; for a texture, its decompressed size + 280). Past that, the game
goes on without the mod's file, or a loading screen waits for good. A mod that makes files bigger (HD
textures, longer parameter files) ships the table with its entries grown. Since only one mod's table is used, mods that
each grow files need one table with all their entries, in the mod that sorts last. Longshot merges the button prompts'
entries into that table (each file gets the larger of the two), so your controller's icons stay in texts; the log says
`(with the button prompts' sizes merged in)` after the table's line. A table it cannot read that way (not the game's
format, or a zstd frame that does not give its size) is used as it is, with the Switch's icons.
If a mod includes tables for several game versions, the prompts use the table name from the game's own RomFS,
not the first table found in the mod. Other versions' tables do not change the prompts' font or its size entries.

For authors: the [existing file contract and game-free example](docs/mods.md) cover discovery, conflicting files,
disabling and what this loader does not provide. The example contains only invented text, not a seed or game data.

## Build from source

For developers, and for players who prefer the command line. The launcher and the builders run the same script,
`nx2/make_game.py`. [CONTRIBUTING.md](CONTRIBUTING.md) has more.

<details>
<summary>Windows, from the command line</summary>

```powershell
python -I nx2/make_game.py --nup "C:\path\your-game.nup" --out "C:\Games\Longshot" --language en-US
python -I nx2/make_game.py --romfs "C:\path\romfs" --exefs "C:\path\exefs" --out "C:\Games\Longshot" --language en-US
```

Add `--check` with your `.nup` or `--exefs` to only check the game's version. It builds nothing.
Add `--log "C:\path\build.log"` to also save the build's output to a file (UTF-8, replaced if it exists); it works
with `--check` and `--update` too, and on Linux.
`LONGSHOT_JOBS=<n>` (or `--jobs <n>`) limits how many files compile at once.
`LONGSHOT_MEM_LIMIT_GB=<n>` lets the compilers take that many GB together, whatever is free now; up to 4 always run.

You need:

- Python 3 from python.org (the Microsoft Store's Python installs packages where `python -I` does not look), with
  `pip install -r requirements.txt` (core tools plus the button-prompt packages).
- [llvm-mingw](https://github.com/mstorsjo/llvm-mingw) (the `ucrt-x86_64` build), unpacked as `llvm-mingw` in the
  clone or with `LONGSHOT_MINGW` set to its folder. Without it the build uses the clang from
  [emsdk](https://github.com/emscripten-core/emsdk) at `C:\emsdk` with Visual Studio's C++ tools and the Windows SDK
  (`LONGSHOT_MSVC=1` forces that route).
- What the builder's `tools` folder carries and a clone does not. From an unpacked release zip, run
  `LongshotBuilder.exe --cli --help` once (it installs the tools, then runs `make_game.py --help`): the folder is
  `%LOCALAPPDATA%\LongshotBuilder\versions\<version>\LongshotBuilder\tools`. The builder sets the variables itself;
  from the command line, set them before you run `make_game.py`:
  - `tools\cpm`: the Vulkan headers and FFmpeg. Set `RX_CPM` to this folder. Without it (and without system
    Vulkan / SPIR-V / VMA headers or `LONGSHOT_*_HEADERS`), the standalone build stops instead of shipping a
    game with no renderer.
  - `tools\third_party`: the prebuilt files a clone lacks (`a64emit`, `lld-link`, `zbic` and SDL2). Copy its `.dll`,
    `.exe`, `.lib` and `.sha1` files to the same places under `third_party\`.
  - `tools\work`: copy it to `work\`. It holds the hot-function list, the link order, the profile and the icon;
    without them the game builds but runs slower.
- .NET 8, only if you want to change the builder's `--cli` program itself (`builder/LongshotBuilder`).

</details>

<details>
<summary>Linux, the AppImage from the command line</summary>

```sh
./Longshot-<version>-x86_64.AppImage --cli --nup ~/your-game.nup --out ~/Games/Longshot   # make_game.py's options
./Longshot-<version>-x86_64.AppImage --play ~/Games/Longshot   # the game in that folder
./Longshot-<version>-x86_64.AppImage --cli --update ~/Games/Longshot # that folder built again with this release
```

`LONGSHOT_JOBS=<n>` (or `--jobs <n>` with `--cli`) limits how many files compile at once.
`LONGSHOT_MEM_LIMIT_GB=<n>` lets the compilers take that many GB together, whatever is free now. Linux stops a compiler
that does not fit; it is then compiled again alone at the end.

In a game folder, `longshot` is the launcher and the game itself is `bin/longshot`. Started with no arguments, the
AppImage only says to get the Longshot Launcher.

</details>

<details>
<summary>Linux, without the AppImage</summary>

You need `clang` and `lld` (LLVM 23 or newer reads the play profile; an older one builds without it), Python 3 with
`pip install -r requirements.txt`, FFmpeg's development files (with dav1d, for the movies) and SDL3 (SDL2 works too).
Then run `python -I nx2/make_game.py ...` as on Windows. Vulkan / SPIR-V / VMA headers come from the distro, from
`LONGSHOT_*_HEADERS`, or from a CPM cache via `RX_CPM`.

`scripts/linux/deps.sh tools` fetches and builds all of these into `build-linux/deps` (the AppImage's own tools,
`tools.lock`'s Linux lines), and `scripts/linux/appimage.sh` makes the AppImage. A folder built from a source checkout has no launcher: `longshot` there is the game.

</details>

<details>
<summary>The Windows game, built on Linux</summary>

Unpack llvm-mingw's Linux build (`ucrt-ubuntu-22.04-x86_64`), then run `make_game.py` with `RX_TARGET=windows` and
`LONGSHOT_MINGW` set to its folder. The objects go to `~/.cache/longshot-builder-windows`, apart from the Linux game's.
The game folder runs under Proton or on Windows.

</details>

### Running the game from the command line

The built game is `bin\longshot.exe` on Windows and `bin/longshot` in a packaged Linux game folder. A build from a
source checkout has `longshot.exe` or `longshot` in its build folder, with no launcher beside it. The `longshot.exe`
or `longshot` launcher in the game folder does not pass these arguments to the game, so start the game directly to
use them.

| Argument | What it does |
|---|---|
| `image.bin` | The game image to run. Without it the game uses the `image.bin`, `romfs` and `user` in its folder |
| `--romfs <folder>` | The extracted RomFS folder |
| `--user <folder>` | Where the game keeps per-user data: saves, caches and temp files (and `mouse_sens.txt` when `HOS_MOUSE_SENS` is set; the menu keeps the sensitivity otherwise). Default: `user` in the game folder on Windows, `~/.local/share/longshot` on Linux; with an explicit image path, `user` in the current folder |
| `--trace` | Log every Horizon OS call (the same as `HOS_TRACE=1`) |
| `--prepare-shaders` | Prepare the shaders as the first start does, then exit without starting the game (no build step runs it) |
| `--compile-shaders all` or `most` | What a preparation of this run builds, over `compile_shaders` in `longshot_options.ini` (the same as `RX_VK_COMPILE_SHADERS`): `all` the predicted pipelines too, `most` without them. See [Compile shaders](#compile-shaders) |

For example, keep saves somewhere else:

```powershell
cd "C:\Games\Longshot"
.\bin\longshot.exe --user "D:\Longshot data"
```

```sh
cd ~/Games/Longshot
./bin/longshot --user "$HOME/longshot-data"
```

`longshot_env.txt` and the other test switches are documented below.

<details>
<summary>AMD FSR 3.1 (every build)</summary>

**AMD FSR 3.1** in the Graphics tab's **Upscaler** row (`dlss=3` in `longshot_options.ini`; `RX_VK_FSR=1` for one run,
`RX_VK_FSR=0` never) upscales in place of the game's own network, and with **Frame generation** at 2x its frame
interpolation can make the frame in between with `RX_VK_FSR_FG=1` (opt-in: the game draws its HUD into the same picture
and FSR 3.1 has no HUD-less colour to keep it still, so the HUD smears; by default the renderer's own makes it). Both are AMD's FidelityFX
SDK 1.1.4 shaders (`third_party/fidelityfx-sdk`, MIT), compiled into the renderer: no AMD runtime or DLL is needed, and
the row offers FSR 3.1 on any Vulkan 1.3 GPU with push descriptors, extended storage image formats and storage writes
without a format (`vk: fsr3: ... can run on this GPU` in the log). It upscales on the same inputs as DLSS and FSR 4: the
scene's colour, its depth, motion from the camera with the skinned characters' own drawn over it (Link, his shield and
the people around him keep their outline while they move), and the same camera jitter (`RX_VK_FSR_JITTER=0` turns it
off). Things that move by themselves and are not skinned (Navi's body, props) can still trail. `RX_VK_FSR3_CHAIN=0` gives
it the camera's motion alone, as before.

</details>

<details>
<summary>NVIDIA DLSS (optional, NVIDIA's SDK under NVIDIA's license)</summary>

A game built with NVIDIA's DLSS SDK offers **NVIDIA DLSS** and **NVIDIA DLAA** in the Graphics tab's **Upscaler** row on
NVIDIA RTX cards (`dlss=` in `longshot_options.ini`, `RX_VK_DLSS=0/1/2` for one run): DLSS Super Resolution in place of
the game's own upscaler, with camera jitter (**NVIDIA Reflex** needs no SDK and is in
every build). NVIDIA's files are NVIDIA's, under its own license, the
[NVIDIA RTX SDKs license](https://github.com/NVIDIA/DLSS/blob/9981c6350dc58c0ce21f024fd65e31e851c31689/LICENSE.txt).
Longshot never includes or redistributes them: each build downloads them for itself, and only after you accept that
license. Never share a game folder built with them, and do not post builds made with them.

- **The Longshot Launcher, Windows and Linux:** turn on **DLSS** in the build's settings. It is off by default and shows
  NVIDIA's license when you turn it on. The build downloads the SDK into the launcher's folder and puts DLSS's runtime
  (`nvngx_dlss.dll`, `libnvidia-ngx-dlss.so.<version>` on Linux) and DLSS Frame Generation's (`nvngx_dlssg.dll`,
  `libnvidia-ngx-dlssg.so.<version>`) in the game folder's `ngx` folder. An update of that folder keeps DLSS.
- **A builder from the command line:** `--cli` with make_game.py's `--fetch-ngx-sdk --accept-nvidia-license`
  (below: what they do). The SDK goes to `work/ngx-sdk` in the Longshot folder, or to `LONGSHOT_NGX_SDK` when that is set
  (the Builder drops a variable set on the PC, so a launcher has none).
- **Windows, the command line:** `python -I scripts/ngx_sdk.py --platform windows <sdk folder>` asks you to accept
  NVIDIA's license (or pass `--accept-nvidia-license`) and fetches the headers, `nvsdk_ngx_s.lib`, `nvngx_dlss.dll` and
  `nvngx_dlssg.dll`, each checked against its SHA-256. Then build with `LONGSHOT_NGX_SDK=<sdk folder>`: `make_game.py`
  copies both runtimes into `<game folder>\ngx`. NGX is also pointed at `%LOCALAPPDATA%\Longshot\ngx`, and at
  `RX_VK_DLSS_DIR` when it is set, and uses the newest of each it finds across them (to try a given one, make sure no
  other folder has a newer one). Without `nvngx_dlssg.dll` there, frame generation takes the driver's own copy, which
  may be older: one that makes 1 frame at most, or refuses every set. Never copy NGX's core, `_nvngx.dll`, beside
  the game: it comes with NVIDIA's driver.
- **Linux, a build from source:** `scripts/linux/ngx-sdk.sh <sdk folder>` (it runs `scripts/ngx_sdk.py --platform
  linux`) asks you to accept NVIDIA's license and fetches `libnvsdk_ngx.a` for the build,
  `libnvidia-ngx-dlss.so.<version>`, DLSS's runtime, and `libnvidia-ngx-dlssg.so.<version>`, frame generation's. Then
  run the build with `LONGSHOT_NGX_SDK=<sdk folder>` set: `make_game.py` puts both runtimes in the game folder's `ngx`
  folder. NGX's core comes with NVIDIA's Linux driver.
- **Linux, a game folder with other than plain English letters in its path:** NVIDIA's NGX on Linux loads DLSS's
  runtimes only from a folder whose whole path is ASCII. A game in `~/Games/Longshot` needs nothing more. A game in a
  folder such as `~/Games/Longshot-Café` (the `é` is not ASCII) cannot use the runtimes in its own `ngx` folder, and the
  log and the Insert menu say so: copy `libnvidia-ngx-dlss.so.*` and `libnvidia-ngx-dlssg.so.*` from there into
  `$XDG_DATA_HOME/longshot/ngx`
  (`~/.local/share/longshot/ngx`), which the game also looks in, or into any other folder with an ASCII path and name
  that folder in `RX_VK_DLSS_DIR`.

The **DLSS model** row chooses DLSS's network, by NVIDIA's preset letter, for every mode: **Transformer (K)**, the
transformer model and the one tested most here (the default); **Transformer 2 (M)**, NVIDIA's second-generation
transformer as it picks it for Performance; **Transformer 2 (L)**, the same generation as tuned for Ultra Performance;
**NVIDIA default**, which lets DLSS's runtime pick for each mode, so it can change with a runtime update. The runtime
the SDK comes with (310.9.16) has all of them. A change applies at once, without a restart (`dlss_model=` in
`longshot_options.ini`, 0 to 3 in that order). `RX_VK_DLSS_PRESET=<letter>` (or `0` for NVIDIA's default) chooses
the preset for one run and wins over the row; the log's `vk: NVIDIA DLSS: ... preset <letter>` line says which one was
asked for, and with `RX_VK_DLSS_LOG=1` NGX's own `Using App hint Preset <letter>` lines say which it took.

**8 GB cards:** DLAA draws the 3D at full size, and DLSS and DLAA add images of their own beside the game's. In busy
areas that can pass what the driver allows the game, and the game then stutters: Quality or Balanced is safer there.
The log's `vk: status:` line every 600 frames shows the video memory in use of the allowance, with DLSS's details
(its images' size, the jitter and the actor motion) appended while it draws, and a `vk: over the driver's
video-memory allowance` line says when the game passed it.

The game's log says whether DLSS started, and why not when it did not (`vk: NVIDIA DLSS:` lines in `longshot_log.txt`).
NGX starts on demand: when the Insert menu first opens (to know whether the Upscaler can offer DLSS) or at the first
gameplay upscale with DLSS chosen, so on the title screen the log has no NGX lines yet.
With `RX_VK_DLSS_LOG=1` NGX's own lines go there too (`ngx:`). Some of them look like errors but are not:
`failed to load NGXCore: 126` (twice) is NVIDIA's library looking for `_nvngx.dll` and `nvngx.dll` beside the game, which
are not there on purpose, before it loads the driver's; `Feature ... failed to load from cache` is NGX finding no model
of its own downloads (`C:\ProgramData\NVIDIA\NGX\models`) before it uses the game folder's `nvngx_dlss.dll`.

</details>

<details>
<summary>AMD FSR 4 (optional, AMD's DLLs not included)</summary>

A game built with FSR 4 offers **AMD FSR 4** and **AMD FSR 4 Native AA** in the Graphics tab's **Upscaler** row: AMD's
machine-learned temporal upscaler in place of the game's own, on the same inputs as DLSS (the scene's colour and depth, the
camera's and the characters' motion, and the camera jitter). Native AA draws the 3D at full size and smooths its edges.
With HDR on **Native**, it takes the game's HDR colour as linear light, so highlights above the paper white are kept
(`RX_VK_FSR4_HDR=0`: cut at the paper white, as before).
Unlike FSR 3.1 (above), it runs AMD's own DLLs, so it needs them beside the game. Choose it in the menu (`dlss=4` or `5`
in `longshot_options.ini`, `RX_VK_FSR4=1` or `2` for one run, `RX_VK_FSR4=0` to leave it out).

- **Windows, the Longshot Launcher:** turn on **FSR 4** in the build's settings. It shows AMD's license first and stays
  on only if you accept; the build then fetches AMD's DLLs for you (below), into the launcher's folder and the game
  folder.
- **The command line:** `make_game.py --fsr` (or `LONGSHOT_FSR=1` for `nx2/build.py`). Rebuilding a folder built with
  it (`--update`) keeps it; `--no-fsr` takes it out.
- **AMD's files:** Longshot includes only AMD's MIT-licensed API headers (`third_party/fidelityfx`). Put
  `amd_fidelityfx_loader_vk.dll` and `amd_fidelityfx_upscaler_vk.dll` from the
  [FidelityFX SDK](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK) 2.4 release (`Kits/FidelityFX/signedbin`)
  beside the game's exe (`bin\longshot.exe`) or in the game folder. (`RX_VK_FSR_DIR` names another folder, for testing:
  set it in the environment or a `HOS_ENV_FILE` file; `longshot_env.txt` takes no paths.) Without them the Upscaler row
  says so and offers the rest. On Linux, a Vulkan FSR 4 library (`amd_fidelityfx_upscaler_vk.so`, such as fsr4-vk's)
  takes their place: AMD publishes none for Linux, so you put it beside the game yourself. A Linux build with FSR 4
  (`--fsr`) compiles it into the Vulkan back end, which every build compiles.
- **Or fetched for you (Windows):** `make_game.py --fsr --fetch-fsr-runtime --accept-amd-license` downloads those two
  DLLs and AMD's license from AMD's FidelityFX SDK repository at the v2.4.0 tag, checks each against its SHA-256, and
  puts them in the game folder (`LONGSHOT_FFX_SDK=<folder>` keeps them between builds, default `workfx-sdk`).
  `python -I scripts/ffx_sdk.py --accept-amd-license --game <game folder>` does the same for a game built already.
  Without `--accept-amd-license` nothing is downloaded (the script asks when run by hand), as with NVIDIA's license for
  DLSS. AMD DLLs already in the game folder that are not v2.4.0's (a newer SDK put there by hand) are left alone, and
  the build says so. They are AMD's, under
  [AMD's FSR SDK license](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/7539ae8b6568ad29ed78a753404351daf3b5cca7/Kits/FidelityFX/docs/license.md)
  (`LICENSE-AMD-FSR-SDK.md` beside them). On Linux the flag does nothing but say so.

The log's `vk: AMD FSR 4:` lines say which of AMD's upscalers it found for the card and which it uses (the newest FSR 4;
never an older FSR in its place), and why not when it did not start. Tested on an RX 6700 XT (RDNA 2).

</details>

<details>
<summary>Intel XeSS (optional, Windows, Intel's DLL not included)</summary>

Every Windows build offers **Intel XeSS** and **Intel XeSS Native AA** in the Graphics tab's **Upscaler** row once Intel's
`libxess.dll` is beside the game: XeSS Super Resolution, Intel's machine-learned temporal upscaler, through its Vulkan
API, in place of the game's own, on the same inputs as DLSS and FSR 4 (the scene's colour and depth, the camera's and the
characters' motion, and the camera jitter). Native AA draws the 3D at full size and smooths its edges. It runs on Intel
Arc and Iris Xe GPUs, and on other GPUs whose Vulkan driver has what XeSS asks for (storage image writes without a format,
`VK_EXT_mutable_descriptor_type`). With HDR on **Native**, it takes the game's HDR colour as linear light, with XeSS's own
exposure (`RX_VK_XESS_HDR=0`: cut at the paper white, as with the other upscalers before).
Choose it in the menu (`dlss=7` or `8` in `longshot_options.ini`, `RX_VK_XESS=1` or `2` for one run, `RX_VK_XESS=0` to
leave it out: then the library is not even loaded). The Auto (this PC) preset never chooses it; it says in the log when
the DLL is there.

- **Intel's file:** Longshot includes only Intel's MIT-licensed XeSS API headers (`third_party/xess`). `libxess.dll` is
  Intel's, under the Intel Simplified Software License, and Longshot does not ship it. Take it from the
  `bin` folder of a recent [Intel XeSS SDK](https://github.com/intel/xess/releases) release (one whose `inc` folder has
  `xess_vk.h`: the Vulkan path), or from a game that ships it, and put it beside the game's exe (`bin\longshot.exe`) or in the game folder.
  (`RX_VK_XESS_DIR` names another folder, for testing: set it in the environment or a `HOS_ENV_FILE` file;
  `longshot_env.txt` takes no paths.) Without it the Upscaler row does not offer XeSS and its hint says why.
- **Or fetched for you:** RecompLauncher's **Intel XeSS** tick (Windows; off until you accept Intel's license, which it
  shows), or `make_game.py --fetch-xess-runtime --accept-intel-license`, downloads `libxess.dll` 2.0.2.68 (78 MB, signed
  by Intel) with Intel's license and third-party notices from Intel's XeSS SDK repository, at the commit
  `third_party/xess`'s headers come from, checks each against its SHA-256, and puts them in the game folder
  (`LONGSHOT_XESS_SDK=<folder>` keeps them between builds, default `work/xess-sdk`).
  `python -I scripts/xess_sdk.py --accept-intel-license --game <game folder>` does the same for a game built already.
  Without `--accept-intel-license` nothing is downloaded (the script asks when run by hand), as with AMD's and NVIDIA's
  licenses. A `libxess.dll` already in the game folder that is not this one (a newer SDK's, put there by hand) is left
  alone, and the build says so. It is Intel's, under the
  [Intel Simplified Software License](https://github.com/intel/xess/blob/de0fb9c1c510661c571164e1418ceca8101dab69/LICENSE.txt)
  (`LICENSE-INTEL-XESS-SDK.txt` beside it). On Linux the flag does nothing but say so.
- **Windows only.** Intel makes no XeSS library for Linux, so a Linux build has no XeSS, and the row says so. XeSS Frame
  Generation and Intel's latency reduction (XeLL) are Direct3D 12 only and are not in Longshot.

The Render resolution row's stops run under the XeSS mode whose range holds them (since XeSS 1.3 each mode takes any size
from its own ratio up to the output: Ultra Quality Plus 1.3x, Ultra Quality 1.5x, Quality 1.7x, Balanced 2x, Performance
2.3x, Ultra Performance 3x), so the game's dynamic resolution at Auto runs under one mode too. The log's
`vk: Intel XeSS:` lines say which DLL it loaded and its version, the extensions and features it asked of the Vulkan
driver, the mode it was made with, and why not when it did not start. Not yet run in the game: reports from Intel GPUs
are welcome.

</details>

<details>
<summary>Switches for testing (longshot_env.txt)</summary>

Switches to test (`RX_VK_NOOPT=1` and the others) go in `longshot_env.txt` in the game folder, one `NAME=VALUE` a
line, on Windows and Linux. The running game reads it, not the builder. A test run can name another file with
`HOS_ENV_FILE=<path>`; the game then reads that one instead and leaves `longshot_env.txt` alone.
`longshot_env.txt` sets only the game's own switches (names that start with `RX_`, `HOS_`, `LONGSHOT_`, `EDEN_`,
`NVN_` or `SDL_`, but not SDL's library names such as `SDL_VULKAN_LIBRARY`), and no value that is a path except
`RX_VK_DLSS_DIR`'s (a folder on this PC, not a network share), and no value at all for the switches that name a file
or folder (`HOS_AUDIO_DUMP`, `RX_VK_SHOTS` and the like) or press buttons on a timer (`HOS_INPUT_SCRIPT`,
`RX_VK_MENU_SCRIPT`); other lines are left out, and the log says so. Switches that need a path, or other variables, go
in a file named by `HOS_ENV_FILE`.
A switch set in the environment the game is started with (a shell's `HOS_FULLSCREEN=0 ./longshot`, a launcher's own)
goes over the same name in the file, and the log says so; within the file, a later line replaces an earlier one. A
switch that stands in for a menu row (`RX_VK_PACE`, `RX_VK_UPSCALE`, `RX_VK_FRAMEGEN_MULT`, `HOS_REALTIME_STEP`,
`HOS_DYNRES`, `HOS_OUTPUT_RES`) applies to that run only: `longshot_options.ini` keeps the value chosen in the menu.

To enable the debug tools, turn **Debug tools** on in the menu's Debug tab (or add `HOS_OOT_CHEATS=1` to
`longshot_env.txt` in the game folder). The tools then show below it on the same tab. **Damage taken** is in the
**Player** group beside **Heart protection**: Half, Normal (the default), Double or Quadruple (`damage_taken=0/1/2/3`
in `longshot_options.ini`). It applies only while Debug tools is on: off, Link takes Normal damage, and the saved
choice returns when the tools do. Heart protection takes priority while enabled. Half scales the full hit before life
is clamped to zero: with two quarter hearts left, a four-quarter hit still kills. The tab's Developer section, always
shown, has the Descriptor reuse, Sampler cache, Robust buffers and ASTC as RGBA8 rows, and on Intel graphics on
Windows the Intel shader compiles row.

A save whose minimap stopped showing after a Teleport on an earlier build (the area left had hidden it, and the flag
stayed set in the save) is mended by the Teleport itself: teleport to an outdoor area, wait until you can move, then
save from the game's System menu. The minimap then stays after a restart.

`RX_VK_UI_GAME_FONT=0` draws the menu with the included font (Libertinus) even when the game's files have the game's own
menu font. `RX_VK_UI_PROMPTS=pad` (or `keys`) shows the controller's (or the keyboard's) prompts whatever was used last,
for screenshots.

For a first run on a 4 GB card such as a GTX 1650, `RX_VK_LOW_SPEC=1` starts with the Minimum (4GB) preset, with
frame generation and upscaling off. It applies only when there is no `longshot_options.ini` yet; after that, change
the settings in the in-game menu.

On **Minimum (4GB)**, when the driver reports a nonzero VRAM budget of at most 4.5 GiB, or the GPU is integrated
and the PC has 16 GB of RAM or less (its "VRAM" is that RAM, whatever budget the driver reports), sampled ASTC textures
use a 1024 dimension limit and an 8 MiB decoded-size threshold for leaving out their top mip levels. This lowers texture
detail to reduce the working set. Other presets and larger or unknown budgets keep the 2048 / 64 MiB defaults;
uncompressed render targets, depth images and the separate `RX_VK_TEX_MAX_MB` allocation limit keep their handling.
The driver budget is sampled at the first texture creation and kept for the session. It is a heuristic, not a physical
VRAM measurement: other applications can lower the budget even on a larger card.

`RX_VK_LOW_VRAM_MIPS=0` (also `off`, `false` or `no`) disables the automatic rule. `RX_VK_TEX_MAX_DIM` overrides only
the dimension limit, so setting it to 2048 still leaves the automatic 8 MiB threshold active on Minimum with a small
budget. `RX_VK_TEX_SKIP_MB` overrides that threshold using its existing parsing. For a more aggressive experiment,
use `RX_VK_TEX_MAX_DIM=512 RX_VK_TEX_SKIP_MB=2`. Changes to these environment switches take effect after a restart.

`RX_VK_MOVIE_UPSCALE=linear` replaces the movies' neural upscale with the plain linear one. The default, `auto`, keeps
the networks and falls back only when a movie frame takes the GPU more than 40 ms. That is on NVIDIA cards: on other
cards, or where the GPU cannot multiply the networks' matrices itself, the movies always get the linear upscale. It does
not change the gameplay upscaler, and movies can look softer. `RX_VK_NETWORK_KEEP=all` takes precedence and keeps the
movie networks.

A movie whose pictures take longer to decode than the time between them is shown, rather than skipped until the sound
has moved on: on a slow CPU the opening used to drop most of its pictures. How fast the decoder is counts from its recent
pictures, so a slow first picture does not stop a decoder that keeps up from catching up later, and catching up takes at
most one picture's time in a frame. With a decoder slower than the movie, the picture can fall behind the sound.
`HOS_VIDEO_NO_CATCHUP=1` skips nothing. Each picture is waited for for about a tenth of a second
(`HOS_VIDEO_ACQUIRE_TRIES=1`: a count of sleeps instead of the clock). After a movie stops or is flushed, and nothing new
has been submitted, there is no wait. The first picture can still be black on some PCs; why is not known yet.

A PC with 4 logical processors or fewer decodes the movies on 2 threads rather than one per processor, so the decoder
leaves the game's own threads their time. A 4-thread laptop with Intel UHD Graphics showed the title and the opening
movie at about 35 fps of 60, with the game's thread waiting 14 to 24 ms on most frames; that report was not run again
with this change. `HOS_DAV1D_THREADS=<n>` sets the count on any PC.

On Windows, Intel's compiler (UHD and Arc) is entered by one thread. A fault inside it leaves that shader out instead
of closing the game, and the program is written to `longshot_driver_faults.txt` so that later starts on the same
driver leave it out too (delete the file to try again). This covers the game's shaders and the renderer's own (texture
decoding, upscaling, frame generation). A draw that only asks whether its pipeline is ready never waits behind another
thread's compile: while one is compiling, the answer is "not yet", even for a pipeline already in a cache, and the
draw is left out until a worker has made it (the log's `vk: asked cold: Intel's gate busy` lines count these). The
renderer's own pipelines made at their first use (the movie's upscale, the texture decoders, a masked clear, the
write-back and late-page fixes) do wait for a compile under way. Whether the driver keeps working after a caught fault
has not been tested. The first scene can still pause while shaders compile, especially when they were not prepared at
the build. If a build's preparation stopped ("the GPU driver stopped responding"), the game leaves a
`longshot_prepare_skipped` file in its `cache` folder and every start after that builds shaders only when first drawn.
Build again with **Prepare shaders now** ticked, or delete that file, to prepare them with this fix.
The Debug tab's **Intel shader compiles** row (Developer section, shown on Intel graphics on Windows) chooses: **One at
a time**, the safe choice on a CPU with 4 cores or fewer; **Side by side**, faster builds on a CPU with many cores, but
Intel's driver can crash the game while building them; or **Auto**, the default: side by side on the Arc B-series with
driver 101.9154 or newer (on a B580 a new area's first visit froze the picture for four to seven minutes one at a time,
and not at all side by side, with no fault in ten areas), one at a time on every other Intel GPU. It applies after a
restart. `RX_VK_NO_INTEL_SERIAL=1` is Side by side for a run and `=0` One at a time, whatever the row says. NVIDIA, AMD, and Intel on other systems are
unchanged.

`RX_VK_SKIP_CODE=<prefix,...>` leaves out the draws of every program with a stage whose native code key starts with one
of the prefixes (the keys the log prints after "its code:"). On a Steam Deck, `RX_VK_SKIP_CODE=2c56dc1006942f74`
avoids a GPU hang in Kokiri Forest and the Great Deku Tree: the GPU runs for seconds in the programs that share that
task shader (a culling pass) until the kernel resets it ([#817](https://github.com/NotRexed/Hookshot/issues/817)).
It is a workaround, not a fix: those are task + mesh programs, so the geometry they draw goes with them, and key models
are missing while it is set. Off unless set.

The switches for scripted test runs, screenshots, frame dumps and profiles are in the
[development guide](docs/development.md#switches-for-testing-and-debugging).

`RX_VK_WB_MAX_KB=<KB>` (off by default; 1024 is the tested value) is the size from which a written GPU binding may go
without most of its write-back. The game
writes tens of MB of culling lists and per-frame buffers through the GPU that the CPU never reads, and copying them back
sat on every submit's memory sync: in Hyrule Field on a 10 GB RTX 3080, about 66 MB and 4.4 ms a submit, holding the
scene at 45-48 fps. The CPU does read and write parts of some large ones, so a binding over the size is written back
whole for the first `RX_VK_WB_PROBATION` submissions that write it (default 8), and after that only the ranges the CPU
read or wrote meanwhile, and its first and last page when it shares them, are written back (`gpu/vk/wb_learn.h`). The
trade-off: a range the CPU first touches only after that (read more rarely than every 8 frames) gets the stale copy
once, and is written back from then on; the log names each one (`in a binding whose write-back was left out`).
Unset or `0`, every written binding is copied back whole. It is off by default because, measured so far (an RTX 3060 Ti
on Windows, an RX 580 on Linux with its mirror forced on), it copies back ~50 MB a frame less at the same frame rate:
results from laptops and other slow PCIe links are wanted. `RX_VK_LOG_WB_SKIPS=1` lists the bindings left out every
600 submits.

`RX_VK_PROGRAM_CACHE_ZSTD=1` keeps the translated shader programs in `longshot_programs.zst.bin`, each record
compressed with zstd and unpacked when it is read. The uncompressed cache is about 6.9 GB; in the reference cache
(46,587 programs) zstd's level 3 made it 1.83 GB (3.75x). It is a file of its own: an existing
`longshot_programs.bin` is left alone, and the first run translates the programs into the compressed cache.

</details>

## How it works

1. `nx2/lift.py` translates the game's ARM64 code to C.
2. `nx2/build.py` compiles that C together with `host/`, which plays the part of the console's system.
3. `host/nvn_native.c` records the game's NVN2 graphics calls, and `gpu/vk/` replays them on Vulkan.
4. Switch 2 Ampere shaders are lowered to Maxwell (`lower_to_maxwell.h`) and translated to SPIR-V by Eden's shader
   recompiler.

More detail is in the [development guide](docs/development.md) and the [design notes](docs/design.md).

## Want to help?

Testing on other graphics cards and reporting the result already helps a lot, and so does playing further into the
game than anyone has yet.

### Send your pipeline list

The first time the game meets a pipeline it builds it on the spot, which stutters. Releases build a list of known
pipelines before the title, and every player's play adds to it. To send yours:

1. In the game, open the menu (Insert or F1), go to the **Debug** tab and choose **Save my pipeline list** (under Developer). Or,
   from a clone, run `python -I nx2/pipeline_share.py <game folder>`.
2. It saves `longshot-pipelines-<date>.zip` in your Downloads folder, copies a filled-in report to the clipboard and
   opens the [Submit your pipelines](https://git.magicbean.dev/MagicBeanDev/Longshot/issues/14) issue in your browser.
3. Attach the zip to a comment there, paste the report and fill in where you played.

The zip holds only `longshot_pipeline_states.bin` (shader hashes, formats and draw state, and the hashes of the compute
programs your play ran, so that a release can keep those ready too: no game code, game data or personal info) and the
report (the version, OS, GPU and driver, output and render resolution, upscaler, frame
generation, HDR and the mods the game loaded). Nothing is uploaded for you. If the list is missing or damaged, it says
so and saves nothing. In a run without a window (`HOS_HEADLESS`) the menu saves the zip but opens nothing.

Bigger ways to help:

- **Linux testing.** The Linux build (`host/posix`) has been tested on several distributions and cards: reports from
  others still help.
- **Fixes in your own fork.** Fork the project, fix what you find and send the change back. Keep game files, keys and
  anything generated from your game out of it.

[CONTRIBUTING.md](CONTRIBUTING.md) has the setup, the test commands, the code conventions and what a good pull request
looks like.

## License

Longshot is free software under the [GNU General Public License, version 3 or later](LICENSE) (GPL-3.0-or-later).
It is a modified version of Hookshot, which NotRexed released under the same license; the changes made since the fork
are Longshot's.

    Hookshot: Copyright (C) 2026 NotRexed and the Hookshot contributors
    Longshot: Copyright (C) 2026 the Longshot contributors

It includes code from Eden and suyu, which are under the GPL too. Third-party parts under other licenses that
work with the GPL (Sirit, the Vulkan Memory Allocator, SDL, Opus, Dear ImGui, zstd, the Libertinus fonts, zbic and the
a64-kit tools) keep their own notices in their folders.

Longshot is an unofficial fan project, not affiliated with or endorsed by Nintendo. The Legend of Zelda and Ocarina of
Time are trademarks of Nintendo.

## Credits

Longshot is a fork of NotRexed's [Hookshot](https://github.com/NotRexed/Hookshot), and exists thanks to the work of many
people:

- **NotRexed**: [Hookshot](https://github.com/NotRexed/Hookshot), which Longshot is a fork of, BlueShell (Mario Kart World),
  Skybound (Tears of the Kingdom) and the a64-kit tools.
- **[Eden](https://git.eden-emu.dev/eden-emu/eden)** and **Eglantine**: the shader recompiler and Switch 2 shader work.
- **suyu**: the graphics decoders and the AArch64 translator.
- **[seedds/nxnative](https://github.com/seedds/nxnative)**: reference for how task and mesh shaders work.
- **[EvilBoyce / evilmfkr](https://github.com/evilmfkr/Hookshot)** (EvilOOT): the GPU culling that brings back Kokiri's foliage
  (trees, ferns, grass, falling leaves, fireflies), the ambient light probes, the pond's water, the game's own fog, the freeze
  after Load (Horizon's mutex handoff) and the exposure flicker fix.
- **[zortos293/Lichii](https://github.com/zortos293/Lichii)**: reference for Ampere shader lowering, and its compare-exchange retry fix.
- **[mstorsjo/llvm-mingw](https://github.com/mstorsjo/llvm-mingw)**: the bundled Windows compiler (no Visual Studio needed).
- **NVIDIA DLSS**: builds made with NVIDIA's DLSS SDK (optional, downloaded by each build, never bundled) use NVIDIA DLSS
  Super Resolution, NVIDIA's technology. NVIDIA and DLSS are trademarks of NVIDIA Corporation.
- **AMD FSR 3.1 and FSR 4**: every build has AMD FidelityFX Super Resolution 3.1, built from AMD's MIT-licensed FidelityFX
  SDK shaders; builds made with FSR 4 (optional; AMD's DLLs are never bundled) use AMD FidelityFX Super Resolution 4
  through AMD's MIT-licensed FSR API headers. Both are AMD's technology. AMD, Radeon and FidelityFX are trademarks of Advanced Micro
  Devices, Inc.
- **[kinnay/zbic](https://github.com/kinnay/zbic)**, **Opus** and **SDL**: decompression, audio and controllers.
- **[sostenesfreitas/ocarina-recomp](https://github.com/sostenesfreitas/ocarina-recomp)**: this README's layout, and its notes
  confirmed the mesh stage's control section.
- **[MythosOfs/Orchid](https://github.com/MythosOfs/Orchid)**: the fog and exposure workaround the game used before
  its fog and lighting were fixed.
- **Mesa (NAK)**: the reference for Ampere's instruction encoding.
- Everyone who sent fixes: SkippySteve (Linux and the AppImage, and many more), monsterlag, klimovich008, hckrman101 (NVIDIA
  DLSS, DLAA and Reflex, the exposure flicker and fog fixes, build speed, CI), madebyjordan (AMD FSR 4.1), N3oTraX (FSR 4 on
  Linux), shayderrr, gab9281, Wpaif, nicholasgower, ZurielAtr, Joey-Magnus, Zorkats,
  d3zessete, chrispository, vzeol, leonidasmark, Segmentree, ShyVortex, hiKareeem and i900gaming, among others.
