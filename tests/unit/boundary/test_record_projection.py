"""The stored record and the journal summary carry product-computed values only.

Anything the constructor wrote (a file path, a script, argv, a failure
signature, a binding symbol, a parameter name, a case, a locator, a reason)
can carry a held-out value. Neither ``package_record_bytes`` nor
``manifest_summary`` may contain any of it, wherever the constructor put it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import hashlib
import json
from typing import Any

import pytest

from ouroboros.boundary.oracle_build import package_from_reply
from ouroboros.boundary.package import (
    AssertionLink,
    BaseFileRef,
    CheckPackage,
    CheckRole,
    CheckSpec,
    PackageFile,
    UncoveredObligation,
    package_record,
    package_record_bytes,
    seal_package,
    seed_criterion_keys,
    seed_digest,
)

from .clamp_fixtures import _seed

SENTINEL = "6173"
FIXED_TIME = datetime(2026, 9, 26, tzinfo=UTC)
SEED = _seed()  # one Seed: its metadata carries a creation time


def _script_path(secret: str) -> str:
    return f".ouroboros_checks/expected_{secret}.py"


def _script(secret: str) -> str:
    # The bot's probe: the held-out value copied into a referenced preservation script.
    return f"from mathutils import clamp\nassert clamp(-3, -2, {secret}) == -2\n"


def _reply(secret: str = SENTINEL) -> dict[str, Any]:
    """A valid reply with ``secret`` in every constructor-controlled free-form field."""
    oracle = {
        "criterion": 1,
        "check_id": f"oracle_{secret}",
        "role": "reproduction",
        "call_kind": "method",
        "params": [f"value_{secret}", "low", "high"],
        "default_binding": {
            "symbol": f"mathutils_{secret}.Clamp{secret}.clamp",
            "arg_map": {f"value_{secret}": 0, "low": 1, "high": f"high_{secret}"},
        },
        "target_named_in_criterion": False,
        "cases": [
            {
                "case_id": f"stated_{secret}",
                "held_out": False,
                "args": {f"value_{secret}": 15, "low": 0, "high": f"{secret}"},
                "init": {"note": f"{secret}"},
                "expect": {"kind": "returns", "value": f"visible {secret}"},
            },
            {
                "case_id": f"held_{secret}",
                "held_out": True,
                "args": {f"value_{secret}": -3, "low": -2, "high": int(secret)},
                "expect": {"kind": "raises", "exception": f"Error{secret}"},
            },
        ],
    }
    preservation = {
        "check_id": f"s{secret}",
        "role": "preservation",
        "argv": ["python3", _script_path(secret)],
        "assertions": [{"criterion": 2, "assertion_id": f"a{secret}", "locator": secret}],
    }
    reproduction = {
        "check_id": f"r{secret}",
        "role": "reproduction",
        "argv": ["python3", _script_path(secret)],
        "failure_signature": f"CLAMP_FAILED_{secret}_SIGNATURE",
        "assertions": [{"criterion": 2, "assertion_id": f"b{secret}"}],
    }
    return {
        "oracles": [oracle],
        "checks": [preservation, reproduction],
        "files": [{"path": _script_path(secret), "content": _script(secret)}],
        "uncovered": [{"criterion": 3, "reason": f"reason {secret}"}],
    }


def _caller_metadata(secret: str) -> dict[str, Any]:
    """Metadata a caller passes with a package, each carrying ``secret``."""
    return {
        "input_digest": hashlib.sha256(secret.encode()).hexdigest(),
        "generator": f"constructor_{secret}",
        "generated_at": FIXED_TIME.replace(microsecond=int(secret[:6])),
    }


def _from_reply(secret: str = SENTINEL) -> CheckPackage:
    keys = seed_criterion_keys(SEED)
    return package_from_reply(
        _reply(secret),
        SEED,
        product_uncovered={keys[2]: f"held_out_{secret}"},
        **_caller_metadata(secret),
    )


def _blind(text: str, package: CheckPackage) -> str:
    """``text`` with the package's random id and its Seed digest replaced by placeholders.

    Both are hex strings that vary from run to run (the id is random, the
    fixture Seed carries its creation time): a numeric sentinel can occur in
    them by chance, which says nothing about a leak. Every other byte is kept.
    """
    if package.sealed:
        text = text.replace(package.package_id, "<package_id>")
    return text.replace(package.seed_digest, "<seed_digest>")


def _assert_sentinel_absent(package: CheckPackage) -> None:
    sealed = seal_package(package)
    record = package_record_bytes(sealed)
    summaries = (
        _blind(json.dumps(sealed.manifest_summary()), sealed),
        _blind(json.dumps(package.manifest_summary()), package),
    )
    assert SENTINEL not in _blind(record.decode("utf-8"), sealed)
    assert json.loads(record) == package_record(sealed)
    for text in summaries:
        assert SENTINEL not in text


def test_no_constructor_text_reaches_the_record_or_the_summary() -> None:
    package = _from_reply()
    # The sentinel is really in the package: in the script, its path and argv,
    # the signature, the binding, the params and the cases.
    assert SENTINEL.encode() in package.to_json_bytes()
    assert any(SENTINEL in item.content for item in package.files)
    _assert_sentinel_absent(package)


def _assembled(secret: str = SENTINEL) -> CheckPackage:
    """A package a caller assembled directly, with ``secret`` in every free-form field.

    Metadata, the uncovered reason, the script, its path, links and
    locator, base_files and scratch_paths.
    """
    seed = SEED
    keys = seed_criterion_keys(seed)
    script = PackageFile.from_content(_script_path(secret), _script(secret))
    return CheckPackage(
        seed_digest=seed_digest(seed),
        criterion_keys=keys,
        **_caller_metadata(secret),
        checks=(
            CheckSpec(
                check_id="script_1_1",
                role=CheckRole.PRESERVATION,
                argv=("python3", _script_path(secret)),
                assertions=tuple(
                    AssertionLink(
                        assertion_id=f"script_1_1.a{position}",
                        criterion_key=key,
                        file=_script_path(secret),
                        locator=f"line {secret}",
                    )
                    for position, key in enumerate(keys[:2], start=1)
                ),
            ),
        ),
        uncovered=(UncoveredObligation(criterion_key=keys[2], reason=f"held_out_{secret}"),),
        files=(script,),
        base_files=(
            BaseFileRef(
                path=f"src/value_{secret}.py", sha256=hashlib.sha256(secret.encode()).hexdigest()
            ),
        ),
        scratch_paths=(f"scratch_{secret}",),
    )


def test_paths_the_package_carries_from_any_caller_are_not_persisted() -> None:
    # base_files and scratch_paths are not set by the reply parser; a caller
    # that assembles a package can still put a held-out value in them.
    _assert_sentinel_absent(_assembled())


def test_the_record_and_the_summary_are_one_projection() -> None:
    package = seal_package(_from_reply())
    record = package_record(package)
    assert record["package"] == package.manifest_summary()
    assert (record["package_id"], record["seed_digest"]) == (
        package.package_id,
        package.seed_digest,
    )


def _normalized(package: CheckPackage) -> tuple[bytes, str, str]:
    """Record bytes and summaries with the random package id replaced by a placeholder."""
    sealed = seal_package(package)
    placeholder = "0" * len(sealed.package_id)

    def blind(text: str) -> str:
        return text.replace(sealed.package_id, placeholder)

    return (
        blind(package_record_bytes(sealed).decode("utf-8")).encode("utf-8"),
        blind(json.dumps(sealed.manifest_summary(), sort_keys=True)),
        json.dumps(package.manifest_summary(), sort_keys=True),
    )


@pytest.mark.parametrize("build", [_from_reply, _assembled])
@pytest.mark.parametrize("other", ["4409", "12", "987654"])
def test_packages_differing_only_in_constructor_bytes_persist_identically(
    build: Callable[[str], CheckPackage], other: str
) -> None:
    # The real guard: nothing persisted is a function of what the constructor
    # wrote (not its text, not a digest or a length of it), so no persisted
    # byte can confirm or narrow a guessed held-out value.
    first, second = build(SENTINEL), build(other)
    assert first.to_json_bytes() != second.to_json_bytes()
    assert _normalized(first) == _normalized(second)


def test_a_held_out_value_in_a_script_cannot_be_enumerated_from_the_record() -> None:
    # The bot's probe: try every 4-digit value against the persisted bytes.
    sealed = seal_package(_from_reply())
    haystack = package_record_bytes(sealed) + json.dumps(sealed.manifest_summary()).encode()
    recovered = [
        guess
        for guess in (f"{number:04d}" for number in range(10_000))
        if hashlib.sha256(_script(guess).encode()).hexdigest().encode() in haystack
    ]
    assert recovered == []


# ---------------------------------------------------------------- persisted ids


def _script_check(check_id: str, assertion_ids: tuple[str, ...]) -> CheckSpec:
    keys = seed_criterion_keys(SEED)
    return CheckSpec(
        check_id=check_id,
        role=CheckRole.PRESERVATION,
        argv=("python3", _script_path("1")),
        assertions=tuple(
            AssertionLink(assertion_id=assertion_id, criterion_key=key)
            for assertion_id, key in zip(assertion_ids, keys, strict=False)
        ),
    )


def _direct(*checks: CheckSpec, **fields: Any) -> CheckPackage:
    keys = seed_criterion_keys(SEED)
    return CheckPackage(
        seed_digest=seed_digest(SEED),
        criterion_keys=keys,
        input_digest="1" * 64,
        generated_at=FIXED_TIME,
        checks=checks,
        files=(PackageFile.from_content(_script_path("1"), _script("1")),),
        uncovered=tuple(
            UncoveredObligation(criterion_key=key, reason="constructor_omitted")
            for key in keys
            if key not in {link.criterion_key for check in checks for link in check.assertions}
        ),
        **fields,
    )


def test_a_directly_built_package_carries_only_minted_script_ids() -> None:
    assert _direct(_script_check("script_1_1", ("script_1_1.a1", "script_1_1.a2")))
    for check in (
        _script_check(f"check_{SENTINEL}", ("check_6173.a1",)),  # free text
        _script_check("script_2_1", ("script_2_1.a1",)),  # names another criterion
        _script_check("script_1_1", (f"a{SENTINEL}",)),  # free assertion id
        _script_check("script_1_1", ("script_1_1.a2",)),  # not its position
        _script_check("script_1_01", ("script_1_01.a1",)),  # not the minted form
    ):
        with pytest.raises(ValueError):
            _direct(check)


def _oracle_spec(check_id: str) -> Any:
    from ouroboros.boundary.oracle_build import build_oracle_spec

    oracle = _reply("7")["oracles"][0]
    return build_oracle_spec(
        SEED,
        criterion_index=0,
        check_id=check_id,
        call_kind=oracle["call_kind"],
        params=oracle["params"],
        default_binding=oracle["default_binding"],
        cases=oracle["cases"],
    )


def test_a_directly_built_oracle_check_is_the_product_harness_check_exactly() -> None:
    from ouroboros.boundary.oracle_build import assemble_package
    from ouroboros.boundary.package import oracle_check

    spec = _oracle_spec("oracle_1")
    assert assemble_package(
        SEED, input_digest="1" * 64, generator=None, oracles=[(spec, CheckRole.REPRODUCTION)]
    )
    # An oracle id naming another criterion than the one it checks.
    with pytest.raises(ValueError):
        _direct_oracle(_oracle_spec("oracle_2"))
    # The oracle check with an assertion id the harness check does not have.
    minted = oracle_check(spec, CheckRole.REPRODUCTION)
    altered = minted.model_copy(
        update={
            "assertions": (
                minted.assertions[0].model_copy(update={"assertion_id": f"a{SENTINEL}"}),
                *minted.assertions[1:],
            )
        }
    )
    packaged = assemble_package(
        SEED, input_digest="1" * 64, generator=None, oracles=[(spec, CheckRole.REPRODUCTION)]
    )
    with pytest.raises(ValueError):
        CheckPackage.model_validate(
            {
                **packaged.model_dump(),
                "checks": [altered.model_dump(), *[c.model_dump() for c in packaged.checks[1:]]],
            }
        )


def _direct_oracle(spec: Any) -> CheckPackage:
    """A package built around ``spec`` directly, without the product's minting."""
    from ouroboros.boundary.package import oracle_check, oracle_files

    keys = seed_criterion_keys(SEED)
    return CheckPackage(
        schema_version="ouroboros.check_package.v2",
        seed_digest=seed_digest(SEED),
        criterion_keys=keys,
        input_digest="1" * 64,
        generated_at=FIXED_TIME,
        checks=(oracle_check(spec, CheckRole.REPRODUCTION),),
        files=oracle_files([spec]),
        uncovered=tuple(
            UncoveredObligation(criterion_key=key, reason="constructor_omitted") for key in keys[1:]
        ),
        oracles=(spec,),
        binding_grammar="ouroboros.binding_grammar.v1",
    )


