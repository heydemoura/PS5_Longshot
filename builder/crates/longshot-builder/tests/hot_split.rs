//! `LONGSHOT_HOT_SPLIT` (on by default, `0` off), the lift's half and the compile's together: a hot unit written for
//! hot-cold splitting (`llvm::Module::split_cold`) and compiled with the hot units' flags
//! (`build_compile::hotsplit::flags`) has the region of a function that the profile never saw run moved out into a cold
//! function of its own, for the Windows and the Linux target; written without the switch, compiled without the flags,
//! or with the function left out, nothing moves.
//!
//! The function is a hot function's shape: a branch that the profile says was never taken, whose side reads and writes
//! guest memory, calls another guest function and returns, and a side that ran, which returns at once. Built for this
//! PC and run from both sides, the program with that side moved out gives every register and every byte of guest memory
//! the program without the switch gives. A never-taken branch into a join (a block that ran through another edge) is
//! not taken for cold.

use mir::{BinOp, Dest, Edge, Flag, Function, Loc, Op, Space, Term, Type};
use std::collections::BTreeSet;

const ENTRY: u64 = 0x8000_1000;

/// The clang the test compiles with: `CC` when it names a clang that runs (another compiler, gcc, cannot read the IR),
/// else that of the PATH (`LONGSHOT_NO_CLANG=1` passes over the test without one).
fn clang() -> Option<String> {
    let found = std::env::var("CC").ok().into_iter().chain(["clang".to_string(), "clang-23".to_string()]).find(|c| {
        std::process::Command::new(c)
            .arg("--version")
            .output()
            .is_ok_and(|o| o.status.success() && String::from_utf8_lossy(&o.stdout).contains("clang version"))
    });
    if found.is_none() && std::env::var("LONGSHOT_NO_CLANG").as_deref() != Ok("1") {
        panic!("no clang to compile the lifted IR with (LONGSHOT_NO_CLANG=1 passes over these checks)");
    }
    found
}

/// b0 branches on Z: to b1, which the profile never saw run (loads and stores of guest memory, a guest call, a
/// return), else to b2 (X0 + 1, a return).
fn function() -> Function {
    let mut f = Function::new(ENTRY);
    let (b0, b1, b2) = (f.start, f.add_block(ENTRY + 0x10), f.add_block(ENTRY + 0x40));
    let z = f.push(b0, Op::Get(Loc::Flag(Flag::Z)));
    f.terminate(
        b0,
        Term::Branch { cond: z, then: Edge::new(Dest::Block(b1), false), otherwise: Edge::new(Dest::Block(b2), false) },
    );
    let x0 = f.push(b1, Op::Get(Loc::X(0)));
    let mut acc = f.push(b1, Op::Get(Loc::X(1)));
    for k in 0..6u64 {
        let off = f.push(b1, Op::Const { ty: Type::I64, value: u128::from(8 * k + 16) });
        let addr = f.push(b1, Op::Bin(BinOp::Add, acc, off));
        let v = f.push(b1, Op::Load { ty: Type::I64, addr, order: None });
        let mixed = f.push(b1, Op::Bin(BinOp::Xor, v, x0));
        f.push(b1, Op::Store { addr, value: mixed, space: Space::Guest, order: None });
        acc = f.push(b1, Op::Bin(BinOp::Add, acc, mixed));
    }
    f.push(b1, Op::Set(Loc::X(2), acc));
    f.push(b1, Op::Call { target: 0x8000_2000 });
    let back = f.push(b1, Op::Get(Loc::X(0)));
    let sum = f.push(b1, Op::Bin(BinOp::Add, back, acc));
    f.push(b1, Op::Set(Loc::X(0), sum));
    f.terminate(b1, Term::Return);
    let x0 = f.push(b2, Op::Get(Loc::X(0)));
    let one = f.push(b2, Op::Const { ty: Type::I64, value: 1 });
    let inc = f.push(b2, Op::Bin(BinOp::Add, x0, one));
    f.push(b2, Op::Set(Loc::X(0), inc));
    f.terminate(b2, Term::Return);
    f.verify().expect("a well-formed function");
    f
}

/// The profile: entered 1000 times, its branch's block ran 1000 times and was never taken (no taken count, as a profile
/// file has it), b2 ran 1000 times.
fn profile() -> profile::Profile {
    let f = profile::Function {
        hash: 1,
        entries: 1000,
        blocks: [(ENTRY, 1000), (ENTRY + 0x40, 1000)].into(),
        ..profile::Function::default()
    };
    profile::Profile { build: String::new(), functions: [(ENTRY, f)].into() }
}

