//! `--output json`: every command says the builder's events on stdout and nothing else, as `longshot-builder dev` does.

#[test]
fn json_output_captures_results_and_diagnostics() {
    let lines: [(&[&str], i32, &str); 6] = [
        (&["--help"], 0, "log"),
        (&["release", "--help"], 0, "log"),
        (&["release", "nothing"], 2, "log"),
        (&["tools", "check", "--lock", "no-such-tools.lock"], 1, "log"),
        (&["compile"], 2, "log"),
        (&["rust-key", "--root", "."], 0, "log"),
    ];
    for flag in [vec!["--output=json"], vec!["--output", "json"]] {
        for (words, status, kind) in lines {
            let out = std::process::Command::new(env!("CARGO_BIN_EXE_longshot-packager"))
                .args(words)
                .args(&flag)
                .output()
                .unwrap();
            assert_eq!(out.status.code(), Some(status), "{words:?}");
            assert!(out.stderr.is_empty(), "{words:?}: {}", String::from_utf8_lossy(&out.stderr));
            let event: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
            assert_eq!((event["v"].as_i64(), event["type"].as_str()), (Some(1), Some(kind)), "{words:?}");
            assert_eq!(event["level"], if status == 0 { "INFO" } else { "ERROR" }, "{words:?}");
        }
    }
}