def _with_case_ids(spec: Any, case_ids: tuple[str, ...]) -> Any:
    from ouroboros.boundary.oracle import OracleSpec

    dumped = spec.model_dump()
    dumped["cases"] = [
        {**case, "case_id": case_id}
        for case, case_id in zip(dumped["cases"], case_ids, strict=True)
    ]
    return OracleSpec.model_validate(dumped)


def test_ids_are_the_dense_sequence_the_product_mints_from_structure() -> None:
    # The bot's probe: a Seed-valid package whose script id spells a value.
    for check in (
        _script_check(f"script_1_{SENTINEL}", (f"script_1_{SENTINEL}.a1",)),
        _script_check("script_1_2", ("script_1_2.a1",)),  # a gap before it
    ):
        with pytest.raises(ValueError):
            _direct(check)
    assert _direct_oracle(_oracle_spec("oracle_1"))
    for spec in (
        _oracle_spec("oracle_1_2"),  # no oracle_1 before it
        _with_case_ids(_oracle_spec("oracle_1"), ("c1", f"c{SENTINEL}")),  # a case gap
    ):
        with pytest.raises(ValueError):
            _direct_oracle(spec)


def test_assembling_mints_dense_ids_whatever_the_parts_carried() -> None:
    # A rebuilt package (after cases or oracles are dropped) is minted again
    # from its structure by the same function, so no gap survives.
    from ouroboros.boundary.oracle_build import assemble_package

    spec = _with_case_ids(_oracle_spec(f"oracle_1_{SENTINEL}"), ("c1", f"c{SENTINEL}"))
    package = assemble_package(
        SEED,
        input_digest="1" * 64,
        generator=None,
        oracles=[(spec, CheckRole.REPRODUCTION)],
        script_checks=[_script_check(f"script_1_{SENTINEL}", (f"script_1_{SENTINEL}.a1",))],
        script_files=(PackageFile.from_content(_script_path("1"), _script("1")),),
    )
    assert [check.check_id for check in package.checks] == ["oracle_1", "script_1_1"]
    assert [case.case_id for case in package.oracles[0].cases] == ["c1", "c2"]
    assert [link.assertion_id for check in package.checks for link in check.assertions] == [
        "oracle_1.c1",
        "oracle_1.c2",
        "script_1_1.a1",
    ]
    sealed = seal_package(package)
    assert SENTINEL not in _blind(package_record_bytes(sealed).decode("utf-8"), sealed)
