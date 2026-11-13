"""Maintained docs must not name repository paths that do not exist.

A doc that links to a moved file, or tells the reader to open
``src/ouroboros/...`` after a refactor, sends a user or an agent to nothing.
This check resolves every repository path a maintained doc names and fails
with one ``path:line: <token> — <remedy>`` line per dead reference. Policy:
``docs/contributing/docs-freshness.md``. Which docs count: ``_corpus.py``.

**What counts as a link is decided by a CommonMark parser, not by patterns.**
Each doc is parsed with ``markdown-it-py`` (the parser ``rich`` already
depends on) with GFM tables on. ``_corpus.iter_lines`` decides only which
lines count: every line it does not yield (an ignore marker, an ignored
region, a fence) is blank in the parsed text, so no token can come from it.
Inline tokens carry no positions, so the parser's own link, image, autolink,
code-span, and raw-HTML rules are wrapped to record where each token starts;
its line is the enclosing block's first line plus the newlines before it.

What is checked:

1. **Link targets**: the ``href`` of every link and the ``src`` of every image
   the parser produces, including links that span lines; reference
   definitions (``[id]: target``) at their own line, used or not, while the
   links that use them are not checked a second time. ``[^note]:`` is a GFM
   footnote, not a definition. Raw HTML (inline tags and HTML blocks) goes
   to the standard library's ``html.parser``, which yields every ``href`` /
   ``src`` of every tag. An HTML block that ends inside an unclosed tag is
   reported, together with the targets that tag already has.

   **URL rule.** A link target is a URL if, after removing a trailing ``:N``
   or ``:N-M`` line suffix (digits only), it starts with an RFC 3986 scheme:
   ``^[A-Za-z][A-Za-z0-9+.-]*:``. Exception: a single letter followed by
   ``:\\`` or ``:/`` is a Windows drive path, which counts as an absolute
   filesystem path and is reported. (The rule is applied after the
   ``#fragment`` and ``?query`` are dropped and the target is percent-decoded;
   as everywhere in this check, the ``-`` of ``:N-M`` may be an en dash.) A
   URL is not checked, except that a ``file:`` URL is reported as an absolute
   filesystem path. Protocol-relative ``//host`` targets and pure ``#anchor``
   targets are not checked either.

   Every other target is a path, checked without its line suffix. A
   relative target is resolved against the doc's own directory; a target
   starting with ``/`` is resolved against the repository root, which is how
   GitHub renders it. The result must exist and stay inside the repository.
   A local absolute filesystem path (``/Users/...``, ``/home/...``, a Windows
   drive path, or a ``file:`` URL) is reported as such: it is never valid in a
   repository doc. Text the parser does not turn into a link (an escaped
   ``\\[``, a bare ``](``, an unclosed ``[x](y``) is not a link and is not
   checked.
2. **Code spans**: a code span whose content starts with a top-level
   repository directory (``src/``, ``tests/``, ``docs/``, ``.github/``, ...)
   is resolved against the repository root and must exist. Tokens that are
   patterns or placeholders are skipped: those containing ``*``, ``?``,
   ``<``, ``>``, ``{``, ``}``, ``$``, ``...``, ``…``, or whitespace. A
   ``::Symbol`` suffix, a ``#fragment``, and a trailing ``:123`` /
   ``:123-130`` / ``:123–130`` line suffix are dropped first.
3. **Code blocks** (fenced lines from the corpus reader, and the fenced or
   indented code blocks the parser finds, such as a fence in a block quote):
   examples there often show the *reader's* project (``src/app.py``,
   ``tests/test_api.py``), which must not be resolved against this
   repository. A path there is checked only when its first segment is a
   top-level repository directory **and** its second segment is a real child
   of that directory (``src/ouroboros/...``, ``tests/unit/...``,
   ``docs/guides/...``), and only when it starts a token rather than
   continuing another path (``/home/me/project/src/ouroboros/x.py`` is not
   checked). The placeholder and suffix rules of code spans apply. Links in
   a code block are literal text, so they are not checked.

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

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from functools import cache
from html.parser import HTMLParser
import os
import posixpath
import re
from typing import Any
from urllib.parse import unquote

from markdown_it import MarkdownIt, rules_inline
from markdown_it.rules_inline import StateInline
from markdown_it.token import Token
import pytest

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
REMEDY_OPEN_TAG = (
    "HTML tag is not closed before the HTML block ends, so its href/src cannot be "
    "checked; close the tag"
)
REMEDY_MISSING_PATH = (
    "repository path does not exist; update it to the current location, or add "
    "`<!-- docs-drift: ignore -->` to the line if it is deliberately hypothetical"
)

_LINK_ATTRIBUTES = frozenset({"href", "src"})
# A URL scheme. ``.`` is left out on purpose: ``missing.md:x`` names a file.
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
_LINE_SUFFIX = re.compile(r":\d+(?:[-–]\d+)?$")
_PLACEHOLDER = re.compile(r"[*?<>{}$\s]|\.\.\.|…")
# A fenced path ends at whitespace, a quote, a bracket, or a shell separator.
_FENCE_TOKEN_BODY = r"[^\s'\"`()\[\],;|&]*"
# A top-level directory name right after one of these continues another path
# or word (``/home/me/src/...``, ``~/docs``, ``my-src/``), so it is not checked.
_FENCE_TOKEN_START = r"(?<![\w./~$@:-])"


# --- the parser --------------------------------------------------------------

#: Inline token types whose start offset is recorded.
_POSITIONED = frozenset({"code_inline", "link_open", "image", "html_inline"})


def _recording_offsets(
    rule: Callable[[StateInline, bool], bool],
) -> Callable[[StateInline, bool], bool]:
    """Wrap an inline rule so each token it creates records where it starts.

    ``link`` and ``image`` also record whether the source was the inline form
    ``[text](target)``, which ends with ``)``; a reference form ends with ``]``
    and is checked at its definition instead.
    """

    def wrapped(state: StateInline, silent: bool) -> bool:
        start, first_new = state.pos, len(state.tokens)
        if not rule(state, silent):
            return False
        inline_form = state.src[state.pos - 1] in ")>"
        for token in state.tokens[first_new:]:
            if token.type in _POSITIONED and "offset" not in token.meta:
                token.meta["offset"] = start
                token.meta["inline_form"] = inline_form
        return True

    return wrapped


_MARKDOWN = MarkdownIt("commonmark", {"inline_definitions": True}).enable("table")
# Rendering would drop ``file:`` links as unsafe; this check reports them.
_MARKDOWN.validateLink = lambda _url: True  # type: ignore[method-assign]
for _name, _rule in (
    ("backticks", rules_inline.backtick),
    ("link", rules_inline.link),
    ("image", rules_inline.image),
    ("autolink", rules_inline.autolink),
    ("html_inline", rules_inline.html_inline),
):
    _MARKDOWN.inline.ruler.at(_name, _recording_offsets(_rule))


class _TagLinks(HTMLParser):
    """Every ``href`` / ``src`` of every tag, with the line it starts on."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        line = self.getpos()[0]
        self.values.extend((line, value or "") for name, value in attrs if name in _LINK_ATTRIBUTES)