/// The IR of `f` with the counts of `p` (`split`: written for hot-cold splitting, with these functions left out).
fn ir(f: &Function, p: &profile::Profile, split: bool, skip: &[u64]) -> String {
    let mut m = llvm::Module::new(llvm::Options { polls: false, mark_bitmap: false, ..Default::default() });
    if split {
        m.split_cold(skip.iter().copied().collect::<BTreeSet<u64>>());
    }
    m.add(f, llvm::Runs { hash: 1, counts: p.function(f.entry, 1), cold: false });
    m.finish()
}

/// `text` compiled by clang at the hot units' level, with the hot units' hot-split flags (`flags`), for `target`
/// (None: this PC's), into `dir`: assembly (`out` "s") or an object (`out` "o").
fn compile(
    clang: &str,
    text: &str,
    target: Option<&str>,
    flags: bool,
    dir: &std::path::Path,
    out: &str,
) -> std::path::PathBuf {
    std::fs::create_dir_all(dir).unwrap();
    let ll = dir.join("t.ll");
    std::fs::write(&ll, text).unwrap();
    let file = dir.join(format!("t.{out}"));
    let r = std::process::Command::new(clang)
        .arg(if out == "s" { "-S" } else { "-c" })
        .args(["-O2", "-Wno-override-module", "-mcx16", "-ffunction-sections", "-ffp-contract=off"])
        .args(target.map(|t| format!("--target={t}")))
        .args(build_compile::hotsplit::flags(flags))
        .args(["-x", "ir"])
        .arg(&ll)
        .arg("-o")
        .arg(&file)
        .output()
        .unwrap();
    assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
    file
}

fn tmp(name: &str) -> std::path::PathBuf {
    std::path::Path::new(env!("CARGO_TARGET_TMPDIR")).join(name)
}

/// The module's IR (`split`, `skip` as [`ir`]) compiled for `target` with or without the hot flags, as assembly (None:
/// no clang).
fn compiled(target: &str, split: bool, skip: &[u64], flags: bool) -> Option<String> {
    let clang = clang()?;
    let dir = tmp(&format!("hot-split-{target}-{split}-{}-{flags}", skip.len()));
    let s = compile(&clang, &ir(&function(), &profile(), split, skip), Some(target), flags, &dir, "s");
    Some(std::fs::read_to_string(s).unwrap())
}

/// The functions the assembly defines, by name, each with its body.
fn bodies(asm: &str) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = Vec::new();
    for line in asm.lines() {
        let label = line.split_whitespace().next().and_then(|w| w.strip_suffix(':'));
        if let Some(name) = label.filter(|n| n.starts_with("f_")) {
            out.push((name.to_string(), String::new()));
        } else if let Some((_, body)) = out.last_mut() {
            body.push_str(line);
            body.push('\n');
        }
    }
    out
}

#[test]
fn the_region_the_profile_never_saw_run_moves_out_and_only_with_the_switch() {
    for target in ["x86_64-w64-mingw32", "x86_64-unknown-linux-gnu"] {
        let Some(on) = compiled(target, true, &[], true) else { return };
        let f = bodies(&on);
        let cold: Vec<&(String, String)> = f.iter().filter(|(n, _)| n.contains(".cold")).collect();
        assert_eq!(cold.len(), 1, "{target}: one cold function:\n{on}");
        let (hot, hot_body) = f.iter().find(|(n, _)| n == "f_80001000").expect("the function itself");
        assert!(hot_body.contains(&cold[0].0), "{target}: {hot} calls the cold part:\n{on}");
        assert!(cold[0].1.contains("f_80002000"), "{target}: the guest call is in the cold part:\n{on}");
        assert!(!hot_body.contains("f_80002000"), "{target}: and no longer in the hot one:\n{on}");
        // (the controls: written without the switch, compiled without the flags, the function left out)
        for (split, skip, flags, what) in [
            (false, &[][..], true, "not written for it"),
            (true, &[][..], false, "no flags"),
            (true, &[ENTRY][..], true, "left out"),
        ] {
            let asm = compiled(target, split, skip, flags).unwrap();
            assert!(!asm.contains(".cold"), "{target}, {what}: nothing moves:\n{asm}");
        }
    }
}

