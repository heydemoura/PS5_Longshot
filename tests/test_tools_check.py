"""Game-free checks of the Check tools workflow (tools-check.yml; docs/dev/reference/releases.md, "Bumping a tool"): it
runs by hand, on a check runner, with no secret, makes each tool twice and uploads nothing to the store; and no workflow
here holds the packages token or asks for the release runners' label. (The archives and the CI images are made, checked
and pushed by MagicBeanAdmin/Release's longshot-tools.yml and longshot-ci-image.yml, with their checks there.) Reads
the files.

Run with: python -I tests/test_tools_check.py
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".forgejo" / "workflows"
CHECK = (WORKFLOWS / "tools-check.yml").read_text(encoding="utf-8")


class ToolsCheckTests(unittest.TestCase):
    def test_by_hand_on_a_check_runner(self):
        self.assertRegex(CHECK, r"(?m)^on:\n  workflow_dispatch:\n")
        self.assertNotRegex(CHECK, r"(?m)^  (push|pull_request|pull_request_target|schedule):")
        self.assertEqual(re.findall(r"(?m)^    runs-on: (.*)$", CHECK), ["ubuntu-24.04"])
        for gone in ("secrets.", "enable-openid-connect", "forgejo.token", "RELEASE_TOKEN"):
            self.assertNotIn(gone, CHECK)

    def test_each_tool_is_made_twice(self):
        self.assertIn("builder/tools/make.sh --out made --check", CHECK)
        # (the input reaches the script through env, never pasted into it)
        self.assertNotRegex(CHECK, r"run:[^\n]*\$\{\{ *inputs")
        self.assertIn("WANTED: ${{ inputs.tools }}", CHECK)

    def test_node_for_the_actions_is_pinned(self):
        """The Node a job in ubuntu:22.04 installs for its actions: a version and its SHA-256"""
        self.assertEqual(len(set(re.findall(r"NODE_(?:VER|SHA256): (\S+)", CHECK))), 2)

    def test_no_workflow_publishes_packages(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path.name):
                self.assertNotIn("PACKAGES_TOKEN", text)
                self.assertNotRegex(text, r"(?m)^\s*runs-on:.*\brelease\b")


if __name__ == "__main__":
    unittest.main()