def html_links(html: str) -> tuple[list[tuple[int, str]], tuple[int, str] | None]:
    """Every ``href`` / ``src`` in ``html`` by 1-based line, and a tag left open at its end.

    ``HTMLParser`` leaves an incomplete construct unconsumed in its buffer until
    more input arrives, so a remainder that opens a start tag is a tag that
    never closed. The attributes it has so far are read by parsing it again
    with a closing ``>``.
    """
    parser = _TagLinks()
    parser.feed(html)
    pending = parser.rawdata
    if not (pending[:1] == "<" and pending[1:2].isascii() and pending[1:2].isalpha()):
        return parser.values, None
    line = parser.getpos()[0]
    closed = _TagLinks()
    closed.feed(pending + ">")
    closed.close()
    values = parser.values + [(line + offset - 1, value) for offset, value in closed.values]
    return values, (line, pending.strip())


@dataclass(frozen=True)
class Reference:
    """One thing a doc names that must exist in the repository."""

    lineno: int
    kind: str  # "link", "code" (a code span), "block" (a code-block line), "open-tag"
    text: str


def _html_references(html: str, first_line: int) -> Iterator[Reference]:
    values, open_tag = html_links(html)
    for line, value in values:
        yield Reference(first_line + line - 1, "link", value)
    if open_tag is not None:
        yield Reference(first_line + open_tag[0] - 1, "open-tag", open_tag[1])


