"""Tests for reusing the parsed config.yaml while its bytes are unchanged.

The reference functions below are the pre-cache ``load_config`` read path
(``_read_yaml_file`` before the cache existed), kept verbatim so every cached
result and redacted error is compared against it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
import os
from pathlib import Path
import random
import sys
import threading
import traceback
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from ouroboros.config import _parse_cache
from ouroboros.config.credential_errors import safe_yaml_error_details
from ouroboros.config.loader import get_max_parallel_workers, load_config, load_credentials
from ouroboros.config.models import OuroborosConfig
from ouroboros.core.errors import ConfigError

_CONFIG = (
    "llm:\n"
    "  backend: claude\n"
    "  qa_model: aaaa\n"
    "llm_role_profiles:\n"
    "  qa: fast\n"
    "economics:\n"
    "  tiers:\n"
    "    frugal:\n"
    "      cost_factor: 1\n"
    "      intelligence_range: [9, 11]\n"
    "      use_cases: [routine_coding]\n"
)


def _reference_safe_load(path: Path) -> Any:
    """The parse ``load_config`` performed before the cache existed."""
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def _reference_read_yaml_file(path: Path, *, description: str = "configuration") -> Any:
    """``_read_yaml_file`` as ``load_config`` used it before the cache existed."""
    failure = None
    try:
        with path.open(encoding="utf-8") as stream:
            return yaml.safe_load(stream)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        failure = ConfigError(
            f"Failed to parse {description} file",
            config_file=str(path),
            details=safe_yaml_error_details(exc),
        )
    except OSError as exc:
        failure = ConfigError(
            f"Failed to read {description} file",
            config_file=str(path),
            details={"error_type": type(exc).__name__},
        )
    raise failure


def _config_error(fn: Callable[[Path], Any], path: Path) -> tuple[Any, ...]:
    """The public ``ConfigError`` ``fn(path)`` raises, in a comparable form."""
    with pytest.raises(ConfigError) as exc_info:
        fn(path)
    error = exc_info.value
    return (
        type(error),
        str(error),
        error.details,
        error.config_file,
        error.config_key,
        error.__context__,
        error.__cause__,
    )


def _outcome(fn: Callable[[Path], Any], path: Path) -> tuple[Any, ...]:
    """Value or exception of ``fn(path)``, in a form comparable across calls."""
    try:
        return ("ok", repr(fn(path)))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        info: list[Any] = [type(e).__name__, str(e)]
        if isinstance(e, UnicodeDecodeError):
            info += [e.encoding, e.start, e.end, e.reason, e.object]
        if isinstance(e, OSError):
            info += [e.errno, e.filename, e.filename2]
        for attr in ("problem_mark", "context_mark"):
            mark = getattr(e, attr, None)
            if mark is not None:
                info.append((mark.name, mark.line, mark.column, mark.index))
        return ("err", *info)


@pytest.fixture(autouse=True)
def _empty_cache() -> Iterator[None]:
    _parse_cache.clear()
    yield
    _parse_cache.clear()


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(_CONFIG, encoding="utf-8")
    return path


def _count_parses() -> Any:
    return patch.object(yaml, "safe_load", wraps=yaml.safe_load)


class TestParseReuse:
    def test_unchanged_file_is_parsed_once(self, config_file: Path) -> None:
        with _count_parses() as safe_load:
            configs = [load_config(config_file) for _ in range(3)]

        assert safe_load.call_count == 1
        assert all(c.model_dump() == configs[0].model_dump() for c in configs)
        assert configs[0].llm.qa_model == "aaaa"

    def test_default_config_path_is_parsed_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".ouroboros").mkdir()
        (tmp_path / ".ouroboros" / "config.yaml").write_text(_CONFIG, encoding="utf-8")

        with _count_parses() as safe_load:
            first, second, third = load_config(), load_config(), load_config()

        assert safe_load.call_count == 1
        assert first.model_dump() == second.model_dump() == third.model_dump()

    def test_same_size_rewrite_with_restored_mtime_is_reparsed(self, config_file: Path) -> None:
        before = config_file.stat()
        assert load_config(config_file).llm.qa_model == "aaaa"

        config_file.write_text(_CONFIG.replace("aaaa", "bbbb"), encoding="utf-8")
        os.utime(config_file, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = config_file.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

        with _count_parses() as safe_load:
            assert load_config(config_file).llm.qa_model == "bbbb"
        assert safe_load.call_count == 1

    def test_every_call_gets_its_own_model(self, config_file: Path) -> None:
        first = load_config(config_file)
        second = load_config(config_file)

        assert first is not second
        assert first.llm_role_profiles is not second.llm_role_profiles
        assert first.economics.tiers is not second.economics.tiers
        tier_lists = (
            first.economics.tiers["frugal"].use_cases,
            second.economics.tiers["frugal"].use_cases,
        )
        assert tier_lists[0] is not tier_lists[1]

        # Frozen models still expose mutable containers; one caller's mutation
        # must reach neither another caller's model nor later loads.
        first.llm_role_profiles["qa"] = "mutated"
        first.economics.tiers["frugal"].use_cases.append("mutated")
        first.economics.tiers.clear()

        third = load_config(config_file)
        for config in (second, third):
            assert config.llm_role_profiles == {"qa": "fast"}
            assert config.economics.tiers["frugal"].use_cases == ["routine_coding"]

    def test_validator_input_is_private_to_each_call(self, config_file: Path) -> None:
        seen: list[Any] = []
        original = OuroborosConfig.model_validate

        def mutating_validate(data: Any) -> Any:
            result = original(data)
            seen.append(data)
            data.clear()  # a validator that consumes its input in place
            return result

        with patch.object(OuroborosConfig, "model_validate", mutating_validate):
            for _ in range(3):
                assert load_config(config_file).llm.qa_model == "aaaa"

        assert len({id(data) for data in seen}) == 3

    def test_parsed_data_is_a_private_deep_copy(self, config_file: Path) -> None:
        reference = _reference_safe_load(config_file)
        first = _parse_cache.safe_load_yaml_file(config_file)
        first["llm"]["backend"] = "mutated"
        first.clear()

        second = _parse_cache.safe_load_yaml_file(config_file)
        assert second == reference
        assert second is not first

    def test_yaml_aliases_stay_shared_within_one_result(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("x: &a {k: [1]}\ny: *a\n", encoding="utf-8")

        for _ in range(2):  # miss, then hit
            data = _parse_cache.safe_load_yaml_file(path)
            assert data == _reference_safe_load(path)
            assert data["x"] is data["y"]

    def test_concurrent_loads_agree(self, config_file: Path) -> None:
        reference = _reference_safe_load(config_file)
        results: list[Any] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(20):
                    results.append(_parse_cache.safe_load_yaml_file(config_file))
            except BaseException as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors
        assert len(results) == 160
        assert all(result == reference for result in results)
        assert len({id(result) for result in results}) == 160


def _pad(size: int) -> bytes:
    """A comment line that ends exactly at byte ``size`` (TextIOWrapper chunks are 8 KiB)."""
    return b"#" + b"x" * (size - 2) + b"\n"


_EDGE_CASES: dict[str, bytes] = {
    "valid": b"a: 1\nb: [1, 2]\n",
    "malformed": b"a: 1\nb: [1, 2\nc: 3\n",
    "bad_indent": b"a:\n  b: 1\n c: 2\n",
    "tab_indent": b"a:\n\tb: 1\n",
    "crlf_valid": b"a: 1\r\nb: |\r\n  x\r\n  y\r\n",
    "crlf_malformed": b"a: 1\r\nb: [1,\r\nc: : :\r\n",
    "cr_only_valid": b"a: 1\rb: |\r  x\r  y\r",
    "cr_only_malformed": b"a: 1\rb: {\rc",
    "bom_valid": b"\xef\xbb\xbfa: 1\n",
    "bom_malformed": b"\xef\xbb\xbfa: 1\nb: [\n",
    "bom_only": b"\xef\xbb\xbf",
    "bom_mid_document": b"a: 1\n\xef\xbb\xbfb: 2\n",
    "empty": b"",
    "whitespace": b"   \n\n",
    "comment_only": b"# nothing\n",
    "non_printable": b"a: 1\nb: x\x07y\n",
    "nul": b"a: \x00\n",
    "invalid_utf8": b"a: 1\xff\n",
    "utf16": "a: 1\n".encode("utf-16"),
    "multi_document": b"a: 1\n---\nb: 2\n",
    "recursive_alias": b"&a [*a]\n",
    "unknown_alias": b"y: *nope\n",
    "unsafe_tag": b"x: !!python/object:os.system ls\n",
    "set_binary_timestamp": (
        b"s: !!set {a, b}\nb: !!binary aGVsbG8=\nt: 2001-12-14t21:59:43.10-05:00\nd: 2002-12-14\n"
    ),
    "large_valid": b"".join(b"key%05d: value %d\n" % (i, i) for i in range(1500)),
    "large_crlf_tail_error": b"".join(b"k%05d: v\r\n" % i for i in range(1500)) + b"z: [\r\n",
}
for _boundary in (8191, 8192, 8193, 16384):
    _EDGE_CASES[f"invalid_utf8_after_{_boundary}"] = _pad(_boundary) + b"a: \xff\n"
    _EDGE_CASES[f"multibyte_split_at_{_boundary}"] = (
        b"#" + b"x" * (_boundary - 1) + "é€😀".encode() + b"\na: 1\n"
    )
    _EDGE_CASES[f"truncated_multibyte_at_{_boundary}"] = (
        b"#" + b"x" * (_boundary - 1) + b"\xe2\x82A\n"
    )
    _EDGE_CASES[f"crlf_split_at_{_boundary}"] = b"#" + b"x" * (_boundary - 1) + b"\r\na: [\r\n"
    _EDGE_CASES[f"non_printable_after_{_boundary}"] = _pad(_boundary) + b"\x08\n"


def _generated_cases(count: int) -> list[bytes]:
    rng = random.Random(2928)
    tokens = [
        b"a", b"b: 1", b":", b" ", b"  ", b"\n", b"\r\n", b"\r", b"-", b"- x", b"[", b"]",
        b"{", b"}", b"'", b'"', b"&x", b"*x", b"\t", b"#", b"!!str", b"---", b"...", b"|",
        b">", b"?", "é".encode(), "😀".encode(), b"\xef\xbb\xbf", b"\x07", b"\xff", b"\xc3",
    ]  # fmt: skip
    return [
        b"".join(rng.choice(tokens) for _ in range(rng.choice([5, 40, 400, 3000])))
        for _ in range(count)
    ]


class TestEquivalenceWithUncachedParse:
    @pytest.mark.parametrize("name", sorted(_EDGE_CASES))
    def test_edge_case_matches_reference(self, tmp_path: Path, name: str) -> None:
        path = tmp_path / "config.yaml"
        path.write_bytes(_EDGE_CASES[name])
        reference = _outcome(_reference_safe_load, path)

        assert _outcome(_parse_cache.safe_load_yaml_file, path) == reference  # miss
        assert _outcome(_parse_cache.safe_load_yaml_file, path) == reference  # hit

    def test_generated_inputs_match_reference(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        outcomes: set[str] = set()
        for data in _generated_cases(300):
            path.write_bytes(data)
            reference = _outcome(_reference_safe_load, path)
            outcomes.add(reference[0] if reference[0] == "ok" else reference[1])
            assert _outcome(_parse_cache.safe_load_yaml_file, path) == reference
            assert _outcome(_parse_cache.safe_load_yaml_file, path) == reference

        # The table must exercise successes and several failure kinds.
        assert {"ok", "UnicodeDecodeError", "ScannerError", "ReaderError"} <= outcomes

    @pytest.mark.parametrize(
        "data",
        [
            b"llm:\n  backend: [claude\nlogging: {}\n",
            b"llm:\r\n  backend: [claude\r\nlogging: {}\r\n",
            b"\xef\xbb\xbfllm:\n  backend: [claude\nlogging: {}\n",
            b"llm:\n  backend: claude\xff\n",
        ],
        ids=["lf", "crlf", "bom", "invalid-utf8"],
    )
    def test_load_config_parse_error_is_identical(self, tmp_path: Path, data: bytes) -> None:
        path = tmp_path / "config.yaml"
        path.write_bytes(data)
        expected = _config_error(_reference_read_yaml_file, path)

        for _ in range(2):  # a failed parse is never cached, so both calls parse
            assert _config_error(load_config, path) == expected
        # The numeric position survives the cache's in-memory text wrapper.
        if b"[" in data:
            assert expected[2] == {"yaml_error": "invalid_yaml", "line": 3, "column": 8}
        else:
            assert expected[2] == {"yaml_error": "invalid_utf8"}

    def test_relative_path_error_names_the_path_as_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        relative = Path("config.yaml")
        relative.write_bytes(b"a: [1\nb: 2\n")

        expected = _outcome(_reference_safe_load, relative)
        assert _outcome(_parse_cache.safe_load_yaml_file, relative) == expected
        assert 'in "config.yaml", line' in expected[2]

    def test_failed_parse_is_not_cached(self, config_file: Path) -> None:
        load_config(config_file)
        config_file.write_text("llm: [\n", encoding="utf-8")

        with _count_parses() as safe_load:
            for _ in range(2):
                with pytest.raises(ConfigError, match="Failed to parse configuration file"):
                    load_config(config_file)
        assert safe_load.call_count == 2
        assert _parse_cache._entries == {}

        config_file.write_text(_CONFIG, encoding="utf-8")
        assert load_config(config_file).llm.qa_model == "aaaa"


class TestFileAccessUnchanged:
    @pytest.mark.skipif(
        sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
        reason="needs POSIX permissions enforced for the current user",
    )
    def test_unreadable_file_raises_the_same_error_after_a_cached_parse(
        self, config_file: Path
    ) -> None:
        load_config(config_file)  # populate the cache first
        config_file.chmod(0)
        try:
            expected_raw = _outcome(_reference_safe_load, config_file)
            raw = _outcome(_parse_cache.safe_load_yaml_file, config_file)
            expected = _config_error(_reference_read_yaml_file, config_file)
            actual = _config_error(load_config, config_file)
        finally:
            config_file.chmod(0o600)

        assert expected_raw[1] == "PermissionError"
        assert raw == expected_raw
        assert actual == expected
        assert expected[2] == {"error_type": "PermissionError"}

    def test_directory_raises_the_same_error(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        path.mkdir()

        assert _outcome(_parse_cache.safe_load_yaml_file, path) == _outcome(
            _reference_safe_load, path
        )
        expected = _config_error(_reference_read_yaml_file, path)
        assert _config_error(load_config, path) == expected
        assert str(expected[1]).startswith("Failed to read configuration file")

    def test_deleted_file_is_reported_missing_despite_a_cached_parse(
        self, config_file: Path
    ) -> None:
        load_config(config_file)
        config_file.unlink()

        with pytest.raises(ConfigError, match="Configuration file not found"):
            load_config(config_file)


_SECRET = "sk-SYNTHETIC-PARSE-CACHE-SECRET"
# Invalid files whose raw parser error carries the secret (asserted below).
_SECRET_BEARING_INVALID: dict[str, bytes] = {
    "undefined-alias": f"llm:\n  api_key: *{_SECRET}\n".encode(),
    "unknown-tag": f"llm:\n  api_key: !{_SECRET} x\n".encode(),
    "invalid-utf8": f"llm:\n  api_key: {_SECRET}".encode() + b"\xff\n",
}


def _assert_redacted(error: ConfigError) -> None:
    """The checks #2968 applies to public configuration errors."""
    assert _SECRET not in str(error)
    assert _SECRET not in repr(error)
    assert _SECRET not in str(error.details)
    assert _SECRET not in str(error.config_key)
    assert _SECRET not in "".join(traceback.format_exception(error))
    assert error.__context__ is None
    assert error.__cause__ is None


