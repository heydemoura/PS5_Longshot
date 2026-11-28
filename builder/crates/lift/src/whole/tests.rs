//! The whole lift on a small synthetic image (instructions assembled here, none from the game): its files, each C
//! file and unit compiled by clang where there is one, and the cache's hits and misses.

use super::*;
use build_core::report::Format;
use std::process::Command;

const BASE: u64 = 0x8000_0000;
const F0: u64 = BASE + 0x10;
const F1: u64 = BASE + 0x20;
const HOOK: u64 = BASE + 0x30;
/// The made-up image's build id.
const BUILD: &str = "00ff00ff00ff00ff00ff00ff00ff00ff00ff00ff";

/// A scratch folder of its own for a test.
fn scratch(name: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("longshot-builder-whole-{name}-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn put(mem: &mut [u8], at: u64, bytes: &[u8]) {
    let at = (at - BASE) as usize;
    mem[at..at + bytes.len()].copy_from_slice(bytes);
}

/// An image folder: one module, `main`, its MOD0 at 0x200 with an empty dynamic section; code from 0x10: f0 calls
/// f1, f1 calls the hooked address; three hooks (a host function, an NVN function, an import to stub).
fn image_dir(dir: &Path) -> PathBuf {
    let mut mem = vec![0u8; 0x1000];
    put(&mut mem, BASE + 4, &0x200u32.to_le_bytes());
    let code: [(u64, u32); 8] = [
        (F0, 0x9400_0004),       // bl f1
        (F0 + 4, 0x9100_0400),   // add x0, x0, #1
        (F0 + 8, 0xd65f_03c0),   // ret
        (F1, 0x9100_0400),       // add x0, x0, #1
        (F1 + 4, 0x9400_0003),   // bl HOOK
        (F1 + 8, 0xd65f_03c0),   // ret
        (HOOK, 0xd65f_03c0),     // (the hooked address: not lifted)
        (HOOK + 4, 0xd65f_03c0), // (another)
    ];
    for (pc, w) in code {
        put(&mut mem, pc, &w.to_le_bytes());
    }
    put(&mut mem, BASE + 0x200, b"MOD0");
    put(&mut mem, BASE + 0x204, &0x20i32.to_le_bytes()); // (the dynamic section at 0x220: a DT_NULL)
    let image = dir.join("image");
    std::fs::create_dir_all(&image).unwrap();
    std::fs::write(image.join("image.bin"), &mem).unwrap();
    let json = format!(
        r#"{{"base": {BASE}, "end": {}, "modules": [{{"name": "main", "base": {BASE}, "size": 4096,
            "build_id": "{BUILD}", "init": {}, "segments": [{{"name": ".text", "addr": {}, "size": 256}},
            {{"name": ".rodata", "addr": {}, "size": 256}}]}}],
            "hooks": [{{"addr": {HOOK}, "c": "imp_hosted", "sym": "hosted"}},
                      {{"addr": {}, "c": "imp_nvnFoo", "sym": "nvnFoo"}},
                      {{"addr": {}, "c": "imp__ZN2nn3svc3BarEv", "sym": "_ZN2nn3svc3BarEv"}}],
            "exports": {{"nnMain": {F0}}}}}"#,
        BASE + 0x1000,
        F0,
        BASE + 0x10,
        BASE + 0x200,
        HOOK + 4,
        HOOK + 8,
    );
    std::fs::write(image.join("image.json"), json).unwrap();
    let host = dir.join("host");
    std::fs::create_dir_all(&host).unwrap();
    std::fs::write(host.join("a.c"), "void imp_hosted(Ctx *c) { }\n").unwrap();
    let mut npdm = vec![0u8; 0x80];
    npdm[..4].copy_from_slice(b"META");
    npdm[0xE] = 44;
    npdm[0x1C..0x20].copy_from_slice(&0x10_0000u32.to_le_bytes());
    std::fs::write(dir.join("main.npdm"), npdm).unwrap();
    image
}

fn options(dir: &Path) -> LiftOptions {
    let mut o = LiftOptions::new(image_dir(dir), dir.join("gen"));
    o.host = Some(dir.join("host"));
    o.npdm = Some(dir.join("main.npdm"));
    o.sizing = Sizing { budget: 1, hot_budget: 1, cold_budget: 1 }; // (a unit per function)
    o.jobs = 2;
    o
}

fn quiet() -> Reporter {
    Reporter::to(Format::Text, Box::new(std::io::sink()))
}

/// clang (or `CC`); a test fails without one, unless `LONGSHOT_NO_CLANG=1` says to pass over what needs it.
fn clang() -> Option<String> {
    let cc = std::env::var("CC").unwrap_or_else(|_| "clang".into());
    let found = Command::new(&cc).arg("--version").output().ok().filter(|o| o.status.success()).map(|_| cc);
    if found.is_none() && std::env::var("LONGSHOT_NO_CLANG").as_deref() != Ok("1") {
        panic!("no clang to compile the lift's files with (LONGSHOT_NO_CLANG=1 passes over these checks)");
    }
    found
}

fn compiles(cc: &str, file: &Path, include: &Path) {
    let out = file.with_extension("o");
    let r = Command::new(cc)
        .args(["-c", "-O1", "-Wall", "-Werror", "-Wno-unused-command-line-argument", "-Wno-override-module", "-mcx16"])
        .arg("-I")
        .arg(include)
        .arg(file)
        .arg("-o")
        .arg(&out)
        .output()
        .unwrap();
    assert!(r.status.success(), "{}: {}", file.display(), String::from_utf8_lossy(&r.stderr));
}