def _inline_references(inline: Token, first_line: int) -> Iterator[Reference]:
    for child in inline.children or ():
        if child.type not in _POSITIONED:
            continue
        lineno = first_line + inline.content[: child.meta["offset"]].count("\n")
        if child.type == "code_inline":
            yield Reference(lineno, "code", child.content)
        elif child.type == "html_inline":
            yield from _html_references(child.content, lineno)
        elif child.meta["inline_form"]:
            attr = "href" if child.type == "link_open" else "src"
            yield Reference(lineno, "link", str(child.attrs.get(attr, "")))


def references(lines: Iterable[DocLine]) -> list[Reference]:
    """Every link target, code span, and code-block line in one doc's ``lines``, by line."""
    lines = list(lines)
    paths = {line.path for line in lines}
    if len(paths) > 1:
        raise ValueError(f"references() takes one doc at a time, got {sorted(paths)}")
    prose = {line.lineno: line.text for line in lines if not line.in_code_block}
    found = [Reference(line.lineno, "block", line.text) for line in lines if line.in_code_block]
    text = "\n".join(prose.get(n, "") for n in range(1, max(prose, default=0) + 1))
    env: dict[str, Any] = {}
    for token in _MARKDOWN.parse(text, env):
        if token.map is None:
            continue
        first_line = token.map[0] + 1
        if token.type == "inline":
            found.extend(_inline_references(token, first_line))
        elif token.type == "definition" and not token.meta["label"].startswith("^"):
            found.append(Reference(first_line, "link", token.meta["url"]))
        elif token.type == "html_block":
            found.extend(_html_references(token.content, first_line))
        elif token.type in ("fence", "code_block"):
            body = first_line + (1 if token.type == "fence" else 0)
            found.extend(
                Reference(body + k, "block", line)
                for k, line in enumerate(token.content.splitlines())
            )
    return sorted(found, key=lambda reference: reference.lineno)


# --- resolving ----------------------------------------------------------------


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


def check_link_target(doc_path: str, target: str) -> str | None:
    """The remedy for a dead link target; ``None`` when it resolves or is not checked.

    ``target`` may be percent-encoded, as the Markdown parser normalises it.
    """
    target = target.strip()
    if not target or target.startswith(("#", "//")):
        return None
    # The URL rule in the module docstring: drop the line suffix, then a
    # drive letter is an absolute path and any other scheme is a URL.
    path = target.split("#", 1)[0].split("?", 1)[0]
    path = _LINE_SUFFIX.sub("", unquote(path))
    if _WINDOWS_DRIVE.match(path) or path.lower().startswith("file:"):
        return REMEDY_ABSOLUTE
    if not path or _URL_SCHEME.match(path):
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


def fenced_path_tokens(text: str, dirs: frozenset[str]) -> list[str]:
    """Tokens on a code-block line that start with a top-level directory."""
    if not dirs:
        return []
    names = "|".join(re.escape(name) for name in sorted(dirs, key=len, reverse=True))
    pattern = re.compile(rf"{_FENCE_TOKEN_START}(?:\./)?(?:{names})/{_FENCE_TOKEN_BODY}")
    return [match.group(0) for match in pattern.finditer(text)]


def _second_segment_is_real(rel_path: str) -> bool:
    parts = _parts(rel_path)
    return len(parts) >= 2 and parts[1] in _entries(os.path.join(REPO_ROOT, parts[0]))