#[test]
fn the_switch_is_read_by_value_and_a_bad_skip_list_is_refused() {
    use build_driver::stages::hot_split;
    for off in ["0", "", "off", "no", "false", " 0 "] {
        assert_eq!(hot_split(Some(off), Some("f_80001000")), Ok(None), "{off:?}: off");
    }
    for on in [None, Some("1"), Some("true"), Some("yes")] {
        assert_eq!(hot_split(on, None), Ok(Some(BTreeSet::new())), "{on:?}: on");
    }
    assert_eq!(
        hot_split(Some("1"), Some("f_80001000, 0x80002000 80003000,,")),
        Ok(Some([0x8000_1000, 0x8000_2000, 0x8000_3000].into()))
    );
    assert_eq!(hot_split(Some("1"), Some("f_80001000,nope")), Err("nope".to_string()));
    let s = build_compile::Settings::from_lookup(|n| (n == "LONGSHOT_HOT_SPLIT").then(|| "1".to_string()));
    assert!(s.hot_split, "the compile's half reads the same switch");
    for v in ["0", "off", ""] {
        let s = build_compile::Settings::from_lookup(|n| (n == "LONGSHOT_HOT_SPLIT").then(|| v.to_string()));
        assert!(!s.hot_split, "{v:?}: off");
    }
}

#[test]
fn unset_is_on_and_0_is_off_in_the_lift_the_compile_and_the_compile_s_check() {
    use build_driver::stages::{check_hot_split_as, hot_split};
    // (the lift's half: unset, the hot units are made for the splitting, none left out; 0, as without the switch)
    assert_eq!(hot_split(None, None), Ok(Some(BTreeSet::new())), "unset: on");
    assert_eq!(hot_split(None, Some("f_80001000")), Ok(Some([0x8000_1000].into())), "unset, a list: on, it left out");
    assert_eq!(hot_split(Some("0"), None), Ok(None), "0: off");
    // (the compile's half: the hot units' flags unset, none at 0)
    let unset = build_compile::Settings::from_lookup(|_| None);
    assert!(unset.hot_split, "unset: the compile's half on");
    let zero = build_compile::Settings::from_lookup(|n| (n == "LONGSHOT_HOT_SPLIT").then(|| "0".to_string()));
    assert!(!zero.hot_split, "0: the compile's half off");
    assert!(build_compile::hotsplit::flags(unset.hot_split).iter().any(|f| f == "-hot-cold-split=true"));
    assert!(build_compile::hotsplit::flags(zero.hot_split).is_empty());
    // (a compile on its own: a lift made unset (its key file's line, no list) compiles unset and not at 0; a lift made
    // at 0 (no line) compiles at 0 and not unset; no complete lift at all compiles at 0 alone)
    let gen_dir = tmp("hot-split-default-check");
    std::fs::create_dir_all(&gen_dir).unwrap();
    let key = gen_dir.join(lift::whole::KEY_FILE);
    std::fs::write(&key, "k\nfunctions 2\nedited 0\nunits 2\nhot_split skip\n").unwrap();
    assert!(check_hot_split_as(&gen_dir, None, None).is_ok(), "made unset, compiled unset");
    assert!(check_hot_split_as(&gen_dir, Some("0"), None).is_err(), "made unset, compiled at 0");
    std::fs::write(&key, "k\nfunctions 2\nedited 0\nunits 2\n").unwrap();
    assert!(check_hot_split_as(&gen_dir, Some("0"), None).is_ok(), "made at 0, compiled at 0");
    let e = check_hot_split_as(&gen_dir, None, None).unwrap_err().to_string();
    assert!(e.contains("made with it off") && e.contains("lift again"), "made at 0, compiled unset: {e}");
    std::fs::remove_file(&key).unwrap();
    assert!(check_hot_split_as(&gen_dir, None, None).is_err(), "no lift.key, compiled unset");
    assert!(check_hot_split_as(&gen_dir, Some("0"), None).is_ok(), "no lift.key, compiled at 0");
}

