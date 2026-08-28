#!/bin/bash
# Ouroboros installer — auto-detects runtime and installs accordingly.
# Usage: curl -fsSL https://raw.githubusercontent.com/Q00/ouroboros/main/scripts/install.sh | bash
#
# Runtime selection (first match wins):
#   1. OUROBOROS_INSTALL_RUNTIME env var
#      (claude|claude-sdk|claude-cli|codex|opencode|hermes|gemini|goose|kiro|copilot|pi|gjc|all)
#   2. Existing ~/.ouroboros/config.yaml runtime — preserved on upgrade
#      unless OUROBOROS_INSTALL_RECONFIGURE=1 (or --reconfigure flag) is set.
#   3. Interactive prompt when stdin is a TTY.
#   4. Auto-detect single CLI on PATH; default to claude in pipe mode.
set -euo pipefail

PACKAGE_NAME="ouroboros-ai"
CLICK_SPEC="click>=8.1.0,<9.0.0"
MIN_PYTHON="3.12"
DEFAULT_PYTHON_SPEC=">=3.12"
LITELLM_PYTHON_SPEC=">=3.12,<3.14"
IS_LOCAL=false
RECONFIGURE="${OUROBOROS_INSTALL_RECONFIGURE:-}"
EXPLICIT_RUNTIME="${OUROBOROS_INSTALL_RUNTIME:-}"

if [ -z "${NO_COLOR:-}" ] && { [ -t 1 ] || [ -n "${FORCE_COLOR:-}" ]; } && { [ "${TERM:-}" != "dumb" ] || [ -n "${FORCE_COLOR:-}" ]; }; then
  BOLD="$(printf '\033[1m')"
  DIM="$(printf '\033[2m')"
  GREEN="$(printf '\033[1;32m')"
  YELLOW="$(printf '\033[1;33m')"
  BLUE="$(printf '\033[1;34m')"
  CYAN="$(printf '\033[1;36m')"
  MAGENTA="$(printf '\033[1;35m')"
  RED="$(printf '\033[1;31m')"
  RESET="$(printf '\033[0m')"
else
  BOLD=""
  DIM=""
  GREEN=""
  YELLOW=""
  BLUE=""
  CYAN=""
  MAGENTA=""
  RED=""
  RESET=""
fi

_say() {
  printf '%s\n' "$*"
}

_blank() {
  printf '\n'
}

_banner() {
  _say "${MAGENTA}╭────────────────────────────────────────────╮${RESET}"
  _say "${MAGENTA}│${RESET} ${BOLD}${CYAN}Ouroboros installer${RESET}                         ${MAGENTA}│${RESET}"
  _say "${MAGENTA}│${RESET} ${DIM}Specification-first AI development${RESET}          ${MAGENTA}│${RESET}"
  _say "${MAGENTA}╰────────────────────────────────────────────╯${RESET}"
  _say "${DIM}Installs the CLI, chooses an agent backend, and wires up local skills.${RESET}"
}

_step() {
  _blank
  _say "${BLUE}◆${RESET} ${BOLD}$1${RESET}"
  if [ "${2:-}" != "" ]; then
    _say "  ${DIM}$2${RESET}"
  fi
}

_ok() {
  _say "  ${GREEN}✓${RESET} $1"
}

_warn() {
  _say "  ${YELLOW}!${RESET} $1"
}

_err() {
  _say "  ${RED}✗${RESET} $1"
}

_info() {
  _say "  ${DIM}•${RESET} $1"
}

# Quote a string as one shell word using ASCII only, independent of the host
# bash version. `printf %q` cannot be used here: bash 3.2 (the macOS system
# bash) escapes only the bytes of a multibyte character that it happens to
# consider unsafe, leaving the rest raw, which emits invalid UTF-8 and lets a
# profile name smuggle RTL overrides or ANSI escapes into the terminal. This
# escapes every byte outside a conservative safe set, so the output is always
# printable ASCII and always re-reads as the original name.
_shell_quote() {
  local _sq_in=$1 _sq_out='' _sq_char _sq_code _sq_i _sq_len
  local LC_ALL=C
  case $_sq_in in
    '') printf "\$''"; return ;;
    *[!A-Za-z0-9_.,:/@%+=-]*) ;;
    *) printf '%s' "$_sq_in"; return ;;
  esac
  _sq_len=${#_sq_in}
  _sq_i=0
  while [ "$_sq_i" -lt "$_sq_len" ]; do
    _sq_char=${_sq_in:_sq_i:1}
    case $_sq_char in
      [A-Za-z0-9_.,:/@%+=-]) _sq_out=$_sq_out$_sq_char ;;
      *)
        # `printf "'c"` yields a signed value, so bytes >= 0x80 arrive
        # negative; mask them back into the 0-255 range before formatting.
        printf -v _sq_code '%d' "'$_sq_char"
        printf -v _sq_code '%03o' "$((_sq_code & 255))"
        _sq_out=$_sq_out\\$_sq_code
        ;;
    esac
    _sq_i=$((_sq_i + 1))
  done
  printf '%s' "\$'$_sq_out'"
}

_choice() {
  printf '  %b[%s]%b %-8s %s\n' "$BOLD" "$1" "$RESET" "$2" "$3"
}

_prompt() {
  printf '%b' "${BOLD}$1${RESET}"
}

# --- Anonymous install telemetry (PostHog) ---------------------------------
# Same privacy contract as the CLI (see TELEMETRY.md): random UUID, no PII,
# no code/paths. Disable with DO_NOT_TRACK=1, OUROBOROS_TELEMETRY=0, or
# telemetry.enabled: false in ~/.ouroboros/config.yaml.
# The API key is a public write-only PostHog project key.

