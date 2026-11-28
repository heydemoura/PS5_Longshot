//! The hot units' hot-cold splitting (on by default; `LONGSHOT_HOT_SPLIT=0` turns it off): the compile's half, and the
//! one reading of the switch ([`on`]) the lift's half takes too.
//!
//! With the switch on, the lift writes each hot unit with every branch the guest profile saw run but never taken
//! weighted 0 (llvm's `Module::split_cold`; 1 into a block that ran through another edge): the blocks behind a 0 never
//! ran. The hot units are then compiled with LLVM's hot-cold splitting, which moves such a region into a `cold`
//! function of its own where it can (one way in, few live values), and block placement puts the rest of them after the
//! blocks that ran. The code that runs every frame is denser in the instruction caches, the instruction TLB and the L3
//! (in Market about 12,000 code pages run every 50-100 ms, and a little over a third of the hot units' guest
//! instructions are in blocks that ran).

/// Whether the switch `LONGSHOT_HOT_SPLIT` (`value`, as set) has the hot units made for hot-cold splitting: unset on,
/// off when [`build_core::switch`] reads it off (`0`, `off`, `no`, `false`, empty), and on for any other value. The
/// lift (build_driver::stages::hot_split) and the compile ([`crate::Settings::hot_split`]) both read it here, so that
/// the two halves cannot disagree on the default.
pub fn on(value: Option<&str>) -> bool {
    value.is_none_or(|v| build_core::switch(v) != Some(false))
}

/// The hot units' extra flags with the switch on (none off): splitting on, and a block cold only behind a branch of
/// probability at most 2^-31, a branch the profile never saw taken (LLVM's default, 1 %, would also take blocks that
/// ran). A call of a `cold` function (a float's slow path) still makes its block cold, whatever the probability.
pub fn flags(on: bool) -> Vec<String> {
    if !on {
        return Vec::new();
    }
    ["-mllvm", "-hot-cold-split=true", "-mllvm", "-hotcoldsplit-cold-probability-denom=2147483647"]
        .map(String::from)
        .to_vec()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unset_is_on_and_an_off_word_is_off() {
        assert!(on(None), "on by default");
        for v in ["1", "on", "yes", "true", " 1 ", "2"] {
            assert!(on(Some(v)), "{v:?}: on");
        }
        for v in ["0", "off", "no", "false", "OFF", "", " 0 "] {
            assert!(!on(Some(v)), "{v:?}: off");
        }
    }

    #[test]
    fn off_adds_nothing_and_on_splits_behind_never_taken_branches_alone() {
        assert!(flags(false).is_empty());
        let f = flags(true);
        for want in ["-hot-cold-split=true", "-hotcoldsplit-cold-probability-denom=2147483647"] {
            let at = f.iter().position(|x| x == want).unwrap_or_else(|| panic!("{want} in {f:?}"));
            assert_eq!(f[at - 1], "-mllvm", "{f:?}");
        }
    }
}
