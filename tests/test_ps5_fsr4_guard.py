"""Check PS5 native Vulkan FSR 4 integration guards: PC path preserved, B_DIRECT drained, no binary weights committed."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    vk_fsr4 = (ROOT / "gpu/vk/vk_fsr4.cpp").read_text(encoding="utf-8")
    for marker in ("#ifdef __PROSPERO__", "#else", "#endif", "LONGSHOT_FSR"):
        if marker not in vk_fsr4:
            raise SystemExit(f"FAIL: gpu/vk/vk_fsr4.cpp missing {marker}")

    ps5_part, pc_part = vk_fsr4.split("#else", 1)
    if "Fsr411::Upscaler" not in ps5_part or '::mkwvk::rec::boundary(::mkwvk::rec::B_DIRECT)' not in ps5_part:
        raise SystemExit("FAIL: PS5 branch in gpu/vk/vk_fsr4.cpp missing Fsr411::Upscaler or B_DIRECT barrier")
    if "s.upscaler->Record(fr)" not in ps5_part or ps5_part.index("B_DIRECT") > ps5_part.index("s.upscaler->Record(fr)"):
        raise SystemExit("FAIL: B_DIRECT barrier must precede s.upscaler->Record(fr)")
    if "amd_fidelityfx" not in pc_part:
        raise SystemExit("FAIL: PC #else branch in gpu/vk/vk_fsr4.cpp must keep upstream FidelityFX path")

    fsr4_dir = ROOT / "gpu/vk/fsr4"
    for req in ("fsr411.h", "fsr411.cpp"):
        if not (fsr4_dir / req).is_file():
            raise SystemExit(f"FAIL: missing gpu/vk/fsr4/{req}")
    bins = [p.name for p in fsr4_dir.rglob("*") if p.suffix.lower() in (".spv", ".bin", ".dll", ".so")]
    if bins:
        raise SystemExit(f"FAIL: binary/asset files must not be committed in gpu/vk/fsr4: {bins}")

    build_py = (ROOT / "ps5/build.py").read_text(encoding="utf-8")
    if "fsr411.cpp" not in build_py or "fsr4_fsr411.o" not in build_py:
        raise SystemExit("FAIL: ps5/build.py does not compile gpu/vk/fsr4/fsr411.cpp -> fsr4_fsr411.o")

    print("ok: PS5 native Vulkan FSR 4 integration keeps PC path, drains B_DIRECT, and commits source only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