def _violations(reference: Reference, path: str, dirs: frozenset[str]) -> Iterator[str]:
    where = f"{path}:{reference.lineno}"
    if reference.kind == "link":
        remedy = check_link_target(path, reference.text)
        if remedy:
            yield f"{where}: {unquote(reference.text)} — {remedy}"
    elif reference.kind == "open-tag":
        yield f"{where}: {reference.text} — {REMEDY_OPEN_TAG}"
    elif reference.kind == "code":
        rel = repo_path_of(reference.text, dirs)
        if rel and not exists_in_repo(rel):
            yield f"{where}: {reference.text} — {REMEDY_MISSING_PATH}"
    else:
        for token in fenced_path_tokens(reference.text, dirs):
            rel = repo_path_of(token, dirs)
            if rel and _second_segment_is_real(rel) and not exists_in_repo(rel):
                yield f"{where}: {token} — {REMEDY_MISSING_PATH}"


def find_violations(lines: Iterable[DocLine], dirs: frozenset[str] | None = None) -> list[str]:
    """Every violation in ``lines``, doc by doc; ``dirs`` defaults to :func:`top_level_dirs`."""
    dirs = top_level_dirs() if dirs is None else dirs
    by_doc: dict[str, list[DocLine]] = {}
    for line in lines:
        by_doc.setdefault(line.path, []).append(line)
    return [
        violation
        for path, doc_lines in by_doc.items()
        for reference in references(doc_lines)
        for violation in _violations(reference, path, dirs)
    ]


# --- the check ---------------------------------------------------------------


def test_corpus_and_top_level_dirs_are_not_empty() -> None:
    assert corpus_files(), "the docs corpus is empty; check CORPUS_GLOBS in _corpus.py"
    assert {"src", "tests", "docs"} <= top_level_dirs()


def test_maintained_docs_name_only_existing_repository_paths() -> None:
    violations = find_violations(iter_lines())
    assert not violations, (
        f"{len(violations)} dead repository path(s) in maintained docs "
        "(see https://github.com/Q00/ouroboros/issues/2921):\n" + "\n".join(violations)
    )


# --- the checker cannot silently pass ----------------------------------------

DIRS = frozenset({"src", "tests", "docs", ".github"})
DOC = "docs/guides/example.md"


def _doc(*texts: str, fenced: bool = False, path: str = DOC) -> list[DocLine]:
    """One synthetic doc: each text on its own line, with a blank line between."""
    return [
        DocLine(path=path, lineno=2 * index + 1, text=text, in_code_block=fenced)
        for index, text in enumerate(texts)
    ]


def _tokens(lines: list[DocLine]) -> list[str]:
    return [v.split(": ", 1)[1].split(" — ")[0] for v in find_violations(lines, DIRS)]


def test_reports_a_dead_relative_link_with_location_token_and_remedy() -> None:
    lines = _doc("See [the loop](../../src/ouroboros/no_such_module.py).")
    assert find_violations(lines, DIRS) == [
        f"{DOC}:1: ../../src/ouroboros/no_such_module.py — {REMEDY_MISSING_LINK}"
    ]


def test_reports_a_dead_inline_code_path() -> None:
    lines = _doc("Edit `src/ouroboros/orchestrator/evolution/loop.py:42`.")
    assert find_violations(lines, DIRS) == [
        f"{DOC}:1: src/ouroboros/orchestrator/evolution/loop.py:42 — {REMEDY_MISSING_PATH}"
    ]


def test_reports_every_violation_instead_of_stopping_at_the_first() -> None:
    lines = _doc(
        "[a](missing-a.md) and `docs/missing-b.md`",
        "[ref]: ./missing-d.md",
        '<img width="9" src="missing-e.png"> <a href="../missing-f.md">f</a>',
        "[![badge](missing-g.svg)](missing-h.md)",
        "[spaced](<missing i.md>)",
    )
    lines += _doc("![img](images/missing-c.png)", path="docs/README.md")
    assert _tokens(lines) == [
        "missing-a.md",
        "docs/missing-b.md",
        "./missing-d.md",
        "missing-e.png",
        "../missing-f.md",
        "missing-h.md",
        "missing-g.svg",
        "missing i.md",
        "images/missing-c.png",
    ]