# TELEMETRY.md declares ~/.ouroboros/.env a trusted persistent control
# source; the application loader applies it at import via python-dotenv's
# dotenv_values(), which resolves a key repeated in the file to its LAST
# occurrence. This standalone installer must honor the same telemetry keys
# before any notice or capture, so a persisted opt-out or destination
# override there holds during install -- including matching that same
# last-wins resolution for an internally-duplicated key, so the two loaders
# never disagree about a single file's meaning. Only the four allowlisted
# telemetry keys are read, and an already-set real process environment
# value always wins over the file, no matter how many times a key repeats
# in it (mirrors config/loader.py).
#
# Grammar covered exactly like dotenv_values(): bare/export-prefixed
# bindings, single- and double-quoted values (with an inline `# comment`
# after a closing quote), one layer of matching quotes around the KEY
# itself, and last-wins on duplicate keys. Grammar this single-line shell
# reader genuinely cannot follow -- a physically MULTILINE quoted value, or
# an ESCAPE sequence inside a double-quoted value -- is never guessed at:
# `_TELEMETRY_USER_ENV_AMBIGUOUS` is set instead, and `_telemetry_enabled`
# treats that as an unconditional opt-out. A real parse error dotenv itself
# also rejects (trailing garbage after a legitimate closing quote) stays a
# plain skip -- both loaders already agree there's nothing to trust there.
_telemetry_load_user_env() {
  local f="$HOME/.ouroboros/.env" line key value rest after trailing
  local preset_var parsed_var
  local preset_DO_NOT_TRACK preset_OUROBOROS_TELEMETRY
  local preset_OUROBOROS_POSTHOG_API_KEY preset_OUROBOROS_POSTHOG_HOST
  local parsed_DO_NOT_TRACK="" parsed_OUROBOROS_TELEMETRY=""
  local parsed_OUROBOROS_POSTHOG_API_KEY="" parsed_OUROBOROS_POSTHOG_HOST=""
  { [ -f "$f" ] && [ -r "$f" ]; } || return 0

  # Phase 1: snapshot which of the four allowlisted keys are already set in
  # the REAL process environment, before the trusted file is consulted at
  # all -- this is the one precedence check that must survive unchanged.
  for key in DO_NOT_TRACK OUROBOROS_TELEMETRY OUROBOROS_POSTHOG_API_KEY OUROBOROS_POSTHOG_HOST; do
    if eval "[ -n \"\${$key+x}\" ]"; then
      printf -v "preset_$key" '%s' true
    else
      printf -v "preset_$key" '%s' false
    fi
  done

  while IFS= read -r line || [ -n "$line" ]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in
      export[[:space:]]*)
        line="${line#export}"
        line="${line#"${line%%[![:space:]]*}"}"
        ;;
    esac
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    key="${key%"${key##*[![:space:]]}"}"
    # python-dotenv also accepts a key wrapped in one layer of matching
    # quotes (`'OUROBOROS_TELEMETRY'=0`, `"DO_NOT_TRACK"=1`) -- strip it
    # before the allowlist check so those parse identically to the bare
    # form. A key that only has ONE side quoted (no matching closing quote)
    # falls through unchanged and correctly misses the allowlist below,
    # same as today.
    case "$key" in
      \"*\")
        key="${key#\"}"
        key="${key%\"}"
        ;;
      \'*\')
        key="${key#\'}"
        key="${key%\'}"
        ;;
    esac
    case "$key" in
      DO_NOT_TRACK|OUROBOROS_TELEMETRY|OUROBOROS_POSTHOG_API_KEY|OUROBOROS_POSTHOG_HOST) ;;
      *) continue ;;
    esac
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    case "$value" in
      \"*)
        # A quoted value's content runs to the FIRST matching quote; only
        # trailing whitespace and/or a `# comment` may follow it (matches
        # python-dotenv). This is what makes a normal persisted opt-out like
        # `OUROBOROS_TELEMETRY="0" # persisted opt-out` parse to "0" instead
        # of falling into the unquoted branch below and keeping its quotes.
        # A missing closing quote and trailing garbage after a real closing
        # quote are NOT the same situation in dotenv, even though both look
        # like "unparseable" to a single-line shell reader:
        #   - trailing garbage after a legitimate closing quote (e.g.
        #     `OUROBOROS_TELEMETRY="0"x`) IS a parse error in python-dotenv
        #     too -- both sides skip the binding, no disagreement possible.
        #   - a MISSING closing quote is not an error to dotenv at all: it
        #     is the start of a value that continues reading subsequent
        #     PHYSICAL LINES until a closing quote is found (a genuinely
        #     multiline value), which this single-line reader cannot
        #     follow. Silently skipping here left telemetry ON while the
        #     application resolved a real (stripped) value -- fail closed
        #     instead, same posture as the escape-bearing case below.
        rest="${value#\"}"
        case "$rest" in
          *\"*) ;;
          *) _TELEMETRY_USER_ENV_AMBIGUOUS=1; continue ;;
        esac
        after="${rest#*\"}"
        trailing="${after#"${after%%[![:space:]]*}"}"
        case "$trailing" in
          ''|'#'*) ;;
          *) continue ;;
        esac
        value="${rest%%\"*}"
        # dotenv_values() decodes backslash escapes inside a double-quoted
        # value (e.g. the two source characters `\n` become an actual
        # newline); this shell parser copies them literally. A hand-rolled
        # parser will never chase python-dotenv's full escape grammar
        # (`\"` alone would need real state tracking), so rather than guess
        # and risk agreeing with neither interpretation, refuse to trust
        # THIS occurrence's value at all and force telemetry off for the
        # whole run: under-collecting on an ambiguous value is always safe,
        # over-collecting is the actual privacy violation. This is
        # deliberately asymmetric -- an ambiguous value that LOOKS like an
        # enable (e.g. `"1\n"`) also disables the installer run, diverging
        # from the application only in the safe direction.
        case "$value" in
          *\\*) _TELEMETRY_USER_ENV_AMBIGUOUS=1; continue ;;
        esac
        ;;
      \'*)
        # Single-quoted values are literal (no escape decoding, matching
        # today's handling already), but a missing closing quote has the
        # SAME multiline-continuation meaning in dotenv as the double-quote
        # case above -- fail closed for the same reason, not a plain skip.
        rest="${value#\'}"
        case "$rest" in
          *\'*) ;;
          *) _TELEMETRY_USER_ENV_AMBIGUOUS=1; continue ;;
        esac
        after="${rest#*\'}"
        trailing="${after#"${after%%[![:space:]]*}"}"
        case "$trailing" in
          ''|'#'*) ;;
          *) continue ;;
        esac
        value="${rest%%\'*}"
        ;;
      *)
        # Unquoted values follow dotenv grammar: an inline ` # comment` is
        # not part of the value. Dropping it here matters for opt-outs --
        # `OUROBOROS_TELEMETRY=0 # off` must parse as "0", not fail the
        # flag match and silently keep collection on.
        value="${value%%[[:space:]]#*}"
        value="${value%"${value##*[![:space:]]}"}"
        ;;
    esac
    [ -n "$value" ] || continue
    # Phase 2: accumulate into a per-key holding variable rather than
    # exporting immediately -- overwriting on every valid occurrence gives
    # LAST-wins semantics for a key duplicated WITHIN the trusted file,
    # matching dotenv_values(). The old per-line "export, then skip if
    # already set" approach silently flipped this to first-wins: once the
    # first occurrence exported the variable, the same `${key+x}` guard
    # would already be true for every later occurrence in THIS SAME file
    # too, blocking them even though none of them are the real process env
    # this guard exists to protect.
    printf -v "parsed_$key" '%s' "$value"
  done < "$f"

  # Phase 3: export each key's last-parsed-from-file value, but ONLY if it
  # was not already present in the real process environment (phase 1) --
  # preserving real process-env precedence while now resolving duplicates
  # inside the file itself with last-wins instead of first-wins. An empty
  # `parsed_*` here means no occurrence in the file ever produced a value
  # worth keeping (every occurrence, if any, failed to parse or was empty).
  for key in DO_NOT_TRACK OUROBOROS_TELEMETRY OUROBOROS_POSTHOG_API_KEY OUROBOROS_POSTHOG_HOST; do
    preset_var="preset_$key"
    parsed_var="parsed_$key"
    if [ "${!preset_var}" = false ] && [ -n "${!parsed_var}" ]; then
      export "$key=${!parsed_var}"
    fi
  done
}
_telemetry_load_user_env

PH_API_KEY="${OUROBOROS_POSTHOG_API_KEY:-phc_mSoetD4ExLDDCi3vNua635NhwRTgHfRaCG9WYNKmrvv5}"
PH_HOST="${OUROBOROS_POSTHOG_HOST:-https://us.i.posthog.com}"