class TestRedactionThroughCache:
    @pytest.mark.parametrize(
        "data", _SECRET_BEARING_INVALID.values(), ids=_SECRET_BEARING_INVALID.keys()
    )
    def test_secret_bearing_invalid_file_is_redacted_on_every_load(
        self, config_file: Path, data: bytes
    ) -> None:
        assert load_config(config_file).llm.qa_model == "aaaa"  # the cache holds a parse
        config_file.write_bytes(data)

        with pytest.raises((UnicodeDecodeError, yaml.YAMLError)) as raw:
            _reference_safe_load(config_file)
        assert _SECRET in str(raw.value) or _SECRET.encode() in getattr(raw.value, "object", b"")
        expected = _config_error(_reference_read_yaml_file, config_file)

        with _count_parses() as safe_load:
            for _ in range(2):  # first and second load of the same bad bytes
                with pytest.raises(ConfigError) as exc_info:
                    load_config(config_file)
                _assert_redacted(exc_info.value)
                assert _config_error(_raise(exc_info.value), config_file) == expected
        assert safe_load.call_count == 2  # the second load parsed again, nothing replayed
        assert _parse_cache._entries == {}  # neither the bad bytes nor an error is kept

        # A cache hit after the failure serves only the restored good parse ...
        config_file.write_text(_CONFIG, encoding="utf-8")
        with _count_parses() as safe_load:
            assert load_config(config_file).llm.qa_model == "aaaa"
            assert load_config(config_file).llm.qa_model == "aaaa"
        assert safe_load.call_count == 1
        # ... and the bad bytes are parsed and redacted again afterwards.
        config_file.write_bytes(data)
        with pytest.raises(ConfigError) as exc_info:
            load_config(config_file)
        _assert_redacted(exc_info.value)

    def test_credentials_are_never_cached(self, tmp_path: Path) -> None:
        path = tmp_path / "credentials.yaml"
        path.write_text(f"providers:\n  openai:\n    api_key: {_SECRET}\n", encoding="utf-8")

        with _count_parses() as safe_load:
            for _ in range(2):
                assert load_credentials(path).providers["openai"].api_key == _SECRET
        assert safe_load.call_count == 2
        assert _parse_cache._entries == {}

    def test_partial_getters_do_not_use_the_cache(self, config_file: Path) -> None:
        config_file.write_text("orchestrator:\n  max_parallel_workers: 5\n", encoding="utf-8")

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.loader.get_config_dir", return_value=config_file.parent),
        ):
            assert get_max_parallel_workers() == 5
        assert _parse_cache._entries == {}