/// The site tables of one site, `t.one`, of form `at` in f1 (found by its add and its bl), in `dir/tables-<at>`.
fn site_tables(dir: &Path, at: &str) -> PathBuf {
    let t = dir.join(format!("tables-{at}"));
    let locs = t.join("sites").join(BUILD);
    std::fs::create_dir_all(&locs).unwrap();
    let def = "format = 2\n\n[[site]]\nname = \"t.one\"\ndoc = \"f1 is called\"\nat = \"AT\"\nshape = \"event\"\n\
               thread = \"serial\"\n";
    std::fs::write(t.join("sites").join("sites.toml"), def.replace("AT", at)).unwrap();
    let loc = format!(
        "format = 2\nbuild = \"{BUILD}\"\n\n[[site]]\nname = \"t.one\"\nfunction = \"f_{F1:08x}\"\nsignature = \"91000400 94000003\"\n"
    );
    std::fs::write(locs.join("locations.toml"), loc).unwrap();
    std::fs::write(t.join("sites.def"), "HOS_SITE(t_one, \"t.one\")\n").unwrap();
    t
}

#[test]
fn writes_every_file_and_they_compile() {
    let dir = scratch("files");
    let o = options(&dir);
    let out = run(&o, &quiet()).unwrap();
    assert_eq!(out.how, How::Lifted);
    assert_eq!(out.functions, 2);
    assert_eq!(out.edited, 0);
    assert_eq!(out.units.len(), 2);
    let gen_dir = &o.gen_dir;
    for f in ["table.c", "imports.c", "image.c", "functions.txt", UNITS_FILE, KEY_FILE, "rt/a64.h", "rt/a64_rt.c"] {
        assert!(gen_dir.join(f).is_file(), "{f}");
    }
    let table = std::fs::read_to_string(gen_dir.join("table.c")).unwrap();
    assert!(table.contains("const uint32_t a64_fn_count = 5;"), "{table}");
    assert!(table.contains(&format!("void f_{HOOK:08x}(Ctx *A64_NOESC c) {{ TAILCALL(imp_hosted); }}")));
    let imports = std::fs::read_to_string(gen_dir.join("imports.c")).unwrap();
    assert!(imports.contains("nvn2_dispatch(c, \"nvnFoo\", k)") && imports.contains("\"_ZN2nn3svc3BarEv\", &seen"));
    assert!(!imports.contains("imp_hosted"));
    let image_c = std::fs::read_to_string(gen_dir.join("image.c")).unwrap();
    assert!(image_c.contains(&format!("hos_module_init[] = {{{F0:#x}ULL}}")) && image_c.contains("priority = 44"));
    let functions = std::fs::read_to_string(gen_dir.join("functions.txt")).unwrap();
    assert_eq!(functions, format!("{F0:08x} 3 lifted_{F0:08x}.ll\n{F1:08x} 3 lifted_{F1:08x}.ll\n"));
    if let Some(cc) = clang() {
        let rt = gen_dir.join("rt");
        for f in ["table.c", "imports.c", "image.c", "rt/a64_rt.c", "rt/a64_fp.c"] {
            compiles(&cc, &gen_dir.join(f), &rt);
        }
        for u in &out.units {
            compiles(&cc, &gen_dir.join(format!("{}.ll", u.name)), &rt);
        }
    }
}

#[test]
fn an_unchanged_lift_is_skipped_and_a_change_rewrites_its_units() {
    let dir = scratch("cache");
    let mut o = options(&dir);
    assert_eq!(run(&o, &quiet()).unwrap().how, How::Lifted);
    let again = run(&o, &quiet()).unwrap();
    assert_eq!(again.how, How::Unchanged);
    assert_eq!(again.units.len(), 2);

    // (a site in f1: its unit alone is lifted again, with the site's hook)
    o.tables = Some(site_tables(&dir, "entry"));
    let edited = run(&o, &quiet()).unwrap();
    assert_eq!((edited.how, edited.units_lifted, edited.edited), (How::Lifted, 1, 1));
    let unit = std::fs::read_to_string(o.gen_dir.join(format!("lifted_{F1:08x}.ll"))).unwrap();
    assert!(unit.contains("@hos_site_on_t_one"), "{unit}");
    let listed = std::fs::read_to_string(o.gen_dir.join(EDITS_FILE)).unwrap();
    assert!(listed.starts_with(&format!("{F1:08x} ")) && listed.trim_end().ends_with("site: t.one"), "{listed}");
    // (the site's form changed in its table: again f1's unit alone)
    o.tables = Some(site_tables(&dir, "return"));
    let moved = run(&o, &quiet()).unwrap();
    assert_eq!((moved.units_lifted, moved.units_written), (1, 1));
    let unit = std::fs::read_to_string(o.gen_dir.join(format!("lifted_{F1:08x}.ll"))).unwrap();
    assert!(unit.contains(&format!("define void @f_1{F1:08x}(")), "the body under its wrapper: {unit}");
    // (the host's list changed, its sites' tables not: the lift writes its HOS_VALUE symbols and checks its lines, so
    // the key holds it and the build lifts again)
    let t = o.tables.clone().unwrap();
    assert_eq!(run(&o, &quiet()).unwrap().how, How::Unchanged);
    std::fs::write(t.join("sites.def"), "HOS_SITE(t_one, \"t.one\")\nHOS_SITE(t_two, \"t.two\")\n").unwrap();
    assert_eq!(run(&o, &quiet()).unwrap().how, How::Lifted, "game/sites.def changed, the same key");

    // (a unit file damaged or removed is written again)
    o.tables = None;
    run(&o, &quiet()).unwrap();
    std::fs::remove_file(o.gen_dir.join(format!("lifted_{F0:08x}.ll"))).unwrap();
    let repaired = run(&o, &quiet()).unwrap();
    assert_eq!((repaired.how, repaired.units_lifted), (How::Lifted, 1));

    // (a unit lifted again to the same text: its file is left as it was, its time too)
    let unit = o.gen_dir.join(format!("lifted_{F1:08x}.ll"));
    let before = std::fs::metadata(&unit).unwrap().modified().unwrap();
    let list = std::fs::read_to_string(o.gen_dir.join(UNITS_FILE)).unwrap();
    let key = units::parse_list(&list)[&format!("lifted_{F1:08x}")].key.clone();
    std::fs::write(o.gen_dir.join(UNITS_FILE), list.replace(&key, "stale")).unwrap();
    o.force = true;
    let same = run(&o, &quiet()).unwrap();
    o.force = false;
    assert_eq!((same.units_lifted, same.units_written), (1, 0));
    assert_eq!(std::fs::metadata(&unit).unwrap().modified().unwrap(), before);

    // (an instruction of f1 changed, as an ExeFS patch does: f1's unit alone)
    let bin = o.image.join("image.bin");
    let mut mem = std::fs::read(&bin).unwrap();
    put(&mut mem, F1, &0x9100_0800u32.to_le_bytes()); // add x0, x0, #2
    std::fs::write(&bin, mem).unwrap();
    let patched = run(&o, &quiet()).unwrap();
    assert_eq!((patched.units_lifted, patched.units_written), (1, 1));

    // (another codegen option: every unit)
    o.codegen.polls = false;
    assert_eq!(run(&o, &quiet()).unwrap().units_lifted, 2);
}

