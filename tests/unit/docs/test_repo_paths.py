"""Maintained docs must not name repository paths that do not exist.

A doc that links to a moved file, or tells the reader to open
``src/ouroboros/...`` after a refactor, sends a user or an agent to nothing.
This check resolves every repository path a maintained doc names and fails
with one ``path:line: <token> — <remedy>`` line per dead reference. Policy:
``docs/contributing/docs-freshness.md``. Which docs count: ``_corpus.py``.

What is checked on each corpus line:

1. **Link targets** in prose: Markdown links and images (``[text](target)``,
   ``![alt](target)``), reference definitions (``[id]: target``), and the
   ``href`` / ``src`` attribute of an inline HTML tag. Skipped: targets with a
   URL scheme (``https:``, ``mailto:``, ...), protocol-relative ``//host``
   targets, and pure ``#anchor`` targets; ``<https://...>`` autolinks are not
   link targets at all. An angle-bracketed destination
   (``[text](<a file.md>)``) is unwrapped and checked like any other. The
   ``#fragment``, a ``?query``, and a trailing ``:123`` / ``:123-130`` line
   suffix (hyphen or en dash) are dropped and the rest is percent-decoded. A
   relative target is resolved against the doc's own directory; a target
   starting with ``/`` is resolved against the repository root, which is how
   GitHub renders it. The result must exist and stay inside the repository.
   A local absolute filesystem path (``/Users/...``, ``/home/...``, a Windows
   drive path, or a ``file:`` URL) is reported as such: it is never valid in a
   repository doc.
   Backticked text is literal, so a link inside a code span is not checked.
2. **Inline code** in prose: a backticked token that starts with a top-level
   repository directory (``src/``, ``tests/``, ``docs/``, ``.github/``, ...)
   is resolved against the repository root and must exist. Tokens that are
   patterns or placeholders are skipped: those containing ``*``, ``?``,
   ``<``, ``>``, ``{``, ``}``, ``$``, ``...``, ``…``, or whitespace. A
   ``::Symbol`` suffix, a ``#fragment``, and a trailing ``:123`` /
   ``:123-130`` / ``:123–130`` line suffix are dropped first.
3. **Fenced code blocks**: examples there often show the *reader's* project
   (``src/app.py``, ``tests/test_api.py``), which must not be resolved against
   this repository. A path in a fence is checked only when its first segment
   is a top-level repository directory **and** its second segment is a real
   child of that directory (``src/ouroboros/...``, ``tests/unit/...``,
   ``docs/guides/...``), and only when it starts a token rather than
   continuing another path (``/home/me/project/src/ouroboros/x.py`` is not
   checked). The placeholder and suffix rules of inline code apply. Links in a
   fence are literal text, so they are not checked.

The top-level directories are read from the checkout, minus
:data:`NON_REPOSITORY_DIRS`: version control, virtualenv, cache, and build
output, plus the client and runtime state directories (``.ouroboros``,
``.claude``, ``.codex``, ``.agents``) that the docs describe in the reader's
own project or home directory.

Existence is compared case-sensitively on every platform, so a path that only
resolves on a case-insensitive filesystem (the macOS default) fails here the
same way it fails on the Linux CI runner and on GitHub.

Whether a ``#fragment`` names a real heading is not checked.

A line that names a nonexistent path on purpose (a hypothetical example, a
recorded historical finding) takes ``<!-- docs-drift: ignore -->``. Inside a
fenced block that comment would render, so bracket the fence with
``<!-- docs-drift: ignore-start -->`` / ``<!-- docs-drift: ignore-end -->``.
"""

from __future__ import annotations

from collections.abc import Iterable
from functools import cache
import os
import posixpath
import re
from urllib.parse import unquote

from tests.unit.docs._corpus import REPO_ROOT, DocLine, corpus_files, iter_lines

