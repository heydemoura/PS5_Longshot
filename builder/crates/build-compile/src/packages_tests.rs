//! The VMA the build takes is new enough for the renderer: what gpu/vk/vk_vma.cpp calls of VMA 3.1 is what
//! [`VK_HEADERS`]' VMA needs (tests/test_target_reuse_memory.py checks the renderer's side).

use super::*;

#[test]
fn the_vma_taken_declares_what_the_renderer_calls() {
    let vma =
        std::fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../../gpu/vk/vk_vma.cpp")).unwrap();
    let package = VK_HEADERS.iter().find(|p| p.label == "VMA").unwrap();
    if vma.contains("vmaGetAllocationInfo2(") {
        assert_eq!(package.needs, Some("vmaGetAllocationInfo2"), "a VMA 3.0 would fail the renderer's build");
    }
}

#[test]
fn streamline_s_sdk_is_taken_for_a_windows_build_with_its_headers_only() {
    let d = crate::testdir("sl-sdk");
    let mut s = Settings::from_lookup(|_| None);
    assert_eq!(sl_sdk(&s), (None, None), "no LONGSHOT_SL_SDK, nothing said");
    s.sl_sdk = Some(d.clone());
    s.windows = true;
    let (none, why) = sl_sdk(&s);
    assert!(none.is_none() && why.unwrap().contains("has no include/sl.h"));
    std::fs::create_dir_all(d.join("include")).unwrap();
    std::fs::write(d.join("include/sl.h"), "").unwrap();
    assert_eq!(sl_sdk(&s), (Some(d.clone()), None));
    s.windows = false;
    let (none, why) = sl_sdk(&s);
    assert!(none.is_none() && why.unwrap().contains("Windows game only"), "Streamline has no Linux runtime");
}