#[test]
fn a_plain_lift_after_an_instrumented_one_lifts_every_unit_again() {
    let dir = scratch("instrumented");
    let mut o = options(&dir);
    o.profiling = Profiling::Generate;
    let generated = run(&o, &quiet()).unwrap();
    let unit = |u: &Listed| std::fs::read_to_string(o.gen_dir.join(format!("{}.ll", u.name))).unwrap();
    assert!(generated.units.iter().all(|u| unit(u).contains("@lsp.counts")), "every unit instrumented");
    // (the same gen/, the profile off: no unit, nor its content key and so its object, is kept from the instrumented
    // lift)
    o.profiling = Profiling::Off;
    let plain = run(&o, &quiet()).unwrap();
    assert_eq!((plain.how, plain.units_lifted), (How::Lifted, 2));
    for u in &plain.units {
        assert!(!unit(u).contains("@lsp."), "{} still instrumented", u.name);
        let before = generated.units.iter().find(|g| g.name == u.name).unwrap();
        assert_ne!(u.key, before.key, "{}: the unit key of the instrumented lift", u.name);
    }
    // (and back: an instrumented lift writes them all again)
    o.profiling = Profiling::Generate;
    let again = run(&o, &quiet()).unwrap();
    assert!(again.units.iter().all(|u| unit(u).contains("@lsp.counts")));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn without_the_texts_kept_a_unit_s_text_is_made_when_asked_the_same_as_its_file() {
    let dir = scratch("texts");
    let kept = options(&dir);
    let with_files = run(&kept, &quiet()).unwrap();
    let mut o = kept.clone();
    o.gen_dir = dir.join("gen-texts");
    o.keep_text = false;
    let (out, session) = run_with(&o, &quiet()).unwrap();
    assert_eq!(out.how, How::Lifted);
    assert_eq!(out.key, with_files.key, "the same lift");
    assert!(out.units.iter().all(Listed::is_pending), "listed by their keys, no texts made");
    let ll = |d: &Path| -> usize {
        std::fs::read_dir(d).unwrap().flatten().filter(|e| e.file_name().to_string_lossy().ends_with(".ll")).count()
    };
    assert_eq!(ll(&o.gen_dir), 0, "no unit files");
    // (made from the lift's session, and from one worked out again: each the text the kept lift wrote)
    for texts in [Texts::new(o.clone(), session), Texts::new(o.clone(), None)] {
        for u in &with_files.units {
            let file = std::fs::read_to_string(kept.gen_dir.join(format!("{}.ll", u.name))).unwrap();
            assert_eq!(texts.text(&format!("{}.ll", u.name)).unwrap(), file);
        }
        texts.finish().unwrap();
    }
    assert!(texts_not_of_this_lift(&o));
    // (the list now holds each unit's content key, as the kept lift's does; the next lift is unchanged, makes nothing)
    let list = units::parse_list(&std::fs::read_to_string(o.gen_dir.join(UNITS_FILE)).unwrap());
    let kept_list = units::parse_list(&std::fs::read_to_string(kept.gen_dir.join(UNITS_FILE)).unwrap());
    assert_eq!(list, kept_list);
    let again = run(&o, &quiet()).unwrap();
    assert_eq!(again.how, How::Unchanged);
    // (a lift with the texts kept again writes them)
    o.keep_text = true;
    let back = run(&o, &quiet()).unwrap();
    assert_eq!((back.how, back.units_lifted), (How::Lifted, 2));
    assert_eq!(ll(&o.gen_dir), 2);
}

/// Whether asking for a text of a lift gen/ no longer holds is refused.
fn texts_not_of_this_lift(o: &LiftOptions) -> bool {
    let mut other = o.clone();
    other.codegen.polls = !other.codegen.polls;
    Texts::new(other, None).text(&format!("lifted_{F0:08x}.ll")).is_err()
}

#[test]
fn the_store_gives_a_lift_to_another_folder() {
    let dir = scratch("store");
    let mut o = options(&dir);
    o.store = Some(dir.join("store"));
    let first = run(&o, &quiet()).unwrap();
    o.gen_dir = dir.join("gen2");
    let second = run(&o, &quiet()).unwrap();
    assert_eq!(second.how, How::Restored);
    assert_eq!(second.key, first.key);
    for u in &first.units {
        let a = std::fs::read(dir.join("gen").join(format!("{}.ll", u.name))).unwrap();
        assert_eq!(a, std::fs::read(o.gen_dir.join(format!("{}.ll", u.name))).unwrap());
    }
    assert_eq!(run(&o, &quiet()).unwrap().how, How::Unchanged);
}

#[test]
fn a_site_table_with_no_sites_lifts_the_same_files() {
    // (parity gate 1's game-free half, docs/dev/reference/parity-gates.md: the files written with a site table present
    // and empty are byte for byte the files written with none, the key's file aside, and no function is edited)
    let dir = scratch("gate1");
    let bare = options(&dir.join("bare"));
    run(&bare, &quiet()).unwrap();
    let mut empty = options(&dir.join("empty"));
    let t = dir.join("tables");
    std::fs::create_dir_all(t.join("sites").join(BUILD)).unwrap();
    std::fs::write(t.join("sites").join("sites.toml"), "format = 2\n").unwrap();
    std::fs::write(t.join("sites").join(BUILD).join("locations.toml"), format!("format = 2\nbuild = \"{BUILD}\"\n"))
        .unwrap();
    std::fs::write(t.join("sites.def"), "").unwrap();
    empty.tables = Some(t);
    let out = run(&empty, &quiet()).unwrap();
    assert_eq!(out.edited, 0);
    let files = |dir: &Path| -> BTreeMap<String, Vec<u8>> {
        let mut all = BTreeMap::new();
        let mut todo = vec![dir.to_path_buf()];
        while let Some(d) = todo.pop() {
            for e in std::fs::read_dir(&d).unwrap() {
                let p = e.unwrap().path();
                if p.is_dir() {
                    todo.push(p);
                } else {
                    let rel = p.strip_prefix(dir).unwrap().to_string_lossy().replace('\\', "/");
                    if rel != KEY_FILE {
                        all.insert(rel, std::fs::read(&p).unwrap());
                    }
                }
            }
        }
        all
    };
    let (a, b) = (files(&bare.gen_dir), files(&empty.gen_dir));
    assert_eq!(a.keys().collect::<Vec<_>>(), b.keys().collect::<Vec<_>>(), "other files");
    for (name, bytes) in &a {
        assert!(&b[name] == bytes, "{name} differs with an empty site table");
    }
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_setjmp_owner_with_more_calls_than_a_slot_holds_stops_the_lift() {
    let dir = scratch("setjmp5");
    let o = options(&dir);
    let image = image::Image::load(&o.image).unwrap();
    let mut plan = discover(&o, &image).unwrap();
    // (four: each a return address of the slot)
    let four: Vec<u64> = (0..4).map(|i| F1 + 4 * i).collect();
    plan.setjmp_owners.insert(F1, four.clone());
    check_setjmp_owners(&plan).unwrap();
    let f = Program::new(&image, &plan, &::edits::Edits::new()).lift(F0).unwrap().body;
    assert!(
        f.to_string().contains(&format!("with setjmp ({:x}, {:x}, {:x}, {:x})", four[0], four[1], four[2], four[3]))
    );
    // (a synthetic owner of five: refused, by its name, not cut to four)
    plan.setjmp_owners.insert(F1, (0..5).map(|i| F1 + 4 * i).collect());
    let e = check_setjmp_owners(&plan).unwrap_err().to_string();
    assert!(e.contains(&format!("f_{F1:08x}")) && e.contains("5 setjmp calls"), "{e}");
    let lifted = Program::new(&image, &plan, &::edits::Edits::new()).lift(F0);
    assert_eq!(lifted.err(), Some(Skip::SetjmpReturns { owner: F1, count: 5 }));
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_call_of_a_setjmp_owner_takes_a_host_setjmp() {
    let dir = scratch("setjmp");
    let o = options(&dir);
    let image = image::Image::load(&o.image).unwrap();
    let mut plan = discover(&o, &image).unwrap();
    plan.setjmp_owners.insert(F1, vec![F1 + 8]);
    let f = Program::new(&image, &plan, &::edits::Edits::new()).lift(F0).unwrap().body;
    assert!(f.to_string().contains(&format!("with setjmp ({:x}, 0, 0, 0)", F1 + 8)), "{f}");
    let mut m = llvm::Module::new(llvm::Options::default());
    m.add(&f, llvm::Runs::default());
    let text = m.finish();
    assert!(text.contains("call i32 @recomp_sj_setjmp(ptr") && text.contains("returns_twice"), "{text}");
    // (the host keeps the context's address, to find its slots by: not `nocapture`)
    assert!(text.contains("declare ptr @recomp_sj_push(ptr, i64, i64, i64, i64)"), "{text}");
    if let Some(cc) = clang() {
        let path = dir.join("setjmp.ll");
        std::fs::write(&path, text).unwrap();
        compiles(&cc, &path, &dir);
    }
}

#[test]
fn a_host_store_on_the_gpu_bitmap_marks_when_its_last_page_is_the_gpu_s() {
    // (A64_MARK as the lifted code marks: a store from a page the GPU does not map into one it does marks both)
    let Some(cc) = clang() else { return };
    let d = scratch("mark-bitmap");
    let rt = d.join("rt");
    std::fs::create_dir_all(&rt).unwrap();
    let o = llvm::Options { mark_bitmap: true, dirty_pages: 4096, ..llvm::Options::default() };
    std::fs::write(rt.join("a64_config.h"), files::config_h(&o)).unwrap();
    std::fs::copy(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../rt/a64.h"), rt.join("a64.h")).unwrap();
    let c = d.join("m.c");
    std::fs::write(
        &c,
        "#include \"a64.h\"\n#include <stdint.h>\nuint8_t a64_dirty[A64_DIRTY_PAGES];\n\
         uint8_t a64_gpu_pages[A64_DIRTY_PAGES / 8];\n\
         int main(void) {\n    a64_gpu_pages[0] = 2; /* page 1 the GPU's, page 0 not */\n\
             A64_MARK(0x0f00, 0x200); /* from page 0 into page 1 */\n\
             A64_MARK(0x2f00, 0x200); /* pages 2 and 3, neither the GPU's */\n\
             return !(a64_dirty[0] && a64_dirty[1] && !a64_dirty[2] && !a64_dirty[3]);\n}\n",
    )
    .unwrap();
    let exe = d.join("m");
    let r = Command::new(&cc).args(["-O1", "-mcx16", "-I"]).arg(&rt).arg(&c).arg("-o").arg(&exe).output().unwrap();
    assert!(r.status.success(), "{}", String::from_utf8_lossy(&r.stderr));
    assert!(Command::new(&exe).status().unwrap().success(), "both pages marked, and only those");
    let _ = std::fs::remove_dir_all(&d);
}

/// A lift of `o`, and what its log said (text).
fn logged(o: &LiftOptions) -> (LiftOutcome, String) {
    #[derive(Clone)]
    struct Buf(Arc<Mutex<Vec<u8>>>);
    impl std::io::Write for Buf {
        fn write(&mut self, b: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(b);
            Ok(b.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let buf = Buf(Arc::new(Mutex::new(Vec::new())));
    let out = run(o, &Reporter::to(Format::Text, Box::new(buf.clone()))).unwrap();
    let text = String::from_utf8_lossy(&buf.0.lock().unwrap()).into_owned();
    (out, text)
}

/// [`image_dir`] with a float add in each function (f0: `bl f1; fadd s0, s0, s1; ret`, f1: `fadd s2, s2, s3; bl HOOK;
/// ret`), and a guest profile in which f0 ran and f1 did not: f0 in a hot unit, f1 in a cold one.
fn float_image(dir: &Path) -> (PathBuf, PathBuf) {
    let image = image_dir(dir);
    let bin = image.join("image.bin");
    let mut mem = std::fs::read(&bin).unwrap();
    let (fadd0, fadd1) = (0x1e21_2800u32, 0x1e23_2842u32);
    put(&mut mem, F0 + 4, &fadd0.to_le_bytes());
    put(&mut mem, F1, &fadd1.to_le_bytes());
    std::fs::write(&bin, &mem).unwrap();
    let hash = profile::code_hash([0x9400_0004, fadd0, 0xd65f_03c0]);
    let p = dir.join("guest.profile");
    std::fs::write(&p, format!("longshot-guest-profile 1 build {BUILD}\nf {F0:x} {hash:016x} 1000\nb {F0:x} 1000\n"))
        .unwrap();
    (image, p)
}

#[test]
fn the_float_middle_path_is_on_by_default_in_the_hot_units_and_their_keys_only() {
    let dir = scratch("fp-nan-mid");
    let mut o = options(&dir);
    let (image, profile) = float_image(&dir);
    o.image = image;
    o.profiling = Profiling::Use { path: profile, share: 0.95, max: 10 };
    assert_eq!(o.fp_nan_mid, Some(BTreeSet::new()), "on by default, no function left out");
    // (the default lift: the hot unit's float slow path has its middle path, the cold one's not)
    let default = run(&o, &quiet()).unwrap();
    let hot_text = std::fs::read_to_string(o.gen_dir.join(format!("lifted_hot_{F0:08x}.ll"))).unwrap();
    let cold_text = std::fs::read_to_string(o.gen_dir.join(format!("lifted_cold_{F1:08x}.ll"))).unwrap();
    assert!(hot_text.contains("call float @lifter.fp.mid.add.f32.f32.i64(float "), "{hot_text}");
    assert!(!cold_text.contains("lifter.fp.mid"), "{cold_text}");
    let _ = std::fs::remove_dir_all(&o.gen_dir);
    // (the option is never in the options every unit's key holds)
    let options_text = codegen_text(&o);
    o.fp_nan_mid = Some(BTreeSet::from([F0]));
    assert_eq!(codegen_text(&o), options_text);
    o.fp_nan_mid = None;
    assert_eq!(codegen_text(&o), options_text);
    let text = |o: &LiftOptions, u: &Listed| std::fs::read_to_string(o.gen_dir.join(format!("{}.ll", u.name))).unwrap();
    let texts = |o: &LiftOptions, units: &[Listed]| -> BTreeMap<String, String> {
        units.iter().map(|u| (u.name.clone(), text(o, u))).collect()
    };
    // (by name: the outcome lists the units in the order the workers finished them)
    let by_name =
        |units: &[Listed]| -> BTreeMap<String, Listed> { units.iter().map(|u| (u.name.clone(), u.clone())).collect() };
    let (hot, cold) = (format!("lifted_hot_{F0:08x}"), format!("lifted_cold_{F1:08x}"));
    let off = run(&o, &quiet()).unwrap();
    let off_texts = texts(&o, &off.units);
    assert_eq!(off_texts.keys().cloned().collect::<Vec<_>>(), [cold.clone(), hot.clone()], "{off_texts:?}");
    assert!(off_texts.values().all(|t| t.contains("@lifter.fp.add.f32.f32.i64(") && !t.contains("lifter.fp.mid")));
    // (on: the lift's key moves and the hot unit's, so the hot unit alone is lifted again; the cold unit keeps its key
    // and its text. The hot unit's float slow path has its middle path, inline)
    o.fp_nan_mid = Some(BTreeSet::new());
    let (on, log) = logged(&o);
    assert!(log.contains("LONGSHOT_FP_NAN_MID, in the 1 units made now (not those kept from before): 1 of the 1 float fast paths of the functions given it"), "{log}");
    assert_eq!((on.how, on.units_lifted), (How::Lifted, 1));
    assert_ne!(on.key, off.key);
    // (the default's lift is this one: the same whole key and units)
    assert_eq!(on.key, default.key, "the default is on");
    assert_eq!(by_name(&on.units), by_name(&default.units));
    let (off_units, on_units) = (by_name(&off.units), by_name(&on.units));
    assert_ne!(on_units[&hot].key, off_units[&hot].key);
    assert_eq!(on_units[&cold].key, off_units[&cold].key, "the cold unit's key as without the option");
    let on_texts = texts(&o, &on.units);
    let h = &on_texts[&hot];
    assert!(h.contains("call i1 @lifter.fp.mid.subnormal.f32.f32.f32(float "), "{h}");
    assert!(h.contains("call float @lifter.fp.mid.add.f32.f32.i64(float "), "{h}");
    assert!(h.contains("call float @lifter.fp.add.f32.f32.i64(float "), "{h}");
    assert!(h.contains("attributes #5 = { nounwind willreturn alwaysinline memory(none) }"), "{h}");
    assert_eq!(on_texts[&cold], off_texts[&cold], "the cold unit's text as without the option");
    if let Some(cc) = clang() {
        compiles(&cc, &o.gen_dir.join(format!("{hot}.ll")), &o.gen_dir.join("rt"));
    }
    // (a cold function on the list: the lift's key moves, no unit's: nothing lifted again)
    o.fp_nan_mid = Some(BTreeSet::from([F1]));
    let skipped = run(&o, &quiet()).unwrap();
    assert_ne!(skipped.key, on.key);
    assert_eq!(by_name(&skipped.units), on_units, "a skip entry moves its own unit's key alone");
    assert_eq!((skipped.how, skipped.units_lifted), (How::Lifted, 0));
    // (the hot function on the list: its unit's key moves back to the one without the option, as every function of it
    // is left out; the cold one's not; the units are the lift's without the option, text for text: the option touches
    // nothing else. The line counts the entries as entries)
    o.fp_nan_mid = Some(BTreeSet::from([F0, F1]));
    let (none, log) = logged(&o);
    let none_units = by_name(&none.units);
    assert_ne!(none_units[&hot].key, on_units[&hot].key);
    assert_eq!(none_units[&hot].key, off_units[&hot].key, "every function left out: the key without the option");
    assert_eq!(none_units[&cold].key, off_units[&cold].key);
    assert_eq!(none.units_lifted, 1);
    assert_eq!(texts(&o, &none.units), off_texts);
    assert!(
        log.contains("1 units made now (not those kept from before): 0 of the 0 float fast paths")
            && log.contains("the 2 entries on the skip list"),
        "none given it: {log}"
    );
    // (a lift on, in a fresh gen/: the cold unit's text made anew is the one without the option, the hot one's not)
    let mut fresh = o.clone();
    fresh.gen_dir = dir.join("gen-fresh");
    fresh.fp_nan_mid = Some(BTreeSet::new());
    let made = run(&fresh, &quiet()).unwrap();
    assert_eq!(made.units_lifted, 2);
    let made_texts = texts(&fresh, &made.units);
    assert_eq!(made_texts[&cold], off_texts[&cold]);
    assert!(made_texts[&hot].contains("lifter.fp.mid."), "{}", made_texts[&hot]);
    // (off again after on: the first lift's key and units, the hot one lifted again, the cold one kept)
    fresh.fp_nan_mid = None;
    let back = run(&fresh, &quiet()).unwrap();
    assert_eq!(back.key, off.key);
    assert_eq!(by_name(&back.units), off_units);
    assert_eq!(back.units_lifted, 1);
    // (and off after every function left out: the lift's key moves back, no unit's (they were the off ones already))
    o.fp_nan_mid = None;
    let back = run(&o, &quiet()).unwrap();
    assert_eq!(back.key, off.key);
    assert_eq!(by_name(&back.units), off_units);
    assert_eq!(back.units_lifted, 0);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_hot_unit_s_key_holds_which_of_its_functions_the_middle_path_leaves_out() {
    // (one hot unit of two functions, each with a float add: f0 and f1 both in the profile, every function hot, the hot
    // units' budget big enough for both)
    let dir = scratch("fp-nan-mid-two");
    let mut o = options(&dir);
    let (image, _) = float_image(&dir);
    o.image = image;
    let (fadd0, fadd1) = (0x1e21_2800u32, 0x1e23_2842u32);
    let (h0, h1) =
        (profile::code_hash([0x9400_0004, fadd0, 0xd65f_03c0]), profile::code_hash([fadd1, 0x9400_0003, 0xd65f_03c0]));
    let p = dir.join("two.profile");
    std::fs::write(
        &p,
        format!(
            "longshot-guest-profile 1 build {BUILD}\nf {F0:x} {h0:016x} 1000\nb {F0:x} 1000\nf {F1:x} {h1:016x} 1000\nb {F1:x} 1000\n"
        ),
    )
    .unwrap();
    o.profiling = Profiling::Use { path: p, share: 1.0, max: 10 };
    o.sizing.hot_budget = 1 << 40;
    let hot = format!("lifted_hot_{F0:08x}");
    let unit = |o: &LiftOptions, out: &LiftOutcome| -> (Listed, String) {
        let u = out.units.iter().find(|u| u.name == hot).unwrap_or_else(|| panic!("{hot} in {:?}", out.units)).clone();
        let text = std::fs::read_to_string(o.gen_dir.join(format!("{hot}.ll"))).unwrap();
        (u, text)
    };
    let mids = |text: &str| text.matches("call float @lifter.fp.mid.add.f32.f32.i64(").count();
    o.fp_nan_mid = None;
    let off = run(&o, &quiet()).unwrap();
    let (off_unit, off_text) = unit(&o, &off);
    assert_eq!(off.units.len(), 1, "{:?}", off.units);
    assert_eq!(mids(&off_text), 0);
    // (on: both functions given the middle path)
    o.fp_nan_mid = Some(BTreeSet::new());
    let on = run(&o, &quiet()).unwrap();
    let (on_unit, on_text) = unit(&o, &on);
    assert_ne!(on_unit.key, off_unit.key);
    assert_eq!(mids(&on_text), 2, "{on_text}");
    // (one of them left out: the unit's key moves again, the unit is lifted again, one middle path)
    o.fp_nan_mid = Some(BTreeSet::from([F1]));
    let one = run(&o, &quiet()).unwrap();
    let (one_unit, one_text) = unit(&o, &one);
    assert_ne!(one_unit.key, on_unit.key, "the key holds which of the unit's functions are left out");
    assert_ne!(one_unit.key, off_unit.key);
    assert_eq!(one.units_lifted, 1);
    assert_eq!(mids(&one_text), 1, "{one_text}");
    // (both left out: the unit's key and text without the option; the lift's key still not the one without it)
    o.fp_nan_mid = Some(BTreeSet::from([F0, F1]));
    let both = run(&o, &quiet()).unwrap();
    let (both_unit, both_text) = unit(&o, &both);
    assert_eq!(both_unit.key, off_unit.key);
    assert_eq!(both_text, off_text);
    assert_ne!(both.key, off.key);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn the_hot_split_is_in_the_key_only_when_on() {
    let dir = scratch("hot-split");
    let mut o = options(&dir);
    // (the options' own value: off. The build's switch is on by default and sets it,
    // build_driver::stages::lift_options; longshot-builder's tests/hot_split.rs checks that default)
    assert_eq!(o.hot_split, None, "LiftOptions::new: off until the build sets it");
    // (off, the options' text is a lift's without the option, so no unit's key moves)
    assert_eq!(
        codegen_text(&o),
        format!("{:?} mark_each={} no_simplify={} split={:?}", o.codegen, o.mark_each, o.no_simplify, o.split)
    );
    // (a profile of the made-up build: f0 ran most and is hot, f1 ran once; their words as image_dir has them)
    let hash = profile::code_hash([0x9400_0004, 0x9100_0400, 0xd65f_03c0]);
    let f1_hash = profile::code_hash([0x9100_0400, 0x9400_0003, 0xd65f_03c0]);
    let profile = dir.join("guest.profile");
    let text = format!(
        "{} 1 build {BUILD}\nf {F0:x} {hash:016x} 1000\nb {F0:x} 1000\nf {F1:x} {f1_hash:016x} 1\nb {F1:x} 1\n",
        profile::MAGIC
    );
    std::fs::write(&profile, text).unwrap();
    o.profiling = Profiling::Use { path: profile, share: 0.5, max: 4000 };
    let text = |o: &LiftOptions, name: &str| std::fs::read_to_string(o.gen_dir.join(format!("{name}.ll"))).unwrap();
    let by_name =
        |units: &[Listed]| -> BTreeMap<String, Listed> { units.iter().map(|u| (u.name.clone(), u.clone())).collect() };
    let hot = format!("lifted_hot_{F0:08x}");
    let off = run(&o, &quiet()).unwrap();
    let off_units = by_name(&off.units);
    assert!(off_units.contains_key(&hot), "f0 in a hot unit: {:?}", off_units.keys());
    let off_texts: BTreeMap<String, String> = off_units.keys().map(|n| (n.clone(), text(&o, n))).collect();
    let key_file = std::fs::read_to_string(o.gen_dir.join(KEY_FILE)).unwrap();
    assert_eq!(hot_split_of(&o.gen_dir), Some(None), "off: no line in the key file: {key_file}");
    assert_eq!(hot_split_of(&dir.join("nowhere")), None);
    // (on: the lift's key moves, and among the units' keys the hot unit's alone: the others' texts are the same either
    // way. These functions have no branch, so the hot unit's text is the same too: the switch adds weights to branches
    // never taken alone, llvm's hotsplit_tests)
    o.hot_split = Some(BTreeSet::new());
    let on = run(&o, &quiet()).unwrap();
    assert_ne!(on.key, off.key);
    assert_eq!(on.units_lifted, 1, "the hot unit alone lifted again");
    for u in &on.units {
        if u.name == hot {
            assert_ne!(u.key, off_units[&u.name].key, "{}", u.name);
        } else {
            assert_eq!(u.key, off_units[&u.name].key, "{}: not a hot unit, its key as off", u.name);
        }
        assert_eq!(text(&o, &u.name), off_texts[&u.name], "{}", u.name);
    }
    assert_eq!(hot_split_of(&o.gen_dir), Some(Some(BTreeSet::new())));
    // (f0 left out: another key for the lift and for f0's hot unit; f1, no hot function, left out: the units' keys as
    // on, the lift's not: it holds the whole list)
    let on_units = by_name(&on.units);
    o.hot_split = Some(BTreeSet::from([F1]));
    let f1_left = run(&o, &quiet()).unwrap();
    assert_ne!(f1_left.key, on.key);
    for u in &f1_left.units {
        assert_eq!(u.key, on_units[&u.name].key, "{}", u.name);
    }
    o.hot_split = Some(BTreeSet::from([F0]));
    let left = run(&o, &quiet()).unwrap();
    assert_ne!(left.key, on.key);
    assert_ne!(by_name(&left.units)[&hot].key, on_units[&hot].key);
    // (and not the off key either: a left-out unit's text is not off's, the guard's weights are in it)
    assert_ne!(by_name(&left.units)[&hot].key, off_units[&hot].key);
    assert_eq!(hot_split_of(&o.gen_dir), Some(Some(BTreeSet::from([F0]))));
    // (and off again: the first lift's key and units)
    o.hot_split = None;
    let back = run(&o, &quiet()).unwrap();
    assert_eq!(back.key, off.key);
    assert_eq!(by_name(&back.units), off_units);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn the_middle_path_and_the_hot_split_are_each_in_the_hot_units_keys_on_their_own() {
    // (f0 hot, f1 cold, a float add in each: float_image)
    let dir = scratch("fp-nan-mid-hot-split");
    let mut o = options(&dir);
    let (image, profile) = float_image(&dir);
    o.image = image;
    o.profiling = Profiling::Use { path: profile, share: 0.95, max: 10 };
    let by_name =
        |units: &[Listed]| -> BTreeMap<String, Listed> { units.iter().map(|u| (u.name.clone(), u.clone())).collect() };
    let (hot, cold) = (format!("lifted_hot_{F0:08x}"), format!("lifted_cold_{F1:08x}"));
    let options_text = codegen_text(&o);
    // (the four settings: (middle path, hot split) -> the lift's key and its units)
    let mut lifts: BTreeMap<(bool, bool), (String, BTreeMap<String, Listed>)> = BTreeMap::new();
    for mid in [false, true] {
        for split in [false, true] {
            o.fp_nan_mid = mid.then(BTreeSet::new);
            o.hot_split = split.then(BTreeSet::new);
            assert_eq!(
                codegen_text(&o),
                options_text,
                "({mid}, {split}): neither in the options every unit's key holds"
            );
            let out = run(&o, &quiet()).unwrap();
            assert_eq!(hot_split_of(&o.gen_dir), Some(o.hot_split.clone()), "({mid}, {split}): the key file's line");
            lifts.insert((mid, split), (out.key.clone(), by_name(&out.units)));
        }
    }
    let whole: BTreeSet<&String> = lifts.values().map(|(k, _)| k).collect();
    assert_eq!(whole.len(), 4, "the lift's key holds both: {lifts:?}");
    let hot_keys: BTreeSet<&String> = lifts.values().map(|(_, u)| &u[&hot].key).collect();
    assert_eq!(hot_keys.len(), 4, "the hot unit's key holds each on its own: {lifts:?}");
    let cold_keys: BTreeSet<&String> = lifts.values().map(|(_, u)| &u[&cold].key).collect();
    assert_eq!(cold_keys.len(), 1, "the cold unit's key holds neither: {lifts:?}");
    // (the middle path's list leaving out the hot unit's one function: its key as with the middle path off and the hot
    // split on; the lift's key not, it holds the whole list)
    o.fp_nan_mid = Some(BTreeSet::from([F0]));
    o.hot_split = Some(BTreeSet::new());
    let out = run(&o, &quiet()).unwrap();
    assert_eq!(by_name(&out.units)[&hot].key, lifts[&(false, true)].1[&hot].key);
    assert!(lifts.values().all(|(k, _)| *k != out.key));
    // (the hot split's list leaving it out: a key of its own, the middle path's part as on)
    o.fp_nan_mid = Some(BTreeSet::new());
    o.hot_split = Some(BTreeSet::from([F0]));
    let out = run(&o, &quiet()).unwrap();
    let left = &by_name(&out.units)[&hot].key;
    assert!(lifts.values().all(|(_, u)| u[&hot].key != *left), "{left}");
    assert_eq!(by_name(&out.units)[&cold].key, lifts[&(false, false)].1[&cold].key);
    assert_eq!(hot_split_of(&o.gen_dir), Some(Some(BTreeSet::from([F0]))));
    let _ = std::fs::remove_dir_all(&dir);
}