def test_reports_each_violation_on_its_own_line_inside_a_block() -> None:
    lines = [
        DocLine(DOC, 1, "A paragraph that wraps,", False),
        DocLine(DOC, 2, "then links [here](missing-a.md) and", False),
        DocLine(DOC, 3, "names `docs/missing-b.md` and [a title](missing-c.md 'that", False),
        DocLine(DOC, 4, "wraps').", False),
        DocLine(DOC, 6, '<p align="center">', False),
        DocLine(DOC, 7, '  <img width="320"', False),
        DocLine(DOC, 8, '       src="missing-d.png">', False),
        DocLine(DOC, 9, "</p>", False),
    ]
    assert [v.split(" — ")[0] for v in find_violations(lines, DIRS)] == [
        f"{DOC}:2: missing-a.md",
        f"{DOC}:3: docs/missing-b.md",
        f"{DOC}:3: missing-c.md",
        f"{DOC}:7: missing-d.png",
    ]


def test_accepts_existing_targets_in_every_supported_form() -> None:
    lines = _doc(
        "[up](../README.md#documentation) [dir](../contributing/) [here](./)",
        "[root](/docs/README.md) [line](../../src/ouroboros/__init__.py:1)",
        '[title](../README.md "Docs index") [encoded](../%52EADME.md)',
        '<img src="../images/ouroboros.png"> <a href="../README.md">x</a>',
        "[docs]: ../README.md",
        "[^1]: Footnote text, not a link target.",
        "[^2]: no-such-footnote.md",
        "`docs/../README.md` `docs/contributing/../README.md`",
        "`src/ouroboros/` `tests/conftest.py::isolate_ouroboros_home`",
        "`src/ouroboros/__init__.py:1-3` `docs/README.md#index` `./docs/README.md`",
        "`src/ouroboros/__init__.py:1–3` (an en-dash range)",
        "`.github/workflows/`",
        "| table | `src/ouroboros/__init__.py` [x](../README.md) |",
    )
    lines += _doc("pytest tests/unit/docs/::x 'src/ouroboros/__init__.py:1'.", fenced=True)
    assert find_violations(lines, DIRS) == []


def test_url_rule_paths_with_a_line_suffix_and_drive_letters_are_reported() -> None:
    lines = _doc(
        "[a](missing.md:123)",
        "[b](missing.md:123-130)",
        "[c](README:12)",
        "[d](C:\\x\\y.md)",
        "[e](C:/x/y.md)",
    )
    assert [v.split(": ", 1)[1] for v in find_violations(lines, DIRS)] == [
        f"missing.md:123 — {REMEDY_MISSING_LINK}",
        f"missing.md:123-130 — {REMEDY_MISSING_LINK}",
        f"README:12 — {REMEDY_MISSING_LINK}",
        f"C:\\x\\y.md — {REMEDY_ABSOLUTE}",
        f"C:/x/y.md — {REMEDY_ABSOLUTE}",
    ]


def test_url_rule_any_rfc_3986_scheme_is_a_url() -> None:
    lines = _doc(
        "[a](a:b)",
        "[b](com.example.app:x)",
        "[c](https://x/y:123)",
        "[d](mailto:a@b)",
        "[e](vscode:extension/x) [f](x-y+z:q)",
    )
    assert find_violations(lines, DIRS) == []


def test_skips_urls_anchors_autolinks_and_non_repository_tokens() -> None:
    lines = _doc(
        "[a](https://example.com/missing) [b](mailto:x@example.com) [c](#heading)",
        "[d](<../README.md>) [e](//cdn.example.com/x.png) <https://example.com>",
        "`config/models.py` `~/.ouroboros/seeds/` `ouroboros run` `app/main.py`",
    )
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
    lines = _doc(*(f"`{token}`" for token in tokens))
    lines += _doc(*tokens[:-1], fenced=True, path="docs/guides/other.md")
    assert find_violations(lines, DIRS) == []


def test_reports_absolute_filesystem_paths_in_link_targets() -> None:
    lines = _doc(
        "[a](/Users/someone/ouroboros/README.md)",
        "[b](/home/someone/ouroboros/README.md)",
        "[c](file:///Users/someone/ouroboros/README.md)",
        "[d](C:\\Users\\someone\\README.md)",
    )
    violations = find_violations(lines, DIRS)
    assert len(violations) == 4
    assert all(violation.endswith(REMEDY_ABSOLUTE) for violation in violations)


