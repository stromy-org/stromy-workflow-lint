#!/usr/bin/env python3
"""Compare immutable and channel Copier renders without claiming channel admission.

Only first-party control-plane refs and three answer provenance scalars may
change. The live channel policy is deliberately outside this public artifact.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path

import yaml
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

CI_INPUTS: tuple[str, ...] = ()
ROOT_REPO = "stromy-org/stromy-org"
ROOT_USE = re.compile(
    r"^stromy-org/stromy-org/\.github/(workflows|actions)/([A-Za-z0-9_.\-/]+)@([a-zA-Z0-9_.-]+)$"
)
SHA = re.compile(r"[0-9a-f]{40}")
REF = re.compile(r"ci-v[1-9][0-9]*")
ANSWERS = {"ci_release_track", "ci_release", "copier_engine_release"}
MAX_FILES = 2000
MAX_BYTES = 64_000_000


class Unavailable(ValueError):
    """The proposed comparison did not have a complete readable input."""


class Drift(ValueError):
    """A render changed outside the narrowly approved substitutions."""


def tree(root: Path) -> dict[str, tuple[str, int]]:
    if root.is_symlink() or not root.is_dir():
        raise Unavailable("render root is not a directory")
    result: dict[str, tuple[str, int]] = {}
    total = 0
    for entry in root.rglob("*"):
        name = entry.relative_to(root).as_posix()
        if entry.is_symlink():
            target = entry.readlink().as_posix()
            if (entry.readlink().is_absolute() or not Path(os.path.normpath(
                    str(entry.parent / target))).is_relative_to(root)):
                raise Unavailable("render symlink leaves its root: " + name)
            result[name] = (target, -1)
            if len(result) > MAX_FILES:
                raise Unavailable("render exceeds comparison bounds")
            continue
        if entry.is_dir():
            continue
        mode = entry.stat().st_mode
        if not stat.S_ISREG(mode):
            raise Unavailable("render contains a non-file: " + name)
        if name.startswith(".git/") or name == ".git":
            raise Unavailable("render contains Git metadata")
        raw = entry.read_bytes()
        total += len(raw)
        if len(result) >= MAX_FILES or total > MAX_BYTES:
            raise Unavailable("render exceeds comparison bounds")
        try:
            result[name] = (raw.decode("utf-8"), mode & 0o111)
        except UnicodeDecodeError:
            # Binary files must remain byte-identical and cannot contain refs.
            result[name] = (raw.decode("latin-1"), mode & 0o111)
    if not result:
        raise Unavailable("render is empty")
    return result


def parsed(source: str, name: str) -> MappingNode:
    try:
        if any(event.anchor is not None for event in yaml.parse(source) if hasattr(event, "anchor")):
            raise Unavailable("YAML anchors are not a comparison input: " + name)
        if any(isinstance(event, AliasEvent) for event in yaml.parse(source)):
            raise Unavailable("YAML aliases are not a comparison input: " + name)
        document = yaml.compose(source)
    except yaml.YAMLError as exc:
        raise Unavailable("invalid YAML: " + name) from exc
    if not isinstance(document, MappingNode):
        raise Unavailable("YAML root is not a mapping: " + name)
    def unique(node: object) -> None:
        if isinstance(node, MappingNode):
            seen: set[str] = set()
            for key, value in node.value:
                if not isinstance(key, ScalarNode) or key.value in seen:
                    raise Unavailable("duplicate or complex YAML key: " + name)
                seen.add(key.value)
                unique(value)
        elif isinstance(node, SequenceNode):
            for value in node.value:
                unique(value)
    unique(document)
    return document


def field(node: MappingNode, key: str) -> object | None:
    return next((value for item, value in node.value if item.value == key), None)


def mappings(node: object) -> list[MappingNode]:
    if not isinstance(node, SequenceNode):
        return []
    return [item for item in node.value if isinstance(item, MappingNode)]


def replace_scalars(source: str, replacements: list[tuple[ScalarNode, str]]) -> str:
    for node, value in sorted(replacements, key=lambda pair: pair[0].start_mark.index, reverse=True):
        if node.style is not None:
            raise Unavailable("allowed scalar must use plain YAML style")
        source = source[:node.start_mark.index] + value + source[node.end_mark.index:]
    return source


def root_ref(node: object, kind: str, sha: str, target: str) -> tuple[ScalarNode, str] | None:
    if not isinstance(node, ScalarNode):
        return None
    match = ROOT_USE.fullmatch(node.value)
    if not match:
        if node.value.startswith(ROOT_REPO + "/.github/"):
            raise Drift("first-party reference has an unsupported path or ref")
        return None
    route, path, ref = match.groups()
    if (route != kind or ref != sha or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise Drift("first-party reference changed from the immutable root")
    return node, node.value.rsplit("@", 1)[0] + "@" + target


def expected_yaml(source: str, name: str, track: str, sha: str, target: str) -> tuple[str, int]:
    root = parsed(source, name)
    replacements: list[tuple[ScalarNode, str]] = []
    changes = 0
    if name == ".copier-answers.yml":
        fields = {key.value: value for key, value in root.value if key.value in ANSWERS}
        if set(fields) != ANSWERS or any(not isinstance(value, ScalarNode) for value in fields.values()):
            raise Unavailable("answer provenance is incomplete")
        if fields["ci_release_track"].value != "immutable" or any(
            fields[key].value != sha for key in ("ci_release", "copier_engine_release")
        ):
            raise Drift("reference answer provenance does not match immutable SHA")
        for key, value in (("ci_release_track", track), ("ci_release", target),
                           ("copier_engine_release", target)):
            replacements.append((fields[key], value))
        changes += 3
    elif name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml")):
        jobs = field(root, "jobs")
        if not isinstance(jobs, MappingNode):
            raise Unavailable("workflow jobs unavailable: " + name)
        copier_callers = 0
        for _job_key, job in jobs.value:
            if not isinstance(job, MappingNode):
                continue
            job_use = field(job, "uses")
            job_result = root_ref(job_use, "workflows", sha, target)
            if job_result:
                replacements.append(job_result)
                changes += 1
            steps = field(job, "steps")
            for step in mappings(steps):
                result = root_ref(field(step, "uses"), "actions", sha,
                                  target if track == "channel" else sha)
                if result:
                    replacements.append(result)
                    changes += 1
            if name == ".github/workflows/copier-update.yml" and job_result:
                copier_callers += 1
                inputs = field(job, "with")
                engine = field(inputs, "engine-ref") if isinstance(inputs, MappingNode) else None
                if not isinstance(engine, ScalarNode) or engine.value != sha:
                    raise Drift("copier engine ref is not paired with immutable caller")
                replacements.append((engine, target))
                changes += 1
        if name == ".github/workflows/copier-update.yml" and copier_callers != 1:
            raise Drift("copier workflow must have one paired first-party caller")
    elif name.startswith(".github/actions/") and name.endswith(("/action.yml", "/action.yaml")):
        runs = field(root, "runs")
        steps = field(runs, "steps") if isinstance(runs, MappingNode) else None
        for step in mappings(steps):
            result = root_ref(field(step, "uses"), "actions", sha,
                              target if track == "channel" else sha)
            if result:
                replacements.append(result)
                changes += 1
    return replace_scalars(source, replacements), changes


def compare(reference: Path, candidate: Path, track: str, sha: str,
            canary_ref: str = "ci-v8") -> dict[str, object]:
    if track not in {"channel", "canary", "immutable"} or not SHA.fullmatch(sha):
        raise Unavailable("track and immutable source SHA are required")
    if not REF.fullmatch(canary_ref):
        raise Unavailable("canary ref must be an immutable ci-vN tag name")
    target = {"channel": "ci-stable", "canary": canary_ref, "immutable": sha}[track]
    before, after = tree(reference), tree(candidate)
    if before.keys() != after.keys():
        raise Drift("render file paths differ")
    if ".copier-answers.yml" not in before:
        raise Unavailable("answer provenance file is missing")
    allowed = 0
    for name, (source, source_mode) in before.items():
        actual, actual_mode = after[name]
        if source_mode != actual_mode:
            raise Drift("file executable bit changed: " + name)
        if source_mode == -1:
            if source != actual:
                raise Drift("symlink target changed: " + name)
            continue
        expected = source
        workflow = name.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))
        action = name.startswith(".github/actions/") and name.endswith(("/action.yml", "/action.yaml"))
        if (name == ".copier-answers.yml" or
                ((workflow or action) and
                 (source != actual or ROOT_REPO + "/.github/" in source))):
            expected, count = expected_yaml(source, name, track, sha, target)
            allowed += count
        if actual != expected:
            raise Drift("render content changed outside approved scalar refs: " + name)
    if track != "immutable" and allowed <= 3:
        raise Drift("no first-party root reference was compared")
    return {"status": "PASS", "admission": "NOT-RUN", "track": track,
            "files": len(before), "allowed_scalars": allowed}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--track", choices=("channel", "canary", "immutable"), required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--canary-ref", default="ci-v8")
    args = parser.parse_args()
    try:
        result = compare(args.reference, args.candidate, args.track, args.sha, args.canary_ref)
    except Drift as exc:
        result = {"status": "FINDING", "admission": "NOT-RUN", "reason": str(exc)}
        code = 1
    except (OSError, Unavailable) as exc:
        result = {"status": "NOT-RUN", "admission": "NOT-RUN", "reason": str(exc)}
        code = 3
    else:
        code = 0
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
