"""Game-free: the PS5 Builder carries and runs the button-prompt generator.

python -I tests/test_ps5_builder_prompts.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    pipeline = (ROOT / "ps5/builder/pipeline.py").read_text(encoding="utf-8")
    app = (ROOT / "ps5/builder/app.py").read_text(encoding="utf-8")
    kit = (ROOT / "ps5/builder/make_kit.py").read_text(encoding="utf-8")
    package = (ROOT / "ps5/builder/package.py").read_text(encoding="utf-8")
    if '"--only", "romfs", "--extract"' not in pipeline:
        raise SystemExit("FAIL: PS5 Builder does not extract the RomFS")
    if '"prompt_assets.py"' not in pipeline or '"--incremental"' not in pipeline:
        raise SystemExit("FAIL: PS5 Builder does not make incremental prompt assets")
    if "prompts(kit, work, a.game, app)" not in pipeline:
        raise SystemExit("FAIL: PS5 Builder does not run the prompt step")
    steps = re.search(r'^STEPS = \[(.*?)\]', app, re.M | re.S)
    if not steps or "Making button prompts" not in steps.group(1):
        raise SystemExit("FAIL: the Builder window does not show the prompt step")
    if "third_party/kenney_input" not in kit or "third_party/kenney_input_keyboard" not in kit:
        raise SystemExit("FAIL: the PS5 kit does not carry the prompt fonts")
    for package_name in ("fonttools", "pillow", "astc-encoder-py", "texture2ddecoder"):
        if package_name not in package:
            raise SystemExit("FAIL: the PS5 package does not install " + package_name)
    print("ok: PS5 Builder extracts the RomFS and builds button prompts")


if __name__ == "__main__":
    main()