def test_reports_a_dead_repository_root_link_as_missing_not_absolute() -> None:
    assert find_violations(_doc("[x](/docs/no-such-page.md)"), DIRS) == [
        f"{DOC}:1: /docs/no-such-page.md — {REMEDY_MISSING_LINK}"
    ]


def test_reports_a_link_that_escapes_the_repository() -> None:
    assert find_violations(_doc("[x](../../../outside.md)"), DIRS) == [
        f"{DOC}:1: ../../../outside.md — {REMEDY_OUTSIDE_REPO}"
    ]


def test_case_mismatch_is_reported_on_every_platform() -> None:
    violations = find_violations(_doc("[x](../README.MD) `docs/readme.md`"), DIRS)
    assert [violation.split(" — ")[0] for violation in violations] == [
        f"{DOC}:1: ../README.MD",
        f"{DOC}:1: docs/readme.md",
    ]


def test_links_that_are_literal_text_are_not_checked() -> None:
    lines = _doc("Write `[label](missing.md)` to link a file.")
    lines += _doc("[label](missing.md)", fenced=True, path="docs/guides/other.md")
    assert find_violations(lines, DIRS) == []


def test_fenced_paths_are_checked_only_under_a_real_second_segment() -> None:
    lines = _doc(
        "uv run pytest tests/unit/no_such_test.py -q",
        'python "src/ouroboros/no_such_module.py"',
        # The reader's project, not this repository.
        "cat src/app.py tests/test_api.py docs/notes.md",
        # Part of another path, not a repository path.
        "cd /home/me/project/src/ouroboros/missing.py ~/docs/guides/x",
        fenced=True,
    )
    assert [violation.split(" — ")[0] for violation in find_violations(lines, DIRS)] == [
        f"{DOC}:1: tests/unit/no_such_test.py",
        f"{DOC}:3: src/ouroboros/no_such_module.py",
    ]


def test_code_blocks_the_parser_finds_get_the_code_block_rule() -> None:
    lines = [
        DocLine(DOC, 1, "> ```", False),
        DocLine(DOC, 2, "> cat src/ouroboros/no_such_module.py src/app.py", False),
        DocLine(DOC, 3, "> ```", False),
        DocLine(DOC, 5, "    cat tests/unit/no_such_test.py tests/test_api.py", False),
    ]
    assert [violation.split(" — ")[0] for violation in find_violations(lines, DIRS)] == [
        f"{DOC}:2: src/ouroboros/no_such_module.py",
        f"{DOC}:5: tests/unit/no_such_test.py",
    ]


def test_runtime_state_dirs_are_not_checked_but_repository_dot_dirs_are() -> None:
    # ``.ouroboros/`` and ``.claude/`` name the reader's own project state.
    assert find_violations(_doc("`.ouroboros/seeds/no-such.yaml` `.claude/no-such.json`")) == []
    assert find_violations(_doc("`.github/workflows/no-such.yml`")) == [
        f"{DOC}:1: .github/workflows/no-such.yml — {REMEDY_MISSING_PATH}"
    ]


def test_reports_every_href_and_src_of_every_html_tag() -> None:
    lines = _doc(
        '<a href="missing-a.md" src="missing-b.png">x</a>',
        # The first target resolves; the dead second one must still be reported.
        '<a href="../README.md" src="missing-c.png"><img src="../README.md" href=missing-d>',
        "<IMG SRC=missing-e.png> <a href='missing&amp;f.md'>f</a> <br/>",
    )
    assert _tokens(lines) == [
        "missing-a.md",
        "missing-b.png",
        "missing-c.png",
        "missing-d",
        "missing-e.png",
        "missing&f.md",
    ]


def test_html_comments_and_non_link_angle_text_are_not_tags() -> None:
    lines = _doc(
        '<!-- <a href="missing.md"> --> a < b, <session_id>, <https://example.com>',
        '`<img src="missing.png">` is literal text',
    )
    assert find_violations(lines, DIRS) == []


