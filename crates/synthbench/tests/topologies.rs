//! The shipped topologies must parse and pass call-graph validation, the same
//! check `validate_config` performs.

use std::path::PathBuf;
use synthbench::config::{validate_call_graph, SynthbenchConfig};

#[test]
fn shipped_topologies_validate() {
    let dir = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../config");
    let mut seen = 0;
    for entry in std::fs::read_dir(&dir).unwrap() {
        let path = entry.unwrap().path().join("topology.json");
        if !path.exists() {
            continue;
        }
        let file = std::fs::File::open(&path).unwrap();
        let cfg: SynthbenchConfig =
            serde_json::from_reader(file).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        validate_call_graph(&cfg.call_graph).unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        seen += 1;
    }
    assert!(seen >= 2, "expected the shipped topologies, found {seen}");
}

fn run_validate(path: &std::path::Path) -> std::process::Output {
    std::process::Command::new(env!("CARGO_BIN_EXE_validate_config"))
        .arg("--config")
        .arg(path)
        .output()
        .unwrap()
}

#[test]
fn validate_config_binary_accepts_shipped_and_rejects_dangling_call() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../config");
    let ok = run_validate(&root.join("ci/topology.json"));
    assert!(
        ok.status.success(),
        "{}",
        String::from_utf8_lossy(&ok.stderr)
    );

    let mut v: serde_json::Value =
        serde_json::from_reader(std::fs::File::open(root.join("ci/topology.json")).unwrap())
            .unwrap();
    v["call_graph"]["services"][0]["methods"][0]["call_sequence"] =
        serde_json::json!([{ "NOPE::missing": 1.0 }]);
    let bad_path = std::env::temp_dir().join(format!("synthbench-bad-{}.json", std::process::id()));
    std::fs::write(&bad_path, v.to_string()).unwrap();
    let bad = run_validate(&bad_path);
    std::fs::remove_file(&bad_path).ok();
    assert!(
        !bad.status.success(),
        "dangling call target must be rejected"
    );
}