def _raise(error: BaseException) -> Callable[[Path], Any]:
    def fn(_path: Path) -> Any:
        raise error

    return fn


class TestBoundedMemory:
    def test_keeps_one_entry_per_path_for_a_few_paths(self, tmp_path: Path) -> None:
        paths = []
        for index in range(_parse_cache._MAX_ENTRIES + 2):
            path = tmp_path / f"c{index}" / "config.yaml"
            path.parent.mkdir()
            path.write_text(_CONFIG, encoding="utf-8")
            paths.append(path)
            load_config(path)

        retained = list(_parse_cache._entries)
        assert retained == [os.path.realpath(p) for p in paths[-_parse_cache._MAX_ENTRIES :]]

        for value in ("bbbb", "cccc", "dddd"):
            paths[-1].write_text(_CONFIG.replace("aaaa", value), encoding="utf-8")
            assert load_config(paths[-1]).llm.qa_model == value
        assert len(_parse_cache._entries) == _parse_cache._MAX_ENTRIES
        assert _parse_cache._entries[os.path.realpath(paths[-1])][0] == paths[-1].read_bytes()

    def test_oversized_file_is_never_retained(
        self, config_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        load_config(config_file)
        assert os.path.realpath(config_file) in _parse_cache._entries

        monkeypatch.setattr(_parse_cache, "_MAX_CACHED_BYTES", len(_CONFIG) - 1)
        config_file.write_text(_CONFIG.replace("aaaa", "bbbb"), encoding="utf-8")
        with _count_parses() as safe_load:
            for _ in range(2):
                assert load_config(config_file).llm.qa_model == "bbbb"
        assert safe_load.call_count == 2
        assert _parse_cache._entries == {}
