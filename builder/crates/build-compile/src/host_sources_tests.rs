//! The host's C files the game compiles ([`host_sources`]): the game's, game/ beside host/, among them (the frame
//! phases, game/phases.c, which tests/test_game_phases.py builds and runs, and the menu rows, game/menu/), on both
//! OSes; and the game's C++, game/*.cpp alone, compiled as C++ with the host's flags and [`GAME_CXX`] ([`plan_host`]).

use super::*;
use crate::options::Settings;

#[test]
fn the_game_compiles_game_s_files_on_both_oses() {
    let d = crate::testdir("host-sources");
    let (host, posix) = (d.join("host"), d.join("host/posix"));
    for f in [
        "host/hos_main.c",
        "host/pgo_rt.c",
        "host/display.c",
        "host/dav1d_av1.c",
        "host/hos.h",
        "game/phases.c",
        "game/phases.h",
        "game/menu/rows.c",
    ] {
        let p = d.join(f);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, "").unwrap();
    }
    let names = |linux, dav1d| -> Vec<String> {
        host_sources(&host, &posix, linux, dav1d)
            .iter()
            .map(|p| p.strip_prefix(&d).unwrap().to_string_lossy().replace('\\', "/"))
            .collect()
    };
    assert_eq!(names(false, false), ["host/display.c", "host/hos_main.c", "game/phases.c", "game/menu/rows.c"]);
    assert_eq!(
        names(false, true),
        ["host/dav1d_av1.c", "host/display.c", "host/hos_main.c", "game/phases.c", "game/menu/rows.c"]
    );
    assert_eq!(
        names(true, false),
        [
            "host/hos_main.c",
            "game/phases.c",
            "game/menu/rows.c",
            "host/posix/win32_posix.c",
            "host/posix/display_sdl.c",
            "host/posix/linux_stubs.c",
            "host/posix/prof.c"
        ]
    );
    // (the checkout's own)
    let real = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../host");
    for linux in [false, true] {
        assert!(
            host_sources(&real, &real.join("posix"), linux, false).iter().any(|p| p.ends_with("game/phases.c")),
            "game/phases.c is not compiled (linux: {linux})"
        );
    }
}

#[test]
fn the_game_s_cpp_is_compiled_as_cpp_and_only_from_game() {
    let d = crate::testdir("host-sources-cpp");
    for sub in ["work", "gen", "build"] {
        std::fs::create_dir_all(d.join(sub)).unwrap();
    }
    let root = d.join("root");
    for f in ["host/hos_main.c", "host/x.cpp", "game/phases.c", "game/x.cpp", "game/x.hpp", "game/menu/x.cpp"] {
        let p = root.join(f);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, "").unwrap();
    }
    let (host, posix) = (root.join("host"), root.join("host/posix"));
    let names = |linux| -> Vec<String> {
        host_sources(&host, &posix, linux, false)
            .iter()
            .filter(|p| !p.starts_with(&posix))
            .map(|p| p.strip_prefix(&root).unwrap().to_string_lossy().replace('\\', "/"))
            .collect()
    };
    for linux in [false, true] {
        assert_eq!(names(linux), ["host/hos_main.c", "game/phases.c", "game/x.cpp"], "linux: {linux}");
    }

    // (its unit: the C++ compiler, the host's flags with the three, an object named as a C file's would be)
    let o = Options {
        root: root.clone(),
        work: d.join("work"),
        gen_dir: d.join("gen"),
        build_dir: d.join("build"),
        mode: Mode::GameOnly,
        jobs: Some(1),
        store: None,
        settings: Settings::from_lookup(|_| None),
        ir_text: None,
        early: None,
        gate: None,
    };
    let l = Layout::of(&o);
    for windows in [false, true] {
        let tc = Toolchain {
            windows,
            mingw: None,
            cc: PathBuf::from("cc"),
            cxx: PathBuf::from("cxx"),
            cxx_mode: Vec::new(),
            target: Vec::new(),
            native_target: Vec::new(),
            native_link: Vec::new(),
            cpu: vec!["-march=x86-64-v3".into()],
            third_party_cpu: Vec::new(),
            native_cpu: None,
            nm: None,
        };
        let i = Inputs {
            options: &o,
            tc: &tc,
            layout: &l,
            version: "-DV".into(),
            shader_stamp: None,
            vk_pgo: Pgo::Off,
        };
        let mut p = Plan::default();
        plan_host(&i, &mut p, &host, &posix, &[], &[], &[]).unwrap();
        let unit = |rel: &str| p.units.iter().find(|u| u.source == root.join(rel)).unwrap_or_else(|| panic!("{rel}"));
        let x = unit("game/x.cpp");
        assert_eq!((x.compiler.as_path(), x.object.clone()), (Path::new("cxx"), l.obj.join("game_x.o")));
        for flag in GAME_CXX {
            assert!(x.flags.iter().any(|f| f == flag), "{flag} (windows: {windows})");
        }
        assert!(x.flags.iter().any(|f| f == "-march=x86-64-v3") && x.depfile && x.kind == Kind::Tree);
        let c = unit("game/phases.c");
        assert_eq!((c.compiler.as_path(), c.object.clone()), (Path::new("cc"), l.obj.join("game_phases.o")));
        assert!(!c.flags.iter().any(|f| GAME_CXX.contains(&f.as_str())), "C stays C (windows: {windows})");
        assert_eq!(unit("host/hos_main.c").object, l.obj.join("host_hos_main.o"));
    }
    let _ = std::fs::remove_dir_all(&d);
}