_telemetry_config_allows() {
  local f="$HOME/.ouroboros/config.yaml" script_path script_dir source_root=""
  local python_candidate ouroboros_cmd shebang status
  # `-e` is false for a dangling symlink. That is invalid persisted state,
  # not a genuinely absent config, so keep it on the fail-closed path.
  [ -e "$f" ] || [ -L "$f" ] || return 0
  [ -r "$f" ] || return 1

  # Match the application resolver: parse the complete YAML document and
  # validate every known field before trusting telemetry.enabled. A partial
  # text parser can accept telemetry.enabled=true while overlooking malformed
  # YAML or an invalid unrelated field. Existing configuration therefore
  # requires a Python environment with Ouroboros' real schema available;
  # otherwise collection fails closed. A genuinely absent config retains the
  # documented default-on behavior after the notice below.
  # `curl | bash` has no sourced file path; nounset must not break this
  # optional local-source optimization.
  script_path="${BASH_SOURCE[0]-}"
  script_dir=""
  if [ -n "$script_path" ]; then
    script_dir=$(CDPATH='' cd -- "$(dirname -- "$script_path")" 2>/dev/null && pwd) || true
  fi
  if [ -n "$script_dir" ] && [ -f "$script_dir/../src/ouroboros/config/models.py" ]; then
    source_root=$(CDPATH='' cd -- "$script_dir/../src" 2>/dev/null && pwd) || true
  fi

  _telemetry_validate_config_with_python() {
    local interpreter="$1"
    "$interpreter" -I - "$f" "$source_root" <<'PY'
import sys

config_path, source_root = sys.argv[1:]
if source_root:
    sys.path.insert(0, source_root)

try:
    import yaml
    from ouroboros.config.models import OuroborosConfig
except Exception:
    raise SystemExit(2)

try:
    with open(config_path, encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file)
    config = OuroborosConfig.model_validate({} if raw is None else raw)
except Exception:
    raise SystemExit(1)

# Older installed schemas may validate without a telemetry field. Fail closed
# instead of dereferencing an attribute the schema does not have.
enabled = getattr(getattr(config, "telemetry", None), "enabled", False)
raise SystemExit(0 if enabled is True else 1)
PY
  }

  for python_candidate in python3 python; do
    python_candidate=$(command -v "$python_candidate" 2>/dev/null || true)
    [ -n "$python_candidate" ] || continue
    if _telemetry_validate_config_with_python "$python_candidate"; then
      unset -f _telemetry_validate_config_with_python
      return 0
    else
      status=$?
      if [ "$status" -ne 2 ]; then
        unset -f _telemetry_validate_config_with_python
        return 1
      fi
    fi
  done

  # uv/pipx entry points carry their environment's Python interpreter in the
  # shebang. It may have the schema when the system Python does not.
  ouroboros_cmd=$(command -v ouroboros 2>/dev/null || true)
  if [ -n "$ouroboros_cmd" ] && [ -r "$ouroboros_cmd" ]; then
    shebang=$(head -n 1 "$ouroboros_cmd" 2>/dev/null || true)
    case "$shebang" in
      '#!'/*)
        python_candidate=${shebang#'#!'}
        case "$python_candidate" in
          *' '*) ;;
          *)
            if [ -x "$python_candidate" ]; then
              if _telemetry_validate_config_with_python "$python_candidate"; then
                unset -f _telemetry_validate_config_with_python
                return 0
              fi
            fi
            ;;
        esac
        ;;
    esac
  fi

  unset -f _telemetry_validate_config_with_python
  return 1
}

_telemetry_enabled() {
  [ -n "$PH_API_KEY" ] || return 1
  # Set by _telemetry_load_user_env when a trusted-file value couldn't be
  # parsed unambiguously (an escape-bearing double-quoted value) -- an
  # opt-out this installer cannot resolve is treated as an opt-out, not
  # ignored. Any one disabling control wins, matching every other flag here.
  [ -z "${_TELEMETRY_USER_ENV_AMBIGUOUS:-}" ] || return 1
  local dnt oel
  # Lowercase and trim each flag before matching so `OFF`, `Yes`, `TRUE`,
  # or values with stray whitespace are recognized the same as their
  # canonical lowercase forms (mirrors config/loader.py's `.strip().lower()`).
  dnt=$(printf '%s' "${DO_NOT_TRACK:-}" | tr '[:upper:]' '[:lower:]')
  dnt="${dnt#"${dnt%%[![:space:]]*}"}"
  dnt="${dnt%"${dnt##*[![:space:]]}"}"
  case "$dnt" in 1|true|on|yes) return 1 ;; esac
  oel=$(printf '%s' "${OUROBOROS_TELEMETRY:-}" | tr '[:upper:]' '[:lower:]')
  oel="${oel#"${oel%%[![:space:]]*}"}"
  oel="${oel%"${oel##*[![:space:]]}"}"
  case "$oel" in 0|false|off|no) return 1 ;; esac
  # OUROBOROS_TELEMETRY=1 is never an override: persisted opt-out and
  # malformed configuration stay authoritative (TELEMETRY.md: any one
  # disabling control wins), matching the application resolver.
  _telemetry_config_allows || return 1
  command -v curl &>/dev/null || return 1
  return 0
}

_telemetry_distinct_id() {
  local f="$HOME/.ouroboros/telemetry.json" id="" winner_id="" tmp file_existed=false lockdir wait_i
  local salvage_id=""
  local uuid_re='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'

  # Four nested helpers, all `unset -f` below so extracting just this outer
  # function's source -- e.g. for an isolated concurrency-test driver --
  # carries them along automatically instead of leaving a dangling
  # reference:
  #
  #   _telemetry_extract_uuid  -- LENIENT, TEXT-ONLY. Prints the id,
  #   canonicalized to lowercase, whenever a sed match ANYWHERE in the raw
  #   text looks UUID-shaped -- ignorant of JSON structure entirely. This is
  #   only correct for genuinely unparseable text (telemetry.py salvages the
  #   same way there, via a raw-text regex): once a document IS valid JSON,
  #   a sed match found "anywhere" could be nested inside another object, or
  #   the first of several duplicate top-level keys, and must NOT be trusted
  #   -- see `_telemetry_read_state`.
  #
  #   _telemetry_read_state  -- the structural source of truth. Requires
  #   python3 (a soft dependency elsewhere in this installer too). Parses
  #   the file as JSON and looks at the TOP-LEVEL `distinct_id` field only
  #   -- Python's own json.load naturally resolves duplicate keys to the
  #   LAST occurrence, matching telemetry.py's loader exactly, and a value
  #   nested inside another object is correctly invisible to a plain
  #   `data.get(...)`. Prints one of: `CANONICAL:<id>` (top-level, pattern-
  #   valid, already lowercase -- fully valid, no repair needed),
  #   `REPAIR:<id>` (top-level, pattern-valid, wrong case -- a real identity
  #   worth salvaging, canonicalized), `INVALID` (valid JSON, but no usable
  #   top-level id -- missing, non-string, non-UUID, or only present nested
  #   -- telemetry.py does NOT salvage from a valid-but-wrong-shaped
  #   document, only from unparseable text, so this must NOT fall through to
  #   the lenient text search), `MALFORMED` (not valid JSON at all -- the
  #   lenient, text-only salvage path legitimately applies here), or
  #   `NO_PYTHON3` when python3 isn't on PATH -- without a JSON parser, pure
  #   shell cannot tell top-level from nested or resolve duplicate keys, so
  #   this is a documented best-effort gap.
  #
  #   _telemetry_canonical_id  -- STRICT adopt-without-repair check ("has a
  #   real repair already landed on disk"): CANONICAL only when python3 is
  #   present; falls back to the old pattern+case-only sed check (no
  #   structural awareness) when it is not.
  #
  #   _telemetry_salvage_candidate  -- the repair candidate getter: reuses a
  #   REPAIR-state top-level id or a MALFORMED-text lenient match; withholds
  #   entirely on INVALID (mirrors telemetry.py: no salvage from valid JSON
  #   lacking a usable top-level id); falls back to the lenient text search
  #   without python3.
  _telemetry_extract_uuid() {
    local r
    r=$(sed -n 's/.*"distinct_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -1)
    if [[ "$r" =~ $uuid_re ]]; then
      printf '%s' "$r" | tr '[:upper:]' '[:lower:]'
    fi
  }
  _telemetry_read_state() {
    local py
    py=$(command -v python3 2>/dev/null || true)
    if [ -z "$py" ]; then
      printf 'NO_PYTHON3'
      return 0
    fi
    "$py" -c '
import json, re, sys

uuid_re = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    print("MALFORMED")
else:
    value = data.get("distinct_id") if isinstance(data, dict) else None
    if isinstance(value, str) and uuid_re.match(value):
        lowered = value.lower()
        print(("CANONICAL:" if lowered == value else "REPAIR:") + lowered)
    else:
        print("INVALID")
' "$1" 2>/dev/null
  }
  _telemetry_canonical_id() {
    local state r
    state=$(_telemetry_read_state "$1")
    case "$state" in
      CANONICAL:*)
        printf '%s' "${state#CANONICAL:}"
        ;;
      NO_PYTHON3)
        r=$(sed -n 's/.*"distinct_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" 2>/dev/null | head -1)
        [[ "$r" =~ $uuid_re ]] || return 0
        [ "$r" = "$(printf '%s' "$r" | tr '[:upper:]' '[:lower:]')" ] || return 0
        printf '%s' "$r"
        ;;
      *) ;; # REPAIR / INVALID / MALFORMED: not yet fully valid
    esac
  }
  _telemetry_salvage_candidate() {
    local state
    state=$(_telemetry_read_state "$1")
    case "$state" in
      CANONICAL:*) printf '%s' "${state#CANONICAL:}" ;;
      REPAIR:*) printf '%s' "${state#REPAIR:}" ;;
      INVALID) ;; # valid JSON, no usable top-level id -- no salvage
      *) _telemetry_extract_uuid "$1" ;; # MALFORMED or NO_PYTHON3
    esac
  }

  if [ -f "$f" ]; then
    file_existed=true
    id=$(_telemetry_canonical_id "$f")
    if [ -z "$id" ]; then
      # Not already fully valid. Keep a real (top-level, or -- only for
      # genuinely unparseable text -- lenient-text) id as a SALVAGE
      # candidate rather than discarding it for a fresh mint; an id that
      # isn't usable at all (no field, a non-UUID value, or only nested
      # inside otherwise-valid JSON) leaves `salvage_id` empty and is
      # treated as fully corrupt below.
      salvage_id=$(_telemetry_salvage_candidate "$f")
    fi
  fi

  if [ -z "$id" ]; then
    if [ -n "$salvage_id" ]; then
      id="$salvage_id"
    elif command -v uuidgen &>/dev/null; then
      id=$(uuidgen | tr '[:upper:]' '[:lower:]')
    elif command -v python3 &>/dev/null; then
      id=$(python3 -c "import uuid; print(uuid.uuid4())" 2>/dev/null || true)
    fi
    if [ -z "$id" ]; then
      unset -f _telemetry_extract_uuid _telemetry_read_state _telemetry_canonical_id _telemetry_salvage_candidate
      return 1
    fi
    if ! mkdir -p "$HOME/.ouroboros" 2>/dev/null; then
      unset -f _telemetry_extract_uuid _telemetry_read_state _telemetry_canonical_id _telemetry_salvage_candidate
      return 1
    fi
    # Publish the (fresh or salvaged) identity via a same-directory temp
    # file, then re-read whatever actually landed on disk instead of
    # trusting our own candidate:
    #   - `$f` absent: publish with `ln` (hard link) — atomic
    #     create-if-not-exists, it fails if a concurrently-starting process
    #     (e.g. `ouroboros setup` in step 4, or another MCP session) already
    #     won, so nothing here is ever clobbered.
    #   - `$f` present but invalid (unparseable, no distinct_id, a non-UUID
    #     value, mismatched case, or malformed JSON around an otherwise
    #     salvageable id): `ln`/an unconditional `mv` are not enough here —
    #     every racing process would each mint or salvage independently and
    #     the last `mv` would silently win, so processes that already
    #     emitted events under their own reading would disagree with what
    #     ends up persisted. Exactly one process must own the repair.
    #     `$f.repair.lock` is the SAME lock name telemetry.py's
    #     `_repair_state` uses (it creates the path with `O_CREAT|O_EXCL` as
    #     a file; here `mkdir` is the atomic create-if-not-exists primitive).
    #     Both primitives only check whether *something* already exists at
    #     that path, regardless of type, so a shell `mkdir` and a Python
    #     `O_EXCL` file open correctly exclude each other at the same path
    #     (verified on macOS: whichever side creates the path first, the
    #     other's create call fails with EEXIST).
    # Either way, every process then adopts whichever valid, canonical
    # distinct_id survives in `$f` (mirrors telemetry.py's
    # `_publish_new_state` / `_repair_state`, which apply the same
    # UUID-validate-canonicalize-and-salvage rules).
    tmp="$f.$$.tmp"
    # Grouped so an unwritable `$HOME/.ouroboros` (read-only dir, e.g.) is
    # silent: a shell redirection's own open failure prints to the *current*
    # stderr before the trailing `2>/dev/null` on this line ever takes
    # effect, unlike an external command's (mv/ln/mkdir below) own error
    # output, which a trailing `2>/dev/null` does catch.
    { printf '{"distinct_id": "%s", "created_at": "%s", "notice_shown": false}\n' \
      "$id" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp"; } 2>/dev/null || true
    if [ "$file_existed" = true ]; then
      lockdir="$f.repair.lock"
      if mkdir "$lockdir" 2>/dev/null; then
        # WINNER: re-validate under the lock -- another process may have
        # repaired the file (to a valid id) between our first read and
        # acquiring it.
        winner_id=$(_telemetry_canonical_id "$f")
        [ -n "$winner_id" ] || mv "$tmp" "$f" 2>/dev/null || true
        rmdir "$lockdir" 2>/dev/null || true
      else
        # LOSER: someone else owns the repair (or a stale lock). Wait,
        # bounded, for a valid id to land.
        winner_id=""
        wait_i=0
        while [ "$wait_i" -lt 10 ]; do
          sleep 0.05
          winner_id=$(_telemetry_canonical_id "$f")
          [ -n "$winner_id" ] && break
          wait_i=$((wait_i + 1))
        done
        if [ -z "$winner_id" ]; then
          # Lock never cleared (stale, from a killed process) -- take over
          # the replace ourselves rather than waiting forever, accepting
          # the tiny remaining race.
          mv "$tmp" "$f" 2>/dev/null || true
          rmdir "$lockdir" 2>/dev/null || rm -f "$lockdir" 2>/dev/null || true
        fi
      fi
    else
      ln "$tmp" "$f" 2>/dev/null || true
    fi
    rm -f "$tmp" 2>/dev/null || true
    # Fail-closed reread: if neither our own publish/repair attempt nor
    # anyone else's landed a usable id on disk (e.g. `$HOME/.ouroboros` is
    # unwritable/read-only, so every `mv`/`ln` above silently failed),
    # printing the local candidate would emit under an ephemeral,
    # never-persisted identity that no other process -- or a later run of
    # this same one -- could ever reproduce, permanently fragmenting
    # identity instead of just skipping a run's telemetry. Drop instead
    # (callers already treat a nonzero return as "skip telemetry"), matching
    # telemetry.py's stable-identity contract. Deliberately
    # `_telemetry_salvage_candidate`, not the strict `_telemetry_canonical_id`:
    # this is a "is there now a real, reusable identity at all" check, not a
    # "was it perfectly repaired" check -- a top-level id already sitting in
    # a file we couldn't write to is still a stable value every future read
    # of that same file will keep finding, so it's the best available
    # anchor, not a reason to go fully ephemeral. It still withholds on a
    # valid-JSON `INVALID` state (no usable top-level id) exactly as the
    # top-of-function salvage check does, so this reread can never resurrect
    # a nested-only or otherwise-unusable value that was correctly rejected
    # earlier in this same call.
    winner_id=$(_telemetry_salvage_candidate "$f")
    if [ -z "$winner_id" ]; then
      unset -f _telemetry_extract_uuid _telemetry_read_state _telemetry_canonical_id _telemetry_salvage_candidate
      return 1
    fi
    id="$winner_id"
  fi
  unset -f _telemetry_extract_uuid _telemetry_read_state _telemetry_canonical_id _telemetry_salvage_candidate
  printf '%s' "$id"
}

_telemetry_notice() {
  _telemetry_enabled || return 0
  local f="$HOME/.ouroboros/telemetry.json" tmp id py already_shown

  # Skip only when the persisted state is valid JSON, a dict, and its
  # TOP-LEVEL `notice_shown` is the literal boolean `true` -- mirrors the
  # identity reader's structural approach above (and telemetry.py's own
  # check). Fail toward disclosure: missing, nested-only (e.g. a
  # `{"wrapper": {"notice_shown": true}}` sibling object), a string
  # "true"/"false", any other non-bool, or malformed JSON must all still
  # show the notice -- a duplicate notice is harmless, a suppressed one
  # breaks the privacy contract. Without python3 on PATH we cannot parse
  # JSON structure in pure shell, so this falls back to the old raw-text
  # grep (documented best-effort gap, matches the identity reader's
  # NO_PYTHON3 fallback).
  py=$(command -v python3 2>/dev/null || true)
  already_shown=false
  if [ -f "$f" ]; then
    if [ -n "$py" ]; then
      if "$py" -c '
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(data, dict) and data.get("notice_shown") is True else 1)
' "$f" 2>/dev/null; then
        already_shown=true
      fi
    elif grep -q '"notice_shown"[[:space:]]*:[[:space:]]*true' "$f" 2>/dev/null; then
      already_shown=true
    fi
  fi
  [ "$already_shown" = false ] || return 0

  _blank
  _say "${BOLD}Anonymous usage stats help improve Ouroboros.${RESET}"
  _info "Collects commands, versions, and success rates — never code, prompts, or paths."
  _info "Opt out: export OUROBOROS_TELEMETRY=0  |  details: https://github.com/Q00/ouroboros/blob/main/TELEMETRY.md"

  # Persist the one-time notice before the first collection attempt. Failure
  # is harmless: this run was disclosed and a later run will disclose again.
  id=$(_telemetry_distinct_id) || return 0
  [ -n "$id" ] || return 0
  tmp="${f}.notice.$$"
  if [ -n "$py" ]; then
    # Structural set: parses the file, sets the TOP-LEVEL `notice_shown` to
    # the literal boolean `true`, and preserves every other field as-is.
    # Unlike the sed fallback below, this also covers a document whose
    # top-level `notice_shown` never existed in the first place (e.g. an
    # already-valid identity file that was never rewritten by the repair
    # path above) -- a blind "replace false with true" text substitution
    # has nothing to match there and would silently leave the field
    # missing forever, so a later run would show the notice again.
    if "$py" -c '
import json, sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("top-level JSON value is not an object")
except Exception:
    sys.exit(1)
data["notice_shown"] = True
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    json.dump(data, fh)
    fh.write("\n")
' "$f" "$tmp" 2>/dev/null; then
      mv "$tmp" "$f" 2>/dev/null || true
    fi
  elif sed 's/"notice_shown"[[:space:]]*:[[:space:]]*false/"notice_shown": true/' "$f" > "$tmp" 2>/dev/null; then
    mv "$tmp" "$f" 2>/dev/null || true
  fi
  rm -f "$tmp" 2>/dev/null || true
}

# _telemetry_ping <event> [key=value ...] — fire-and-forget, never fails.
_telemetry_ping() {
  _telemetry_enabled || return 0
  local event="$1" id os py body kv k v props api_key_safe event_safe id_safe
  shift
  id=$(_telemetry_distinct_id) || return 0

  os=$(uname -s | tr '[:upper:]' '[:lower:]')
  [[ "$os" =~ ^[A-Za-z0-9._-]{1,32}$ ]] || os="unknown"

  py=$(command -v python3 2>/dev/null || true)
  body=""
  if [ -n "$py" ]; then
    # Structural encoding: every value is JSON-escaped by python3's real
    # encoder and passed in via argv -- never interpolated into the -c code
    # string itself -- so a hostile value (embedded quotes, backslashes,
    # control characters, an attempted `","injected":"...` payload) can
    # only ever become the STRING CONTENTS of its own declared property; it
    # can never alter the payload's structure or add an undeclared key.
    # Keys are call-site literals already, but are still whitelisted to
    # `[A-Za-z0-9_]+` defensively -- a key that fails that check is
    # dropped, never stringified into a place it could do damage.
    body=$("$py" -c '
import json, re, sys

key_re = re.compile(r"^[A-Za-z0-9_]+$")

event, distinct_id, api_key, os_name = sys.argv[1:5]
props = {"os": os_name}
for kv in sys.argv[5:]:
    key, sep, value = kv.partition("=")
    if sep and key_re.match(key):
        props[key] = value
print(json.dumps(
    {
        "api_key": api_key,
        "event": event,
        "distinct_id": distinct_id,
        "properties": props,
    },
    separators=(",", ":"),
))
' "$event" "$id" "$PH_API_KEY" "$os" "$@" 2>/dev/null)
  fi

  if [ -z "$body" ]; then
    # No python3 (or the structural encoder failed): sanitize every value
    # through a strict safe-token filter before string concatenation, so no
    # quote/backslash/control character can ever survive to mutate the JSON
    # structure. Every legitimate installer value (version numbers,
    # method/runtime names, yes/no flags, a canonical-lowercase distinct_id,
    # the os/arch tokens above) fits this charset losslessly; anything that
    # doesn't is stripped down to it (then length-capped) or falls back to
    # `unknown` -- best-effort, consistent with the identity/notice
    # readers' own NO_PYTHON3 fallback posture elsewhere in this file.
    props='"os":"'"$os"'"'
    for kv in "$@"; do
      k=$(printf '%s' "${kv%%=*}" | tr -cd 'A-Za-z0-9_')
      [ -n "$k" ] || continue
      v=$(printf '%s' "${kv#*=}" | tr -cd 'A-Za-z0-9._-')
      v="${v:0:64}"
      [ -n "$v" ] || v="unknown"
      props="$props,\"$k\":\"$v\""
    done
    api_key_safe=$(printf '%s' "$PH_API_KEY" | tr -cd 'A-Za-z0-9._-')
    event_safe=$(printf '%s' "$event" | tr -cd 'A-Za-z0-9._-')
    id_safe=$(printf '%s' "$id" | tr -cd 'A-Za-z0-9._-')
    body='{"api_key":"'"$api_key_safe"'","event":"'"$event_safe"'","distinct_id":"'"$id_safe"'","properties":{'"$props"'}}'
  fi

  curl -fsS -m 4 -X POST "$PH_HOST/capture/" -H 'Content-Type: application/json' \
    -d "$body" \
    >/dev/null 2>&1 &
  return 0
}
# ---------------------------------------------------------------------------

# Parse simple flags: --reconfigure, --runtime <name>
while [ $# -gt 0 ]; do
  case "$1" in
    --reconfigure)
      RECONFIGURE="1"
      shift
      ;;
    --runtime)
      EXPLICIT_RUNTIME="${2:-}"
      shift 2
      ;;
    --runtime=*)
      EXPLICIT_RUNTIME="${1#--runtime=}"
      shift
      ;;
    *)
      shift
      ;;
  esac
done

# Override PACKAGE_NAME if running inside the repository clone
if [ -f "pyproject.toml" ] && grep -q "name = \"ouroboros-ai\"" pyproject.toml; then
  PACKAGE_NAME="."
  IS_LOCAL=true
elif [ -f "$(dirname "$0")/../pyproject.toml" ] && grep -q "name = \"ouroboros-ai\"" "$(dirname "$0")/../pyproject.toml"; then
  PACKAGE_NAME="$(dirname "$0")/.."
  IS_LOCAL=true
fi

# Auto-detect: if PyPI's info.version response is a pre-release, allow
# pre-releases. On lookup/parsing failure, stay stable-only.
PRE_FLAG=""
if [ "$IS_LOCAL" = false ] && command -v curl &>/dev/null; then
  LATEST=$(curl -fsSL "https://pypi.org/pypi/${PACKAGE_NAME}/json" 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['info']['version'])" 2>/dev/null || true)
  if [ -n "$LATEST" ]; then
    if echo "$LATEST" | grep -qE '(a|b|rc|dev)'; then
      PRE_FLAG="yes"
    fi
  fi
fi

# Closed install-channel vocabulary. Unknown values may contain private slugs,
# customer names, or account identifiers, so they always fold to `direct`.
case "${OUROBOROS_INSTALL_REF:-direct}" in
  direct|readme|readme-hero|readme-ko|readme-hero-ko|readme-zh|readme-hero-zh|docs-getting-started)
    INSTALL_REF="${OUROBOROS_INSTALL_REF:-direct}"
    ;;
  *) INSTALL_REF="direct" ;;
esac

_banner

_telemetry_notice

# 1. Detect installer: uv > pipx > pip (determines Python requirement)
HAS_UV=false
HAS_PIPX=false
PYTHON=""

_step "1/4  Checking Python tooling" "Prefer uv, then pipx, then pip."

if command -v uv &>/dev/null; then
  HAS_UV=true
  _ok "uv found: $(uv --version)"
elif command -v pipx &>/dev/null; then
  HAS_PIPX=true
  _ok "pipx found: $(pipx --version)"
fi

# Helper: check whether a Python executable meets the selected profile's range.
_python_ok() {
  local cmd="$1"
  local spec="${2:-$DEFAULT_PYTHON_SPEC}"
  local ver
  ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
  if [ -z "$ver" ]; then
    return 1
  fi
  if [ "$(printf '%s\n' "$MIN_PYTHON" "$ver" | sort -V | head -n1)" != "$MIN_PYTHON" ]; then
    return 1
  fi
  if [ "$spec" = "$LITELLM_PYTHON_SPEC" ] && [ "$(printf '%s\n' "3.14" "$ver" | sort -V | head -n1)" = "3.14" ]; then
    return 1
  fi
  return 0
}

_python_requirement_message() {
  if [ "${INSTALL_PYTHON_SPEC:-$DEFAULT_PYTHON_SPEC}" = "$LITELLM_PYTHON_SPEC" ]; then
    printf 'Python %s (Ouroboros LiteLLM profiles support Python 3.12-3.13; use Python 3.13)' "$LITELLM_PYTHON_SPEC"
  else
    printf 'Python %s' "$DEFAULT_PYTHON_SPEC"
  fi
}

_print_python_install_remediation() {
  _say "${BOLD}Install one of these, then run the installer again:${RESET}"
  _info "uv: pipx install uv"
  _info "uv: pip install --user uv"
  _info "uv: brew install uv"
  _info "uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
  if [ "${INSTALL_PYTHON_SPEC:-$DEFAULT_PYTHON_SPEC}" = "$LITELLM_PYTHON_SPEC" ]; then
    _info "Python 3.13: https://www.python.org/downloads/"
  else
    _info "Python ${MIN_PYTHON}+: https://www.python.org/downloads/"
  fi
}

# 2. Detect runtimes
_step "2/4  Choosing an agent backend" "Codex, Claude, Hermes, OpenCode, Gemini, Goose, Kiro, Copilot, Pi, and GJC are supported."
EXTRAS=""
RUNTIME=""
HAS_CODEX=false
HAS_CLAUDE=false
HAS_HERMES=false
HAS_OPENCODE=false
HAS_GEMINI=false
HAS_GOOSE=false
HAS_KIRO=false
HAS_COPILOT=false
HAS_PI=false
HAS_OMP=false
HAS_GJC=false
HAS_DSH=false
DSH_PLUGIN_SPEC="github:Q00/ouroboros#main&path:integrations/dsh-plugin"
if command -v codex &>/dev/null; then
  _ok "Codex found: $(which codex)"
  HAS_CODEX=true
fi
if command -v claude &>/dev/null; then
  _ok "Claude found: $(which claude)"
  HAS_CLAUDE=true
fi
if command -v hermes &>/dev/null; then
  _ok "Hermes found: $(which hermes)"
  HAS_HERMES=true
fi
if command -v opencode &>/dev/null; then
  _ok "OpenCode found: $(which opencode)"
  HAS_OPENCODE=true
fi
if command -v gemini &>/dev/null; then
  _ok "Gemini found: $(which gemini)"
  HAS_GEMINI=true
fi
if command -v goose &>/dev/null; then
  _ok "Goose found: $(which goose)"
  HAS_GOOSE=true
fi
if command -v kiro-cli &>/dev/null; then
  _ok "Kiro found: $(which kiro-cli)"
  HAS_KIRO=true
fi
if command -v copilot &>/dev/null; then
  _ok "Copilot found: $(which copilot)"
  HAS_COPILOT=true
fi
if command -v pi &>/dev/null; then
  _ok "Pi: $(which pi)"
  HAS_PI=true
fi
if command -v gjc &>/dev/null; then
  _ok "GJC found: $(which gjc)"
  HAS_GJC=true
fi
if command -v omp &>/dev/null; then
  _ok "OMP found: $(which omp)"
  HAS_OMP=true
fi
if command -v dsh &>/dev/null; then
  _ok "DeepSeek Harness found: $(which dsh)"
  HAS_DSH=true
fi

RUNTIME_COUNT=0
[ "$HAS_CLAUDE" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_CODEX" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_HERMES" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_OPENCODE" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_GEMINI" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_GOOSE" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_KIRO" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_COPILOT" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_PI" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_GJC" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))
[ "$HAS_OMP" = true ] && RUNTIME_COUNT=$((RUNTIME_COUNT + 1))

# Map a runtime name to (EXTRAS, RUNTIME) pair.
# Used after explicit/preserved runtime resolution to derive install extras.
# Keep this table boring and explicit: agents reading this installer should be
# able to see why Claude's SDK runtime and the modern MCP server are installed
# in different environments. Only the explicit Claude CLI path has no SDK deps.
_runtime_to_extras() {
  # `tui` ships with every selection so the settings GUI
  # (`ouroboros config`) works out of the box (#1414).
  case "$1" in
    claude) EXTRAS="[claude,tui]"; RUNTIME="claude" ;;
    claude-sdk) EXTRAS="[claude-sdk,tui]"; RUNTIME="claude-sdk" ;;
    claude-cli) EXTRAS="[claude-cli,tui]"; RUNTIME="claude-cli" ;;
    codex)   EXTRAS="[tui]"; RUNTIME="codex" ;;
    opencode) EXTRAS="[tui]"; RUNTIME="opencode" ;;
    hermes)  EXTRAS="[mcp,tui]"; RUNTIME="hermes" ;;
    gemini)  EXTRAS="[tui]"; RUNTIME="gemini" ;;
    goose)   EXTRAS="[tui]"; RUNTIME="goose" ;;
    kiro)    EXTRAS="[tui]"; RUNTIME="kiro" ;;
    copilot) EXTRAS="[tui]"; RUNTIME="copilot" ;;
    pi)      EXTRAS="[tui]"; RUNTIME="pi" ;;
    gjc)     EXTRAS="[tui]"; RUNTIME="gjc" ;;
    omp)     EXTRAS="[tui]"; RUNTIME="omp" ;;
    all)     EXTRAS="[all]"; RUNTIME="" ;;
    "")      EXTRAS="[tui]"; RUNTIME="" ;;
    *)
      _err "unsupported runtime '$1'"
      _info "Expected one of: claude, claude-sdk, claude-cli, codex, opencode, hermes, gemini, goose, kiro, copilot, pi, gjc, omp, all"
      exit 1
      ;;
  esac
}

# Try to read the previously-configured runtime from ~/.ouroboros/config.yaml.
# Preserves user choice across upgrades unless --reconfigure / --runtime is set.
EXISTING_RUNTIME=""
EXISTING_CONFIG="$HOME/.ouroboros/config.yaml"
if [ -z "$EXPLICIT_RUNTIME" ] && [ -z "$RECONFIGURE" ] && [ -f "$EXISTING_CONFIG" ] && command -v python3 &>/dev/null; then
  EXISTING_RUNTIME=$(EXISTING_CONFIG="$EXISTING_CONFIG" python3 -c "
import os, re
supported = {'claude', 'claude_mcp', 'codex', 'opencode', 'hermes', 'gemini', 'goose', 'kiro', 'copilot', 'pi', 'gjc', 'omp'}
try:
    lines = open(os.environ['EXISTING_CONFIG']).read().splitlines()
    in_orchestrator = False
    for line in lines:
        if re.match(r'^orchestrator:\s*(?:#.*)?$', line):
            in_orchestrator = True
            continue
        if in_orchestrator and line and not line[0].isspace():
            break
        if in_orchestrator:
            match = re.match(r'\s+runtime_backend:\s*[\"\']?([^\"\'\s#]+)', line)
            if match and match.group(1) in supported:
                # Preserve the transport identity on unattended upgrades:
                # claude is the SDK/MCP 1 profile; claude_mcp is the
                # explicit dependency-free CLI/MCP 2 worker profile.
                print('claude-cli' if match.group(1) == 'claude_mcp' else match.group(1))
                break
except Exception:
    pass
" 2>/dev/null || true)
fi

if [ -n "$EXPLICIT_RUNTIME" ]; then
  _blank
  _ok "Runtime: $EXPLICIT_RUNTIME (from --runtime / OUROBOROS_INSTALL_RUNTIME)"
  _runtime_to_extras "$EXPLICIT_RUNTIME"
elif [ -n "$EXISTING_RUNTIME" ]; then
  _blank
  _ok "Runtime: $EXISTING_RUNTIME (preserved from $EXISTING_CONFIG)"
  _info "Re-run with --reconfigure to choose again."
  _runtime_to_extras "$EXISTING_RUNTIME"
elif [ "$RUNTIME_COUNT" -gt 1 ]; then
  if [ -t 0 ]; then
    _blank
    _say "${BOLD}Multiple runtimes detected. Pick where Ouroboros should appear first:${RESET}"
    _choice 1 "Claude" "Claude SDK (MCP 1.x) + skills; MCP 2 server is isolated (${PACKAGE_NAME}[claude,tui])"
    _choice 2 "Codex" "Codex plugin artifacts (${PACKAGE_NAME}[tui])"
    _choice 3 "Hermes" "Hermes agent guides + MCP server (${PACKAGE_NAME}[mcp,tui])"
    _choice 4 "OpenCode" "OpenCode commands and agent files (${PACKAGE_NAME}[tui])"
    _choice 5 "Gemini" "Gemini CLI integration (${PACKAGE_NAME}[tui])"
    _choice 6 "Goose" "Goose CLI integration (${PACKAGE_NAME}[tui])"
    _choice 7 "Kiro" "Kiro CLI integration (${PACKAGE_NAME}[tui])"
    _choice 8 "Copilot" "GitHub Copilot integration (${PACKAGE_NAME}[tui])"
    _choice 9 "Pi" "Pi CLI bridge and instruction artifacts (${PACKAGE_NAME}[tui])"
    _choice 10 "GJC" "GJC CLI bridge and instruction artifacts (${PACKAGE_NAME}[tui])"
    _choice 11 "OMP" "Oh My Pi (omp) CLI bridge (${PACKAGE_NAME}[tui])"
    _choice 12 "All" "Install every optional integration (${PACKAGE_NAME}[all])"
    _prompt "Select [1]: "
    read -r choice
    case "${choice:-1}" in
      2) _runtime_to_extras "codex" ;;
      3) _runtime_to_extras "hermes" ;;
      4) _runtime_to_extras "opencode" ;;
      5) _runtime_to_extras "gemini" ;;
      6) _runtime_to_extras "goose" ;;
      7) _runtime_to_extras "kiro" ;;
      8) _runtime_to_extras "copilot" ;;
      9) _runtime_to_extras "pi" ;;
      10) _runtime_to_extras "gjc" ;;
      11) _runtime_to_extras "omp" ;;
      12) _runtime_to_extras "all" ;;
      *) _runtime_to_extras "claude" ;;
    esac
  else
    # Pipe mode: default to claude when multiple runtimes exist
    _warn "Multiple runtimes detected in non-interactive mode; defaulting to Claude."
    _runtime_to_extras "claude"
  fi
elif [ "$HAS_CLAUDE" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "claude"
elif [ "$HAS_CODEX" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "codex"
elif [ "$HAS_HERMES" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "hermes"
elif [ "$HAS_OPENCODE" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "opencode"
elif [ "$HAS_GEMINI" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "gemini"
elif [ "$HAS_GOOSE" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "goose"
elif [ "$HAS_KIRO" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "kiro"
elif [ "$HAS_COPILOT" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "copilot"
elif [ "$HAS_PI" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "pi"
elif [ "$HAS_GJC" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "gjc"
elif [ "$HAS_OMP" = true ] && [ "$RUNTIME_COUNT" -eq 1 ]; then
  _runtime_to_extras "omp"
else
  # No runtime CLI on PATH yet — first install. Always prompt when interactive
  # so the user picks deliberately rather than silently defaulting to claude.
  if [ -t 0 ]; then
    _blank
    _say "${BOLD}No runtime CLI detected yet. Choose the agent you plan to use:${RESET}"
    _choice 1 "Claude" "Claude SDK (MCP 1.x) + skills; MCP 2 server is isolated (${PACKAGE_NAME}[claude,tui])"
    _choice 2 "Codex" "Codex plugin artifacts (${PACKAGE_NAME}[tui])"
    _choice 3 "Hermes" "Hermes agent guides + MCP server (${PACKAGE_NAME}[mcp,tui])"
    _choice 4 "OpenCode" "OpenCode commands and agent files (${PACKAGE_NAME}[tui])"
    _choice 5 "Gemini" "Gemini CLI integration (${PACKAGE_NAME}[tui])"
    _choice 6 "Goose" "Goose CLI integration (${PACKAGE_NAME}[tui])"
    _choice 7 "Kiro" "Kiro CLI integration (${PACKAGE_NAME}[tui])"
    _choice 8 "Copilot" "GitHub Copilot integration (${PACKAGE_NAME}[tui])"
    _choice 9 "Pi" "Pi CLI bridge and instruction artifacts (${PACKAGE_NAME}[tui])"
    _choice 10 "GJC" "GJC CLI bridge and instruction artifacts (${PACKAGE_NAME}[tui])"
    _choice 11 "OMP" "Oh My Pi (omp) CLI bridge (${PACKAGE_NAME}[tui])"
    _choice 12 "All" "Install every optional integration (${PACKAGE_NAME}[all])"
    _choice 0 "None" "Base CLI only; choose a backend later"
    _prompt "Select [1]: "
    read -r choice
    case "${choice:-1}" in
      0) _runtime_to_extras "" ;;
      2) _runtime_to_extras "codex" ;;
      3) _runtime_to_extras "hermes" ;;
      4) _runtime_to_extras "opencode" ;;
      5) _runtime_to_extras "gemini" ;;
      6) _runtime_to_extras "goose" ;;
      7) _runtime_to_extras "kiro" ;;
      8) _runtime_to_extras "copilot" ;;
      9) _runtime_to_extras "pi" ;;
      10) _runtime_to_extras "gjc" ;;
      11) _runtime_to_extras "omp" ;;
      12) _runtime_to_extras "all" ;;
      *) _runtime_to_extras "claude" ;;
    esac
  else
    # Pipe mode (curl | bash): install base package, skip runtime-specific setup.
    _blank
    _warn "No runtime detected in non-interactive mode; installing the base package."
    _info "Pick a backend afterwards with: ouroboros setup --runtime <claude|claude-sdk|claude-cli|codex|opencode|hermes|gemini|goose|kiro|copilot|pi|gjc>"
    _runtime_to_extras ""
  fi
fi

if [ -n "$RUNTIME" ]; then
  _ok "Selected backend: $RUNTIME"
elif [ "$EXTRAS" = "[all]" ]; then
  _ok "Selected backend: all"
else
  _info "Selected backend: none yet"
fi

INSTALL_SPEC="${PACKAGE_NAME}${EXTRAS}"
INSTALL_PYTHON_SPEC="$DEFAULT_PYTHON_SPEC"
case "$EXTRAS" in
  "[all]" | "[litellm]" | *litellm*)
    INSTALL_PYTHON_SPEC="$LITELLM_PYTHON_SPEC"
    ;;
esac

# Python check: always required for pip; also needed by pipx to pick the right interpreter.
if [ "$HAS_UV" = false ]; then
  if [ "$HAS_PIPX" = true ]; then
    # For pipx: probe versioned candidates first, then fall back to generic names.
    for cmd in python3.14 python3.13 python3.12 python3 python; do
      if command -v "$cmd" &>/dev/null && _python_ok "$cmd" "$INSTALL_PYTHON_SPEC"; then
        PYTHON="$(command -v "$cmd")"
        break
      fi
    done
    if [ -z "$PYTHON" ]; then
      _blank
      _err "pipx requires $(_python_requirement_message), but none was found."
      _blank
      _print_python_install_remediation
      exit 1
    fi
    _ok "Python found: $($PYTHON --version)"
  else
    # pip fallback: prefer explicit compatible versions for constrained profiles.
    if [ "$INSTALL_PYTHON_SPEC" = "$LITELLM_PYTHON_SPEC" ]; then
      PYTHON_CANDIDATES="python3.13 python3.12 python3 python"
    else
      PYTHON_CANDIDATES="python3 python"
    fi
    for cmd in $PYTHON_CANDIDATES; do
      if command -v "$cmd" &>/dev/null && _python_ok "$cmd" "$INSTALL_PYTHON_SPEC"; then
        PYTHON="$cmd"
        break
      fi
    done
    if [ -z "$PYTHON" ]; then
      _blank
      _err "No installer found: uv, pipx, or $(_python_requirement_message)."
      _blank
      _print_python_install_remediation
      exit 1
    fi
    _ok "Python found: $($PYTHON --version)"
  fi
fi

_step "3/4  Installing Ouroboros" "Package: ${INSTALL_SPEC}"
_say "Installing ${INSTALL_SPEC} ..."

# 3. Install (or upgrade if already installed)
# uv tool install has issues with [extras] syntax — use --with for reliability.
INSTALL_METHOD=""
if [ "$HAS_UV" = true ]; then
  INSTALL_METHOD="uv"
  _info "Install method: uv tool install"
  # `click` is also declared in pyproject, but keep this explicit so the
  # installer can repair already-published wheels whose metadata missed it.
  UV_ARGS=(tool install --upgrade --python "$INSTALL_PYTHON_SPEC" "$PACKAGE_NAME" --with "$CLICK_SPEC")
  if [ -n "$PRE_FLAG" ]; then
    UV_ARGS+=(--prerelease=allow)
  fi
  # Map extras to explicit --with flags for uv.
  # NOTE: Pin specs MUST mirror [project.optional-dependencies] in
  # pyproject.toml. tests/unit/scripts/test_install_runtime_selection.py
  # asserts `[all]` covers every compatible extra while MCP stays in its
  # isolated profile, so silent drift is caught in CI rather than discovered
  # by a user with a conflicting tool environment.
  case "$EXTRAS" in
    "[claude,tui]" | "[claude-sdk,tui]")
      UV_ARGS+=(
        --with "claude-agent-sdk==0.2.139"
        --with "anthropic==0.122.0"
      )
      ;;
    "[mcp,tui]")
      UV_ARGS+=(--with "mcp==2.0.0")
      ;;
    "[all]")
      UV_ARGS+=(
        --with "claude-agent-sdk==0.2.139"
        --with "anthropic==0.122.0"
        --with "litellm==1.91.0"
      )
      ;;
  esac
  # Every install ships the settings GUI (`ouroboros config`).
  UV_ARGS+=(
    --with "textual==8.2.8"
    --with "textual-serve==1.1.3"
  )
  uv "${UV_ARGS[@]}"
elif [ "$HAS_PIPX" = true ]; then
  INSTALL_METHOD="pipx"
  _info "Install method: pipx"
  if [ -n "$PRE_FLAG" ]; then
    pipx install --force --python "$PYTHON" --pip-args='--pre' "$INSTALL_SPEC"
  else
    pipx install --force --python "$PYTHON" "$INSTALL_SPEC"
  fi
  # The venv name is the distribution name even when installing from a local path.
  pipx inject "ouroboros-ai" "$CLICK_SPEC"
else
  INSTALL_METHOD="pip"
  _info "Install method: pip --user"
  if [ -n "$PRE_FLAG" ]; then
    $PYTHON -m pip install --user --upgrade --pre "$INSTALL_SPEC" "$CLICK_SPEC"
  else
    $PYTHON -m pip install --user --upgrade "$INSTALL_SPEC" "$CLICK_SPEC"
  fi
fi

# Ensure setup runs the freshly-installed uv tool binary rather than a stale
# command already on PATH. uv can install into UV_TOOL_BIN_DIR or its configured
# tool bin directory without updating the current shell's PATH, so remember the
# fresh executable and invoke that exact path below. For pipx/pip installs,
# preserve the existing PATH command unless no ouroboros command is visible.
OUROBOROS_BIN=""
_prepend_path_if_ouroboros() {
  local candidate="$1"
  if [ -n "$candidate" ] && [ -x "$candidate/ouroboros" ]; then
    export PATH="$candidate:$PATH"
    if [ -z "$OUROBOROS_BIN" ]; then
      OUROBOROS_BIN="$candidate/ouroboros"
    fi
    return 0
  fi
  return 1
}

if [ "$INSTALL_METHOD" = "uv" ]; then
  _prepend_path_if_ouroboros "${UV_TOOL_BIN_DIR:-}" || true
  if command -v uv &>/dev/null; then
    UV_TOOL_BIN="$(uv tool dir --bin 2>/dev/null || true)"
    _prepend_path_if_ouroboros "$UV_TOOL_BIN" || true
  fi
fi

if [ -z "$OUROBOROS_BIN" ] && ! command -v ouroboros &>/dev/null; then
  for p in "$HOME/.local/bin" "$HOME/.cargo/bin" "$HOME/bin"; do
    _prepend_path_if_ouroboros "$p" || true
  done
fi

OUROBOROS_SETUP_CMD=""
if [ -n "$OUROBOROS_BIN" ]; then
  OUROBOROS_SETUP_CMD="$OUROBOROS_BIN"
elif command -v ouroboros &>/dev/null; then
  OUROBOROS_SETUP_CMD="ouroboros"
fi


# 4. Setup (ouroboros CLI configures runtime-specific integration)
_step "4/4  Wiring local integrations" "Creates config and runtime-specific files when a backend was selected."
if [ -n "$RUNTIME" ] && [ -n "$OUROBOROS_SETUP_CMD" ]; then
  _info "Running: $OUROBOROS_SETUP_CMD setup --runtime $RUNTIME --non-interactive"
  if "$OUROBOROS_SETUP_CMD" setup --runtime "$RUNTIME" --non-interactive; then
    :
  else
    setup_status=$?
    _warn "Runtime setup failed; installation is incomplete. Fix the error above and re-run setup."
    exit "$setup_status"
  fi
elif [ -n "$RUNTIME" ]; then
  _warn "ouroboros command is not on PATH yet; run setup after your shell sees the installed binary."
else
  _info "No backend selected; skipping runtime setup."
fi

# Refresh already-installed artifacts for every detected runtime, even those
# that are not the preserved primary backend. Setup only wires the selected
# runtime, so upgrades must not leave other runtimes' rules/skills/bridges
# stale. `setup refresh` is presence-gated per artifact and never touches MCP
# registrations or config.
if [ -n "$OUROBOROS_SETUP_CMD" ]; then
  _info "Refreshing runtime artifacts for detected runtimes"
  "$OUROBOROS_SETUP_CMD" setup refresh || _warn "Artifact refresh skipped; run: ouroboros setup refresh"
fi
_decimal_at_least() {
  local left right
  left="${1#${1%%[!0]*}}"
  right="${2#${2%%[!0]*}}"
  [ -n "$left" ] || left=0
  [ -n "$right" ] || right=0
  if [ "${#left}" -ne "${#right}" ]; then
    [ "${#left}" -gt "${#right}" ]
  else
    [ "$left" = "$right" ] || [[ "$left" > "$right" ]]
  fi
}

_ensure_omp_tool_call_timeout() {
  local omp_bin current
  omp_bin="$(command -v omp 2>/dev/null || true)"
  if [ -z "$omp_bin" ]; then
    return 0
  fi
  current="$("$omp_bin" config get extensionHandlers.toolCallTimeoutMs 2>/dev/null || true)"
  case "$current" in
    ''|*[!0-9]*) ;;
    *)
      if _decimal_at_least "$current" 60000; then
        return 0
      fi
      ;;
  esac
  if "$omp_bin" config set extensionHandlers.toolCallTimeoutMs 60000 >/dev/null 2>&1; then
    _ok "OMP MCP tool timeout set to 60s"
  else
    _warn "Could not set OMP MCP tool timeout; run: omp config set extensionHandlers.toolCallTimeoutMs 60000"
  fi
}

_ensure_omp_tool_call_timeout

# 5. Claude Code integration. The default Claude selection and its explicit
# SDK alias stay on MCP 1.x. The plugin-owned MCP launcher starts MCP 2 in a
# separate uvx environment and selects the dependency-free CLI worker.
# Skill refresh is artifact-only and should happen whenever Claude is present;
# otherwise a Codex-primary upgrade can leave Claude Code reading stale skills.
if command -v claude &>/dev/null && { [ "$RUNTIME" = "claude" ] || [ "$RUNTIME" = "claude-sdk" ] || [ "$EXTRAS" = "[all]" ]; }; then
  _blank
  _say "${BLUE}◆${RESET} ${BOLD}Claude Code extras${RESET}"

  _warn "Claude SDK is isolated on MCP 1.x. The Claude plugin launches the Ouroboros MCP 2 server in a separate uvx environment with --runtime claude-cli."
fi

if command -v claude &>/dev/null; then
  if ! { [ "$RUNTIME" = "claude" ] || [ "$RUNTIME" = "claude-sdk" ] || [ "$EXTRAS" = "[all]" ]; }; then
    _blank
    _say "${BLUE}◆${RESET} ${BOLD}Claude Code skills${RESET}"
  fi
  # 5a. Install/update Ouroboros skills (claude plugin)
  _info "Installing Ouroboros skills via Claude plugin marketplace..."
  claude plugin marketplace add Q00/ouroboros 2>/dev/null || true
  claude plugin marketplace update ouroboros 2>/dev/null || true
  if claude plugin install ouroboros@ouroboros 2>/dev/null; then
    # `install` is a no-op for an already-installed plugin — `update` moves it to the latest version
    claude plugin update ouroboros@ouroboros >/dev/null 2>&1 || true
    _ok "Skills installed"
  else
    _warn "Skills skipped. Manual install: claude plugin marketplace add Q00/ouroboros && claude plugin install ouroboros@ouroboros"
  fi
fi

_telemetry_ping install_completed "runtime=${RUNTIME:-none}" \
  "version=${LATEST:-unknown}" "ref=$INSTALL_REF"

_blank
_say "${GREEN}${BOLD}Done! Ouroboros is ready.${RESET}"
_blank
_say "${BOLD}Get started${RESET}"
_info 'Open your AI coding agent and run: > ooo interview "your idea here"'
_info 'Or from the terminal: ouroboros init start "your idea here"'
if [ -n "$RUNTIME" ]; then
  _info "Current backend: $RUNTIME"
fi
_info "Switch backend later: ouroboros setup --runtime <claude|claude-sdk|claude-cli|codex|opencode|hermes|gemini|goose|kiro|copilot|pi|gjc>"
_say "${BOLD}Model settings — use your default model or configure one directly${RESET}"
_info 'Inside your AI agent: > ooo config   (opens in your browser)'
_info 'From this terminal:  ouroboros config   (full-screen TUI)'

# 6. Optional direct model settings (interactive installs only).
# install.sh always runs in a real terminal when interactive, so the
# full-screen Textual settings app can open right here. curl|bash pipe
# installs skip this automatically ([ -t 0 ] is false).
if [ -t 0 ] && [ -z "${OUROBOROS_INSTALL_SKIP_CONFIG_GUI:-}" ]; then
  if [ "$RUNTIME" = "codex" ]; then
    GUI_MESSAGE="Codex's current default model is ready to use. Configure a model directly now?"
  else
    GUI_MESSAGE="Your selected runtime's default model is ready to use. Configure a model directly now?"
  fi
  # Most people should start with the runtime default. The settings UI is for
  # deliberate model pins, so it must remain an opt-in rather than a fresh
  # install speed bump.
  GUI_DEFAULT="n"
  GUI_HINT="[y/N]"
  _blank
  _say "${BOLD}${GUI_MESSAGE}${RESET}"
  _prompt "Open direct model settings $GUI_HINT: "
  read -r gui_choice
  case "${gui_choice:-$GUI_DEFAULT}" in
    y | Y | yes | YES)
      GUI_OK=false
      if [ -n "${OUROBOROS_SETUP_CMD:-}" ] && "$OUROBOROS_SETUP_CMD" config; then
        GUI_OK=true
      elif command -v uvx &>/dev/null; then
        # The selected extras may not include [tui]; run the GUI from an
        # ephemeral env with the tui extra instead of fattening the install.
        _info "Settings GUI needs the tui extra; running it via uvx..."
        if uvx --python "$DEFAULT_PYTHON_SPEC" --from "${PACKAGE_NAME}[tui]" ouroboros config; then
          GUI_OK=true
        fi
      fi
      if [ "$GUI_OK" = false ]; then
        _warn "Could not open the settings GUI."
        _info "Run it later with: uvx --python '>=3.12' --from '${PACKAGE_NAME}[tui]' ouroboros config"
      fi
      ;;
    *)
      _info "Using the runtime default model. You can change it anytime:"
      _info '  in your AI agent: > ooo config'
      _info '  in a terminal:    ouroboros config'
      ;;
  esac
fi

# 6. DeepSeek Harness integration. Unlike the other hosts, dsh keeps its plugins
# per profile, and `dsh plugin` requires --profile, so there is no single global
# install to run. Cover the profile a dsh user actually boots (`web`, which
# `dsh web` scaffolds) and refresh any other profile that already carries the
# bundle, which is how an existing install picks up a new release.
if [ "$HAS_DSH" = true ]; then
  _blank
  _say "${BLUE}◆${RESET} ${BOLD}DeepSeek Harness plugin${RESET}"

  DSH_PROFILE_ROOT="${DSH_HOME:-$HOME/.dsh}/profiles"
  DSH_TARGET_PROFILES=("web")
  _dsh_json_python="${PYTHON:-}"
  if [ -z "$_dsh_json_python" ]; then
    _dsh_json_python=$(command -v python3 2>/dev/null || true)
  fi
  if [ -z "$_dsh_json_python" ]; then
    _dsh_ouroboros_cmd=$(command -v ouroboros 2>/dev/null || true)
    if [ -n "$_dsh_ouroboros_cmd" ] && [ -r "$_dsh_ouroboros_cmd" ]; then
      _dsh_shebang=$(head -n 1 "$_dsh_ouroboros_cmd" 2>/dev/null || true)
      case "$_dsh_shebang" in
        '#!'/*)
          _dsh_json_python=${_dsh_shebang#'#!'}
          case "$_dsh_json_python" in
            *' '*) _dsh_json_python="" ;;
          esac
          [ -x "$_dsh_json_python" ] || _dsh_json_python=""
          ;;
      esac
    fi
  fi
  if [ -n "$_dsh_json_python" ] && [ -d "$DSH_PROFILE_ROOT" ]; then
    for _dsh_profile_dir in "$DSH_PROFILE_ROOT"/*/; do
      [ -d "$_dsh_profile_dir" ] || continue
      _dsh_profile=${_dsh_profile_dir#"$DSH_PROFILE_ROOT"/}
      _dsh_profile=${_dsh_profile%/}
      [ "$_dsh_profile" = "web" ] && continue
      # Only profiles that already opted in. Adding Ouroboros tools to an
      # unrelated profile because the installer ran is not an upgrade.
      if "$_dsh_json_python" -c 'import json, sys; data = json.load(open(sys.argv[1], encoding="utf-8")); dependencies = data.get("dependencies", {}) if isinstance(data, dict) else {}; sys.exit(0 if isinstance(dependencies, dict) and "dsh-ouroboros" in dependencies else 1)' "$_dsh_profile_dir/package.json" 2>/dev/null; then
        DSH_TARGET_PROFILES+=("$_dsh_profile")
      fi
    done
  fi

  for _dsh_profile in "${DSH_TARGET_PROFILES[@]}"; do
    _dsh_profile_q=$(_shell_quote "$_dsh_profile")
    if dsh plugin --profile "$_dsh_profile" add "$DSH_PLUGIN_SPEC" >/dev/null 2>&1; then
      _ok "dsh profile $_dsh_profile_q: Ouroboros tools installed"
    else
      _warn "dsh profile $_dsh_profile_q: install skipped"
      _info "Manual install: dsh plugin --profile $_dsh_profile_q add \"$DSH_PLUGIN_SPEC\""
    fi
  done
  _info "Type 'ooo interview <goal>' in a dsh chat to use them."
fi

_blank
_say "${BOLD}Like Ouroboros?${RESET}"
_info "Give the repo a star on GitHub: https://github.com/Q00/ouroboros"
