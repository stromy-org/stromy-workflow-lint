"""Positive and mutation controls for the public render delta comparator."""
from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("render_delta", ROOT / "render_delta.py")
assert SPEC and SPEC.loader
delta = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delta)
SHA = "a" * 40


def render(root: Path, track: str) -> None:
    ref = {"immutable": SHA, "channel": "ci-stable", "canary": "ci-v8"}[track]
    action_ref = "ci-stable" if track == "channel" else SHA
    files = {
        ".copier-answers.yml":
            f"_src_path: /same/source\nci_release_track: {track}\nci_release: {ref}\n"
            f"copier_engine_release: {ref}\nother: preserved\n",
        ".github/workflows/ci.yml":
            "on: push\npermissions: read-all\njobs:\n  lint:\n"
            f"    uses: stromy-org/stromy-org/.github/workflows/ci-python.yml@{ref}\n"
            "    secrets:\n      APP_ID: ${{ secrets.APP_ID }}\n"
            "  local:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"      - uses: stromy-org/stromy-org/.github/actions/check@{action_ref}\n"
            f"      - uses: actions/checkout@{'b' * 40}\n",
        ".github/workflows/copier-update.yml":
            "on: workflow_dispatch\njobs:\n  update:\n"
            f"    uses: stromy-org/stromy-org/.github/workflows/copier-update.yml@{ref}\n"
            f"    with:\n      engine-ref: {ref}\n",
        ".github/actions/local/action.yml":
            "name: Local\nruns:\n  using: composite\n  steps:\n"
            f"    - uses: stromy-org/stromy-org/.github/actions/check@{action_ref}\n",
        ".github/actions/local/verify.sh":
            "#!/bin/sh\n# SOURCE: stromy-org/stromy-org/.github/actions/local\nexit 0\n",
        "scripts/check.sh": "#!/bin/sh\nexit 0\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


class DeltaTest(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="render-delta-")
        self.addCleanup(self.scratch.cleanup)
        self.before = Path(self.scratch.name) / "before"
        self.after = Path(self.scratch.name) / "after"
        render(self.before, "immutable")

    def compare(self, track):
        return delta.compare(self.before, self.after, track, SHA)

    def test_all_three_modes_are_structural_only(self):
        for track in ("immutable", "channel", "canary"):
            with self.subTest(track=track):
                render(self.after, track)
                result = self.compare(track)
                self.assertEqual((result["status"], result["admission"]), ("PASS", "NOT-RUN"))
                self.assertGreater(result["allowed_scalars"], 3)

    def test_dangerous_mutations_fail(self):
        mutations = {
            "permission": (".github/workflows/ci.yml", "permissions: read-all", "permissions: write-all"),
            "secret": (".github/workflows/ci.yml", "APP_ID: ${{ secrets.APP_ID }}", "APP_ID: ${{ secrets.OTHER }}"),
            "third-party": (".github/workflows/ci.yml", "actions/checkout@", "actions/setup-node@"),
            "root-path": (".github/workflows/ci.yml", "ci-python.yml@", "other.yml@"),
            "script": ("scripts/check.sh", "exit 0", "exit 1"),
            "engine": (".github/workflows/copier-update.yml", "engine-ref: ci-stable", "engine-ref: ci-v8"),
            "provenance": (".copier-answers.yml", "other: preserved", "other: changed"),
            "source": (".copier-answers.yml", "/same/source", "/other/source"),
        }
        for label, (name, old, new) in mutations.items():
            with self.subTest(label=label):
                render(self.after, "channel")
                path = self.after / name
                path.write_text(path.read_text().replace(old, new))
                with self.assertRaises(delta.Drift):
                    self.compare("channel")
        render(self.after, "channel")
        (self.after / "extra.txt").write_text("new path")
        with self.assertRaises(delta.Drift):
            self.compare("channel")

    def test_unavailable_and_file_mode_do_not_pass(self):
        render(self.after, "channel")
        script = self.after / "scripts/check.sh"
        script.chmod(0o755)
        with self.assertRaises(delta.Drift):
            self.compare("channel")
        script.chmod(0o644)
        (self.after / ".copier-answers.yml").unlink()
        with self.assertRaises(delta.Drift):
            self.compare("channel")

    def test_unchanged_old_root_ref_still_fails(self):
        render(self.after, "channel")
        name = ".github/workflows/ci.yml"
        for root, ref in ((self.before, SHA), (self.after, "ci-stable")):
            path = root / name
            path.write_text(path.read_text().replace("ci-python.yml@" + ref,
                                                    "ci-python.yml@ci-v7"))
        with self.assertRaisesRegex(delta.Drift, "immutable root"):
            self.compare("channel")


if __name__ == "__main__":
    unittest.main()