def test_reports_a_tag_left_open_at_the_end_of_an_html_block() -> None:
    lines = _doc(
        '<div><img alt="a > b" src="missing.png"',
        '<div><custom-card href="missing.md"',
    )
    assert [v.split(": ", 1)[1] for v in find_violations(lines, DIRS)] == [
        f"missing.png — {REMEDY_MISSING_LINK}",
        f'<img alt="a > b" src="missing.png" — {REMEDY_OPEN_TAG}',
        f"missing.md — {REMEDY_MISSING_LINK}",
        f'<custom-card href="missing.md" — {REMEDY_OPEN_TAG}',
    ]


def test_a_tag_that_closes_on_a_later_line_is_read_whole() -> None:
    lines = [
        DocLine(DOC, 1, 'Intro <img alt="a > b"', False),
        DocLine(DOC, 2, 'src="missing.png"> and <custom-card', False),
        DocLine(DOC, 3, 'href="missing.md">.', False),
    ]
    assert [v.split(" — ")[0] for v in find_violations(lines, DIRS)] == [
        f"{DOC}:1: missing.png",
        f"{DOC}:2: missing.md",
    ]


def test_a_complete_tag_with_a_quoted_angle_bracket_is_not_open() -> None:
    lines = _doc('<img alt="a > b" src="../images/ouroboros.png"> and <a href="../README.md">')
    assert find_violations(lines, DIRS) == []


def test_unclosed_link_or_tag_in_a_paragraph_is_text_not_a_link() -> None:
    # CommonMark renders these as literal text, so there is no link to follow.
    lines = _doc(
        'Intro <img alt="a > b" src="missing.png"',
        '<custom-card href="missing.md"',
        "See [the guide](missing.md",
    )
    assert find_violations(lines, DIRS) == []


def test_link_destinations_follow_commonmark() -> None:
    lines = _doc(
        "[a](../README(archive).md) [b](missing((nested)).md)",
        "[c](missing\\(1\\).md) [d [nested [text]] here](missing-d.md)",
    )
    assert _tokens(lines) == [
        "../README(archive).md",
        "missing((nested)).md",
        "missing(1).md",
        "missing-d.md",
    ]
    # Balanced parentheses around a link, titles in every form, and empty targets.
    ok = _doc("(see [x](../README.md)) [y](../README.md 'Docs') [z](../README.md (Docs)) [e]()")
    assert find_violations(ok, DIRS) == []


def test_reference_links_are_checked_once_at_their_definition() -> None:
    lines = _doc("See [the guide][guide] and [again][guide].", "[guide]: missing.md")
    assert [v.split(" — ")[0] for v in find_violations(lines, DIRS)] == [f"{DOC}:3: missing.md"]


def test_text_that_only_resembles_a_link_is_not_checked() -> None:
    lines = _doc(
        "\\[not a link\\](missing-a.md) [x](missing-b.md junk) [y](missing-c(.md )",
        # Review round 4: neither is a CommonMark link.
        "plain ](missing.md)",
        "\\[literal](missing.md)",
    )
    assert find_violations(lines, DIRS) == []


def test_lines_the_corpus_reader_drops_contribute_nothing() -> None:
    # Line 2 carries an ignore marker, so ``iter_lines`` does not yield it; the
    # link it would complete cannot form.
    lines = [DocLine(DOC, 1, "[a dead", False), DocLine(DOC, 3, "link](missing.md)?", False)]
    assert find_violations(lines, DIRS) == []
    joined = [DocLine(DOC, 1, "[a dead", False), DocLine(DOC, 2, "link](missing.md)?", False)]
    assert [v.split(" — ")[0] for v in find_violations(joined, DIRS)] == [f"{DOC}:1: missing.md"]


def test_references_takes_one_doc_at_a_time() -> None:
    lines = [DocLine("docs/a.md", 1, "x", False), DocLine("docs/b.md", 1, "y", False)]
    with pytest.raises(ValueError, match="one doc at a time"):
        references(lines)
