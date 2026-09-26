"""Tier C: model-written checks rejected at admission before any command runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.boundary.admission import PackageVerdict, admit_check_package
from ouroboros.boundary.admission_rules import (
    RULE_DYNAMIC_IMPORT,
    RULE_EXEC,
    RULE_NETWORK,
    script_rule_violations,
    unsafe_checks,
)
from ouroboros.boundary.constructor import CHECK_DIR, package_from_reply
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata

# The pattern from the feature smoke, reconstructed from its description (the
# original package was not kept): the check scans every workspace module,
# loads each one by file path, and monkeypatches whatever it finds instead of
# calling a declared entry point.
SMOKE_MONKEYPATCH = """import glob
import importlib.util
import sys

sys.path.insert(0, ".")
found = None
for path in glob.glob("*.py"):
    spec = importlib.util.spec_from_file_location("candidate_" + path[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in dir(module):
        target = getattr(module, name)
        if callable(target) and getattr(target, "__code__", None) is not None:
            if target.__code__.co_argcount == 3:
                found = target
                setattr(module, name, lambda a, b, t: a + (b - a) * t)
if found is None or found(0, 10, 0.5) != 5:
    print("OUROBOROS_CHECK_FAILED:repro_lerp")
    sys.exit(1)
"""


@pytest.mark.parametrize(
    ("source", "rules"),
    [
        (SMOKE_MONKEYPATCH, {RULE_DYNAMIC_IMPORT}),
        (
            "import importlib, glob\nfor p in glob.glob('*.py'):\n"
            "    importlib.import_module(p[:-3])\n",
            {RULE_DYNAMIC_IMPORT},
        ),
        ("import runpy\nrunpy.run_path('tool.py')\n", {RULE_DYNAMIC_IMPORT}),
        ("from importlib.machinery import SourceFileLoader\n", {RULE_DYNAMIC_IMPORT}),
        ("name = 'calc'\n__import__(name)\n", {RULE_DYNAMIC_IMPORT}),
        ("exec(open('calc.py').read())\n", {RULE_EXEC}),
        ("value = eval(open('answer.txt').read())\n", {RULE_EXEC}),
        ("code = compile(open('calc.py').read(), 'calc', 'exec')\n", {RULE_EXEC}),
        ("import socket\n", {RULE_NETWORK}),
        ("from urllib.request import urlopen\nurlopen('http://example.com')\n", {RULE_NETWORK}),
        ("import requests\nrequests.get('http://example.com')\n", {RULE_NETWORK}),
        ("import http.client\n", {RULE_NETWORK}),
    ],
)
def test_unsafe_patterns_are_found(source: str, rules: set[str]) -> None:
    assert set(script_rule_violations(source)) == rules


@pytest.mark.parametrize(
    "source",
    [
        # The prescribed feature-check guards stay admissible.
        "import importlib.util, sys\nsys.path.insert(0, '.')\n"
        "if importlib.util.find_spec('mathutils') is None:\n    sys.exit(1)\n",
        "import sys\nsys.path.insert(0, '.')\nimport mathutils\n"
        "if not hasattr(mathutils, 'lerp'):\n    print('OUROBOROS_CHECK_FAILED:x')\n    sys.exit(1)\n",
        "import importlib\nm = importlib.import_module('mathutils')\n",
        "try:\n    from mathutils import lerp\nexcept (ImportError, AttributeError):\n    lerp = None\n",
        "import json, tempfile, subprocess\nsubprocess.run(['python3', 'tool.py'])\n",
        "this is not python",
    ],
)
def test_admissible_patterns_pass(source: str) -> None:
    assert script_rule_violations(source) == ()


def _seed() -> Seed:
    return Seed(
        goal="add lerp",
        acceptance_criteria=("lerp(0, 10, 0.5) returns 5",),
        ontology_schema=OntologySchema(name="m", description="math"),
        metadata=SeedMetadata(seed_id="seed_rules", ambiguity_score=0.1),
    )


async def test_the_smoke_pattern_is_rejected_at_admission_as_tier_c(tmp_path: Path) -> None:
    base = tmp_path / "base"
    base.mkdir()
    (base / "mathutils.py").write_text("def clamp(v, lo, hi):\n    return v\n")
    marker = base.parent / "ran"
    script = f"open({str(marker)!r}, 'w').write('ran')\n" + SMOKE_MONKEYPATCH
    reply = {
        "checks": [
            {
                "check_id": "repro_lerp",
                "role": "reproduction",
                "argv": ["python3", f"{CHECK_DIR}/repro_lerp.py"],
                "failure_signature": "OUROBOROS_CHECK_FAILED:repro_lerp",
                "assertions": [{"criterion": 1}],
            }
        ],
        "files": [{"path": f"{CHECK_DIR}/repro_lerp.py", "content": script}],
    }
    package = package_from_reply(reply, _seed(), input_digest="1" * 64, generator="fixture")
    assert unsafe_checks(package) == (("repro_lerp", RULE_DYNAMIC_IMPORT),)

    admission = await admit_check_package(package, base, reject_unsafe_checks=True)
    assert admission.verdict is PackageVerdict.REJECTED
    assert admission.reasons == ("unsafe_check:dynamic_workspace_import:repro_lerp",)
    assert admission.check_tiers == {"repro_lerp": "C"}
    assert admission.checks == ()  # no command ran
    assert not marker.exists()
    assert admission.event_summary()["check_tiers"] == {"repro_lerp": "C"}

    # The study default (flag unset) keeps the earlier behavior: the check runs.
    permissive = await admit_check_package(package, base)
    assert permissive.check_tiers is None and permissive.checks
    assert "check_tiers" not in permissive.event_summary()