/// The program that runs `f_80001000` from both sides of its branch (Z clear, then set) over a guest page mapped at the
/// window's base (llvm::Options' default) and prints a hash of the whole context and the page after each.
const HARNESS: &str = r#"
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <sys/mman.h>
#endif
uint8_t a64_dirty[1u << 27];
int calls;   /* (the guest call is in the region moved out alone: a call says that region ran) */
void f_80002000(uint8_t *c) { uint64_t x0; memcpy(&x0, c, 8); x0 = x0 * 3 + 7; memcpy(c, &x0, 8); calls++; }
void f_80001000(uint8_t *c);
int main(void) {
    const uint64_t guest = 0x20000000, size = 0x10000;
    uint8_t *want = (uint8_t *)(uintptr_t)(0x100000000000ULL + guest);
#ifdef _WIN32
    void *got = VirtualAlloc(want, size, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE);
#else
    void *got = mmap(want, size, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
#endif
    if (got != (void *)want) { printf("no window\n"); return 2; }
    for (int z = 0; z < 2; z++) {
        uint8_t c[864];
        memset(c, 0, sizeof c);
        uint64_t x0 = 5, x1 = guest + 0x100;
        memcpy(c, &x0, 8);
        memcpy(c + 8, &x1, 8);
        c[264 + 1] = (uint8_t)z;   /* (the context's flags: n, z, c, v from 264) */
        memset(want, 0, 0x200);   /* (the region walks the page by the values it loads: from zeros, by x0 a step) */
        calls = 0;
        f_80001000(c);
        uint64_t h = 1469598103934665603ULL;
        for (int k = 0; k < 864; k++) h = (h ^ c[k]) * 1099511628211ULL;
        for (int k = 0; k < 0x200; k++) h = (h ^ want[k]) * 1099511628211ULL;
        printf("z=%d calls=%d %016llx\n", z, calls, (unsigned long long)h);
    }
    return 0;
}
"#;

/// The output of the harness linked with `text` compiled for this PC (`flags`: the hot flags), or None (no clang).
fn run(clang: &str, text: &str, flags: bool, name: &str) -> String {
    let dir = tmp(&format!("hot-split-run-{name}"));
    let obj = compile(clang, text, None, flags, &dir, "o");
    let c = dir.join("main.c");
    std::fs::write(&c, HARNESS).unwrap();
    let exe = dir.join(if cfg!(windows) { "t.exe" } else { "t" });
    let r = std::process::Command::new(clang).args(["-O1"]).arg(&c).arg(&obj).arg("-o").arg(&exe).output().unwrap();
    assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
    let r = std::process::Command::new(&exe).output().unwrap();
    assert!(r.status.success(), "{name}: {}", String::from_utf8_lossy(&r.stdout));
    String::from_utf8_lossy(&r.stdout).into_owned()
}

#[test]
fn run_through_the_region_moved_out_it_gives_what_the_program_without_the_switch_gives() {
    let Some(clang) = clang() else { return };
    let (f, p) = (function(), profile());
    let off = run(&clang, &ir(&f, &p, false, &[]), false, "off");
    let on = ir(&f, &p, true, &[]);
    let asm = std::fs::read_to_string(compile(&clang, &on, None, true, &tmp("hot-split-run-asm"), "s")).unwrap();
    assert!(asm.contains("f_80001000.cold.1"), "the region moved out in this PC's build too:\n{asm}");
    let on = run(&clang, &on, true, "on");
    let lines: Vec<&str> = on.lines().collect();
    assert_eq!(lines.len(), 2, "{on}");
    // (Z clear: the side that ran, no call; Z set: the region moved out, its guest call made once)
    assert!(lines[0].starts_with("z=0 calls=0 ") && lines[1].starts_with("z=1 calls=1 "), "{on}");
    assert_eq!(on, off, "the same registers and memory from both sides");
}

/// b0 branches on Z: to b1 (never taken from here) or b2; b2 goes on to b1, which ran through it: a join.
fn join() -> Function {
    let mut f = Function::new(ENTRY);
    let (b0, b1, b2) = (f.start, f.add_block(ENTRY + 0x10), f.add_block(ENTRY + 0x40));
    let z = f.push(b0, Op::Get(Loc::Flag(Flag::Z)));
    f.terminate(
        b0,
        Term::Branch { cond: z, then: Edge::new(Dest::Block(b1), false), otherwise: Edge::new(Dest::Block(b2), false) },
    );
    let mut acc = f.push(b1, Op::Get(Loc::X(1)));
    for k in 0..6u64 {
        let off = f.push(b1, Op::Const { ty: Type::I64, value: u128::from(8 * k + 16) });
        let addr = f.push(b1, Op::Bin(BinOp::Add, acc, off));
        let v = f.push(b1, Op::Load { ty: Type::I64, addr, order: None });
        f.push(b1, Op::Store { addr, value: v, space: Space::Guest, order: None });
        acc = f.push(b1, Op::Bin(BinOp::Add, acc, v));
    }
    f.push(b1, Op::Set(Loc::X(2), acc));
    f.push(b1, Op::Call { target: 0x8000_2000 });
    f.terminate(b1, Term::Return);
    let x0 = f.push(b2, Op::Get(Loc::X(0)));
    let one = f.push(b2, Op::Const { ty: Type::I64, value: 1 });
    let inc = f.push(b2, Op::Bin(BinOp::Add, x0, one));
    f.push(b2, Op::Set(Loc::X(0), inc));
    f.terminate(b2, Term::Jump(Edge::new(Dest::Block(b1), false)));
    f.verify().expect("a well-formed function");
    f
}

#[test]
fn a_branch_never_taken_into_a_join_is_weighted_one_and_nothing_moves_out() {
    // (b0 ran 1000 times and never branched to b1; b1 ran 1000 times, through b2)
    let f = profile::Function {
        hash: 1,
        entries: 1000,
        blocks: [(ENTRY, 1000), (ENTRY + 0x10, 1000), (ENTRY + 0x40, 1000)].into(),
        ..profile::Function::default()
    };
    let p = profile::Profile { build: String::new(), functions: [(ENTRY, f)].into() };
    let text = ir(&join(), &p, true, &[]);
    assert!(text.contains("!{!\"branch_weights\", i32 1, i32 1000}"), "{text}");
    assert!(!text.contains("i32 0, i32 1000"), "{text}");
    let Some(clang) = clang() else { return };
    for target in ["x86_64-w64-mingw32", "x86_64-unknown-linux-gnu"] {
        let s = compile(&clang, &text, Some(target), true, &tmp(&format!("hot-split-join-{target}")), "s");
        let asm = std::fs::read_to_string(s).unwrap();
        assert!(!asm.contains(".cold"), "{target}: the join stays in the hot code:\n{asm}");
    }
}

#[test]
fn a_compile_on_its_own_refuses_a_lift_made_with_the_switch_otherwise() {
    use build_driver::{set_flag, stages::check_hot_split};
    let gen_dir = tmp("hot-split-compile-check");
    std::fs::create_dir_all(&gen_dir).unwrap();
    let key = gen_dir.join(lift::whole::KEY_FILE);
    // (no complete lift there (a lift that stopped or failed deletes its key file, its texts can stay): refused with the
    // switch on, as before without it)
    let _ = std::fs::remove_file(&key);
    set_flag("LONGSHOT_HOT_SPLIT", "1");
    set_flag("LONGSHOT_HOT_SPLIT_SKIP", "");
    assert!(check_hot_split(&gen_dir).is_err());
    set_flag("LONGSHOT_HOT_SPLIT", "0");
    assert!(check_hot_split(&gen_dir).is_ok());
    set_flag("LONGSHOT_HOT_SPLIT", "1");
    // (an off lift, the switch on: refused; off too: fine)
    std::fs::write(&key, "k\nfunctions 2\nedited 0\nunits 2\n").unwrap();
    let e = check_hot_split(&gen_dir).unwrap_err().to_string();
    assert!(e.contains("lift again"), "{e}");
    set_flag("LONGSHOT_HOT_SPLIT", "0");
    assert!(check_hot_split(&gen_dir).is_ok());
    // (an on lift with f_80001000 left out, the switch off: refused; on with the same list: fine; another list: refused)
    std::fs::write(&key, "k\nfunctions 2\nedited 0\nunits 2\nhot_split skip 80001000\n").unwrap();
    assert!(check_hot_split(&gen_dir).is_err());
    set_flag("LONGSHOT_HOT_SPLIT", "1");
    set_flag("LONGSHOT_HOT_SPLIT_SKIP", "f_80001000");
    assert!(check_hot_split(&gen_dir).is_ok());
    set_flag("LONGSHOT_HOT_SPLIT_SKIP", "");
    assert!(check_hot_split(&gen_dir).is_err());
    // (a bad list: refused, whatever the lift)
    set_flag("LONGSHOT_HOT_SPLIT_SKIP", "f_80001000,nope");
    assert!(check_hot_split(&gen_dir).unwrap_err().to_string().contains("nope"));
    set_flag("LONGSHOT_HOT_SPLIT", "0");
    set_flag("LONGSHOT_HOT_SPLIT_SKIP", "");
}