#: Children of the repository root that are not repository content.
NON_REPOSITORY_DIRS: frozenset[str] = frozenset(
    {
        # Version control, environments, caches, build output.
        ".git",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "target",
        "htmlcov",
        # Client and runtime state that the docs describe in the reader's own
        # project or home directory (``.ouroboros/seeds/``,
        # ``.claude/settings.json``), not files of this repository.
        ".ouroboros",
        ".claude",
        ".codex",
        ".agents",
    }
)

REMEDY_MISSING_LINK = (
    "link target does not exist; point it at the file's current location, or remove the link"
)
REMEDY_OUTSIDE_REPO = (
    "link target resolves outside the repository; link a file inside this repository"
)
REMEDY_ABSOLUTE = (
    "absolute filesystem path is never valid in a repository doc; use a path relative to this doc"
)
REMEDY_MISSING_PATH = (
    "repository path does not exist; update it to the current location, or add "
    "`<!-- docs-drift: ignore -->` to the line if it is deliberately hypothetical"
)

# Inline code span: a run of backticks, the content, the same run again.
_CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
# ``[text](target)`` and ``![alt](target)``, with an optional quoted title. The
# text may hold one level of brackets, and the lookahead lets matches overlap,
# so ``[![badge](img.png)](page.md)`` yields both ``img.png`` and ``page.md``.
_INLINE_LINK = re.compile(
    r"(?=!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]"
    r"\(\s*(<[^>]*>|[^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\))"
)
# ``[id]: target`` at the start of a line; ``[^note]:`` is a footnote, not a link.
_REFERENCE_DEF = re.compile(r"^ {0,3}\[(?!\^)[^\]]+\]:\s*(<[^>]*>|\S+)")
# The first ``href="..."`` / ``src='...'`` of an HTML tag.
_HTML_ATTR = re.compile(r"<[A-Za-z][^>]*?\b(?:href|src)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_LINE_SUFFIX = re.compile(r":\d+(?:[-–]\d+)?$")
_PLACEHOLDER = re.compile(r"[*?<>{}$\s]|\.\.\.|…")
# A fenced path ends at whitespace, a quote, a bracket, or a shell separator.
_FENCE_TOKEN_BODY = r"[^\s'\"`()\[\],;|&]*"
# A top-level directory name right after one of these continues another path
# or word (``/home/me/src/...``, ``~/docs``, ``my-src/``), so it is not checked.
_FENCE_TOKEN_START = r"(?<![\w./~$@:-])"


def top_level_dirs() -> frozenset[str]:
    """Names of the repository's top-level directories, read from the checkout."""
    with os.scandir(REPO_ROOT) as entries:
        return frozenset(
            entry.name
            for entry in entries
            if entry.is_dir() and entry.name not in NON_REPOSITORY_DIRS
        )


@cache
def _entries(directory: str) -> frozenset[str]:
    try:
        return frozenset(os.listdir(directory))
    except (FileNotFoundError, NotADirectoryError):
        return frozenset()


def _parts(rel_path: str) -> list[str]:
    return [part for part in rel_path.split("/") if part not in ("", ".")]


def exists_in_repo(rel_path: str) -> bool:
    """Whether a repository-relative POSIX path exists, matching case exactly."""
    current = str(REPO_ROOT)
    for part in _parts(rel_path):
        if part not in _entries(current):
            return False
        current = os.path.join(current, part)
    return True


def resolve_link(doc_path: str, target: str) -> str | None:
    """Resolve a link target as GitHub does for a link in ``doc_path``.

    Returns the repository-relative path, or ``None`` when the target escapes
    the repository root.
    """
    if target.startswith("/"):
        joined = target.lstrip("/")
    else:
        joined = posixpath.join(posixpath.dirname(doc_path), target)
    normalised = posixpath.normpath(joined) if joined else "."
    if normalised == ".." or normalised.startswith("../"):
        return None
    return "" if normalised == "." else normalised


def _strip_code_spans(text: str) -> str:
    return _CODE_SPAN.sub("``", text)


def link_targets(text: str) -> list[str]:
    """Every link target on a prose line."""
    prose = _strip_code_spans(text)
    # ``![alt](x)`` also matches from its ``[``; keep each target once.
    starts: dict[int, str] = {}
    for match in _INLINE_LINK.finditer(prose):
        starts.setdefault(match.start(1), match.group(1))
    targets = list(starts.values())
    reference = _REFERENCE_DEF.match(prose)
    if reference:
        targets.append(reference.group(1))
    targets.extend(match.group(1) or match.group(2) or "" for match in _HTML_ATTR.finditer(prose))
    return targets


def check_link_target(doc_path: str, target: str) -> str | None:
    """The remedy for a dead link target; ``None`` when it resolves or is not checked."""
    target = target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()
    if not target or target.startswith(("#", "//")):
        return None
    if target.lower().startswith("file:") or _WINDOWS_DRIVE.match(target):
        return REMEDY_ABSOLUTE
    if _URL_SCHEME.match(target):
        return None
    path = target.split("#", 1)[0].split("?", 1)[0]
    path = _LINE_SUFFIX.sub("", unquote(path))
    if not path:
        return None
    resolved = resolve_link(doc_path, path)
    if resolved is not None and exists_in_repo(resolved):
        return None
    if path.startswith("/"):
        # ``/docs/x.md`` is a repository-root link; ``/Users/me/x.md`` is not.
        first = _parts(path)[0] if _parts(path) else ""
        return REMEDY_MISSING_LINK if first in _entries(str(REPO_ROOT)) else REMEDY_ABSOLUTE
    return REMEDY_OUTSIDE_REPO if resolved is None else REMEDY_MISSING_LINK


def repo_path_of(token: str, dirs: frozenset[str]) -> str | None:
    """The repository-relative path a code token names, or ``None`` if it names none.

    ``None`` covers tokens that do not start with a top-level directory and
    tokens that are patterns or placeholders.
    """
    token = token.strip()
    if token.startswith("./"):
        token = token[2:]
    first, sep, _ = token.partition("/")
    if not sep or first not in dirs or _PLACEHOLDER.search(token):
        return None
    token = token.split("::", 1)[0].split("#", 1)[0].rstrip(".,:;")
    rel = posixpath.normpath(_LINE_SUFFIX.sub("", token))
    return None if rel == ".." or rel.startswith("../") else rel


def inline_code_tokens(text: str) -> list[str]:
    return [match.group(2) for match in _CODE_SPAN.finditer(text)]


def fenced_path_tokens(text: str, dirs: frozenset[str]) -> list[str]:
    """Tokens on a fenced line that start with a top-level directory."""
    if not dirs:
        return []
    names = "|".join(re.escape(name) for name in sorted(dirs, key=len, reverse=True))
    pattern = re.compile(rf"{_FENCE_TOKEN_START}(?:\./)?(?:{names})/{_FENCE_TOKEN_BODY}")
    return [match.group(0) for match in pattern.finditer(text)]


def _second_segment_is_real(rel_path: str) -> bool:
    parts = _parts(rel_path)
    return len(parts) >= 2 and parts[1] in _entries(os.path.join(REPO_ROOT, parts[0]))


def check_line(line: DocLine, dirs: frozenset[str]) -> list[str]:
    """Every violation on one corpus line, formatted ``path:line: token — remedy``."""
    found: list[tuple[str, str]] = []
    if line.in_code_block:
        for token in fenced_path_tokens(line.text, dirs):
            rel = repo_path_of(token, dirs)
            if rel and _second_segment_is_real(rel) and not exists_in_repo(rel):
                found.append((token, REMEDY_MISSING_PATH))
    else:
        for target in link_targets(line.text):
            remedy = check_link_target(line.path, target)
            if remedy:
                found.append((target, remedy))
        for token in inline_code_tokens(line.text):
            rel = repo_path_of(token, dirs)
            if rel and not exists_in_repo(rel):
                found.append((token, REMEDY_MISSING_PATH))
    return [f"{line.where()}: {token} — {remedy}" for token, remedy in found]


def find_violations(lines: Iterable[DocLine], dirs: frozenset[str] | None = None) -> list[str]:
    """Every violation in ``lines``; ``dirs`` defaults to :func:`top_level_dirs`."""
    dirs = top_level_dirs() if dirs is None else dirs
    return [violation for line in lines for violation in check_line(line, dirs)]


# --- the check ---------------------------------------------------------------


def test_corpus_and_top_level_dirs_are_not_empty() -> None:
    assert corpus_files(), "the docs corpus is empty; check CORPUS_GLOBS in _corpus.py"
    assert {"src", "tests", "docs"} <= top_level_dirs()


def test_maintained_docs_name_only_existing_repository_paths() -> None:
    violations = find_violations(iter_lines())
    assert not violations, (
        f"{len(violations)} dead repository path(s) in maintained docs "
        "(policy: docs/contributing/docs-freshness.md):\n" + "\n".join(violations)
    )


# --- the checker cannot silently pass ----------------------------------------

DIRS = frozenset({"src", "tests", "docs", ".github"})


def _line(text: str, *, path: str = "docs/guides/example.md", fenced: bool = False) -> DocLine:
    return DocLine(path=path, lineno=7, text=text, in_code_block=fenced)


def test_reports_a_dead_relative_link_with_location_token_and_remedy() -> None:
    line = _line("See [the loop](../../src/ouroboros/no_such_module.py).")
    assert find_violations([line], DIRS) == [
        f"docs/guides/example.md:7: ../../src/ouroboros/no_such_module.py — {REMEDY_MISSING_LINK}"
    ]


def test_reports_a_dead_inline_code_path() -> None:
    line = _line("Edit `src/ouroboros/orchestrator/evolution/loop.py:42`.")
    assert find_violations([line], DIRS) == [
        "docs/guides/example.md:7: src/ouroboros/orchestrator/evolution/loop.py:42 — "
        f"{REMEDY_MISSING_PATH}"
    ]


def test_reports_every_violation_instead_of_stopping_at_the_first() -> None:
    lines = [
        _line("[a](missing-a.md) and `docs/missing-b.md`"),
        _line("![img](images/missing-c.png)", path="docs/README.md"),
        _line("[ref]: ./missing-d.md"),
        _line('<img width="9" src="missing-e.png"> <a href="../missing-f.md">f</a>'),
        _line("[![badge](missing-g.svg)](missing-h.md)"),
        _line("[spaced](<missing i.md>)"),
    ]
    tokens = [
        violation.split(": ", 1)[1].split(" — ")[0] for violation in find_violations(lines, DIRS)
    ]
    assert tokens == [
        "missing-a.md",
        "docs/missing-b.md",
        "images/missing-c.png",
        "./missing-d.md",
        "missing-e.png",
        "../missing-f.md",
        "missing-h.md",
        "missing-g.svg",
        "<missing i.md>",
    ]


def test_accepts_existing_targets_in_every_supported_form() -> None:
    lines = [
        _line("[up](../README.md#documentation) [dir](../contributing/) [here](./)"),
        _line("[root](/docs/README.md) [line](../../src/ouroboros/__init__.py:1)"),
        _line('[title](../README.md "Docs index") [encoded](../%52EADME.md)'),
        _line('<img src="../images/ouroboros.png"> <a href="../README.md">x</a>'),
        _line("[docs]: ../README.md"),
        _line("[^1]: Footnote text, not a link target."),
        _line("`docs/../README.md` `docs/contributing/../README.md`"),
        _line("`src/ouroboros/` `tests/conftest.py::isolate_ouroboros_home`"),
        _line("`src/ouroboros/__init__.py:1-3` `docs/README.md#index` `./docs/README.md`"),
        _line("`src/ouroboros/__init__.py:1–3` (an en-dash range)"),
        _line("`.github/workflows/`"),
        _line("pytest tests/unit/docs/::x 'src/ouroboros/__init__.py:1'.", fenced=True),
    ]
    assert find_violations(lines, DIRS) == []


def test_skips_urls_anchors_autolinks_and_non_repository_tokens() -> None:
    lines = [
        _line("[a](https://example.com/missing) [b](mailto:x@example.com) [c](#heading)"),
        _line("[d](<../README.md>) [e](//cdn.example.com/x.png) <https://example.com>"),
        _line("`config/models.py` `~/.ouroboros/seeds/` `ouroboros run` `app/main.py`"),
    ]
    assert find_violations(lines, DIRS) == []


def test_skips_patterns_and_placeholders() -> None:
    tokens = [
        "src/ouroboros/*.py",
        "src/ouroboros/mod?.py",
        "src/ouroboros/<module>.py",
        "src/ouroboros/{a,b}.py",
        "src/$PKG/x.py",
        "src/ouroboros/.../x.py",
        "src/ouroboros/…/x.py",
        "src/ouroboros/missing.py extra",
    ]
    lines = [_line(f"`{token}`") for token in tokens]
    lines += [_line(token, fenced=True) for token in tokens[:-1]]
    assert find_violations(lines, DIRS) == []


def test_reports_absolute_filesystem_paths_in_link_targets() -> None:
    lines = [
        _line("[a](/Users/someone/ouroboros/README.md)"),
        _line("[b](/home/someone/ouroboros/README.md)"),
        _line("[c](file:///Users/someone/ouroboros/README.md)"),
        _line("[d](C:\\Users\\someone\\README.md)"),
    ]
    violations = find_violations(lines, DIRS)
    assert len(violations) == 4
    assert all(violation.endswith(REMEDY_ABSOLUTE) for violation in violations)


def test_reports_a_dead_repository_root_link_as_missing_not_absolute() -> None:
    assert find_violations([_line("[x](/docs/no-such-page.md)")], DIRS) == [
        f"docs/guides/example.md:7: /docs/no-such-page.md — {REMEDY_MISSING_LINK}"
    ]


def test_reports_a_link_that_escapes_the_repository() -> None:
    assert find_violations([_line("[x](../../../outside.md)")], DIRS) == [
        f"docs/guides/example.md:7: ../../../outside.md — {REMEDY_OUTSIDE_REPO}"
    ]


def test_case_mismatch_is_reported_on_every_platform() -> None:
    violations = find_violations([_line("[x](../README.MD) `docs/readme.md`")], DIRS)
    assert [violation.split(" — ")[0] for violation in violations] == [
        "docs/guides/example.md:7: ../README.MD",
        "docs/guides/example.md:7: docs/readme.md",
    ]


def test_links_that_are_literal_text_are_not_checked() -> None:
    lines = [
        _line("Write `[label](missing.md)` to link a file."),
        _line("[label](missing.md)", fenced=True),
    ]
    assert find_violations(lines, DIRS) == []


def test_fenced_paths_are_checked_only_under_a_real_second_segment() -> None:
    lines = [
        _line("uv run pytest tests/unit/no_such_test.py -q", fenced=True),
        _line('python "src/ouroboros/no_such_module.py"', fenced=True),
        # The reader's project, not this repository.
        _line("cat src/app.py tests/test_api.py docs/notes.md", fenced=True),
        # Part of another path, not a repository path.
        _line("cd /home/me/project/src/ouroboros/missing.py ~/docs/guides/x", fenced=True),
    ]
    assert [violation.split(" — ")[0] for violation in find_violations(lines, DIRS)] == [
        "docs/guides/example.md:7: tests/unit/no_such_test.py",
        "docs/guides/example.md:7: src/ouroboros/no_such_module.py",
    ]


def test_runtime_state_dirs_are_not_checked_but_repository_dot_dirs_are() -> None:
    # ``.ouroboros/`` and ``.claude/`` name the reader's own project state.
    assert find_violations([_line("`.ouroboros/seeds/no-such.yaml` `.claude/no-such.json`")]) == []
    assert find_violations([_line("`.github/workflows/no-such.yml`")]) == [
        f"docs/guides/example.md:7: .github/workflows/no-such.yml — {REMEDY_MISSING_PATH}"
    ]
