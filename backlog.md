# Ouroboros Code Audit Backlog

> Generated: 2026-08-23
> Method: 5 parallel analysis agents over `src/ouroboros/` (312k LOC at audit time; live tracked-module counts are intentionally omitted because they change independently of these findings)
> Baseline: `ruff` clean · `mypy` clean **only because 14 error codes are disabled**

## Executive summary

| Category | Count | Highest severity |
| :--- | ---: | :--- |
| A. Type-safety suppression | 12 | HIGH |
| B. Correctness / real bugs | 24 | HIGH |
| C. Security | 11 | HIGH |
| D. Abstraction / god objects | 29 | HIGH |
| E. Error handling & resilience | 35 | HIGH |
| F. Duplication | 12 | MEDIUM |
| G. Dead code & hygiene | 21 | LOW |
| H. Validation gaps | 14 | HIGH |
| **Total** | **158** | |

Headline: `mypy` reported "no issues" under the configured suppressions. Its checked-file
total can include Hatch-VCS-generated `src/ouroboros/_version.py`, so it is not a tracked-module inventory. Re-enabling just 3 of the 14
suppressed error codes surfaces **430 errors across 102 files**. The suppression is
concentrated where it hurts most — the runtime factory that constructs all 15
backends, and the MCP wire boundary.

---

## A. Type-safety suppression (12)

| # | Location | Issue | Sev |
| ---: | :--- | :--- | :--- |
| A1 | `pyproject.toml:199-215` | 14 mypy error codes disabled, incl. `arg-type`, `attr-defined`, `return-value`, `call-arg`, `override`. Hides 430 real errors. | HIGH |
| A2 | `orchestrator/runtime_factory.py:64-68` | `_runtime_kwargs()` returns `dict[str, object]`; splatted into 13 backend constructors → 48 of the file's 50 errors. A renamed kwarg in one backend fails only when a user selects it. | HIGH |
| A3 | `orchestrator/runtime_factory.py:91-120` | `codex_mcp` / `claude_mcp` build `runtime_kwargs` then discard it, silently dropping `skill_dispatcher`. Worker skill dispatch unavailable on both. Invisible to mypy because of A2. | HIGH |
| A4 | `orchestrator/runtime_factory.py:112` | `cli_path: str \| Path` → `build_claude_worker_runtime(cli_path: str)`. A `Path` reaches `_base_command() -> list[str]`; any `" ".join()` consumer raises. | MED |
| A5 | `mcp/sdk_mapping.py:172-175, 214` | `**common` heterogeneous-dict splat at 10 sites → 34 errors. Disables `Literal` discriminator checking on the MCP wire boundary. | MED |
| A6 | `core/file_lock.py:19-23` | Windows branch guarded with `os.name == "nt"`; mypy only narrows `sys.platform`. Entire Windows lock path is neither type-checked nor covered (`pragma: no cover`). | MED |
| A7 | `orchestrator/runtime_factory.py:213` | `GooseCliRuntime.skill_dispatcher: Any \| None` vs `SkillDispatchHandler \| None` on every sibling. | LOW |
| A8 | `orchestrator/runtime_factory.py:318` | `dict` invariance error; one-line annotation fix. | LOW |
| A9 | `providers/kiro_adapter.py:307` | `.get()` on a value typed `object`; a non-mapping `response_format` raises `AttributeError` in the request path. | MED |
| A10 | `mcp/detached_worker.py:257-264` | `error` rebound from `MCPServerError` to `str`; the reported `arg-type` error points at the symptom, not the cause, because `assignment` is also suppressed. | LOW |
| A11 | `cli/main.py:75-77` | Typer vendored-Click vs Click type divergence; needs a `cast` to be clean. | LOW |
| A12 | `mcp/sdk_mapping.py:33-39` | `_model_object` dumps nulls then `model_validate`s; an SDK upgrade adding a non-nullable field turns this into a wire-boundary `ValidationError`. | LOW |

---

## B. Correctness / real bugs (24)

| # | Location | Issue | Sev |
| ---: | :--- | :--- | :--- |
| B1 | `orchestrator/runner.py:9563` | `constraint_violations=[]` → `calculate_constraint_drift` returns `0.0` immediately. **30% of the drift score is hardcoded to zero.** | HIGH |
| B2 | `orchestrator/runner.py:9564` | `current_concepts=[]` → `calculate_ontology_drift` returns `1.0`. Every measurement carries a fixed `+0.2`. Combined with B1, `is_acceptable` is effectively always `False`. Correct usage exists at `evolution/drift_recording.py:25`. | HIGH |
| B3 | `orchestrator/runner.py:9455 / 11433 / 11657` | Message-consumption loop triplicated; the drift block exists in **only the first**. Resuming a session silently stops emitting `DriftMeasuredEvent`. | HIGH |
| B4 | `orchestrator/runner.py:9301` | `ACNode(depends_on=[])` hardcodes "no dependencies" → the graph is flat, so mutually dependent ACs run concurrently. No TODO marker. | HIGH |
| B5 | `core/file_lock.py:208-210` | `ContextVar.reset(token)` is called **before** `_release_posix_lock`. A cross-context finalize raises `ValueError`, skipping the release → directory flock held until process exit. | HIGH |
| B6 | `core/file_lock.py:169-186` | Reentrant lease found active → yields **without acquiring any flock**. An `asyncio.Task` inherits the lease via copied context; if the outer holder exits first, the child runs unlocked. | HIGH |
| B7 | `core/git_workflow.py:140` | `is_on_protected_branch` returns `False` on git timeout / missing binary / non-repo. The only guard against auto-committing to `main` **fails open**. | HIGH |
| B8 | `orchestrator/n_version_tournament.py:176,199` | `export_worktree_diff` returns `""` on git error; `apply_diff_to_workspace` treats `""` as success. The tournament winner's work is silently discarded at `log.debug`. | HIGH |
| B9 | `tui/screens/lineage_detail.py:537-578` | `git status --porcelain` guard wrapped in `except Exception: pass` with "try checkout anyway" → `git checkout <tag>` runs over an uncommitted tree. | HIGH |
| B10 | `core/session_signal.py:270 vs 334` | `to_event_data()` omits `message` by default; `from_event_data` requires it. `from_event_data(to_event_data())` fails for every default-serialized signal. | HIGH |
| B11 | `core/seed_verify_gate.py:74-84` | `except Exception: return "warn"` — an unrelated config typo silently downgrades a `block` gate to `warn`. | HIGH |
| B12 | `core/security.py:537-543` | `sanitize_for_logging` recurses into `dict` but **not `list`**. `{"providers": [{"api_key": "sk-live-…"}]}` logs the secret verbatim. | HIGH |
| B13 | `core/initial_context.py:9` | `core` imports `bigbang.pm_seed` while `bigbang/seed_generator.py:56` imports `core.seed` — a package-level cycle with core on the wrong side. | HIGH |
| B14 | `orchestrator/parallel_executor.py:11395` vs `runner.py:1783` | Same BLOCKED fallback; only the parallel path overrides `failure_class` to `BLOCKED`. A blocked direct-route attempt is misclassified `EVIDENCE_MISSING`. | HIGH |
| B15 | `orchestrator/parallel_executor.py:12161` vs `runner.py:2116` | Route-replay validation implemented twice, using **two different enum types** (`RouteVerifierOutcome` vs `VerifierOutcome`) for the same concept. | HIGH |
| B16 | `core/worktree.py:376-386` | PID-reuse false-negative: a recycled PID makes a dead lock owner look alive → `WorktreeError("Task already active")` forever. Same-host locks have no time-based escape. | MED |
| B17 | `core/file_lock.py:509` | Windows `blocking=True` retries ~10× then raises raw `OSError`; POSIX blocks forever. Normalization to `BlockingIOError` is skipped because it is gated on `not blocking`. | MED |
| B18 | `core/file_lock.py:507-511` | `exclusive=False` is silently exclusive on Windows (CRT `_locking` has no shared mode). `checkpoint.py:485` and `interview.py:1234` both expect shared readers. | MED |
| B19 | `core/file_lock.py:74-76` | Post-`yield` revalidation raises `ESTALE` from `__exit__` **after** the caller committed its write. Callers may retry a non-idempotent operation. | MED |
| B20 | `core/ac_tree.py:186-203` | `add_node` silently **replaces** the root on any later depth-0 insert, orphaning the tree. No parent-exists or depth-contiguity check. `from_dict` bypasses the `max_depth` cap entirely. | MED |
| B21 | `core/ac_tree.py:58` | `ACNode` is `frozen=True` but all five `with_*` methods share the same `metadata` dict by reference — mutating one mutates every historical copy. | MED |
| B22 | `core/conductor.py:118-127` | `stable_payload_digest` uses `json.dumps(default=str)`; the digest gates successor authorization, so two different payloads sharing a `repr` authorize identically. | MED |
| B23 | `mcp/detached_jobs.py:296` | Windows detach flags via `getattr(..., 0)`; if absent the flags collapse to `0` and the "detached" worker dies with its parent. POSIX has no such hole. | MED |
| B24 | `mcp/tools/evolution_handlers.py:152-155` | `Seed.from_dict(...)` under `except Exception: pass` leaves the **raw unvalidated string** in `normalized["seed_content"]`. | MED |

---

## C. Security (11)

| # | Location | Issue | Sev |
| ---: | :--- | :--- | :--- |
| C1 | `core/security.py:537-543` | Secret leak via un-recursed lists (= B12). `observability/logging.py:58` is the live consumer. | HIGH |
| C2 | `core/security.py:327,376` | `type(value) is not str` lets `str` subclasses and `StrEnum` members bypass **both** `is_credential_shaped` and `is_stable_authority_identity`. The codebase uses `StrEnum` heavily. | MED |
| C3 | `core/security.py:639-664` | `validate_path_containment` is check-then-use TOCTOU: non-strict `resolve()` then the caller opens by name. `file_lock.py` demonstrates the correct `O_NOFOLLOW`/`dir_fd` technique 500 lines away. | MED |
| C4 | `core/owner_only.py:51-70` | `package_owned_directory` fully resolves symlinks, so a symlink pointing **into** the namespace causes `secure_directory` to chmod a caller-owned directory to `0700` — the exact bug the docstring claims to have fixed. | MED |
| C5 | `core/owner_only.py:232-234` | `mkdir(parents=True, mode=…)` applies the mode to the **leaf only**; intermediates get `0o777 & ~umask`. `~/.ouroboros/state` stays world-readable while `state/interviews` is `0700`. | MED |
| C6 | `core/security.py:452-457` | `is_sensitive_field` substring-matches `"key"`/`"auth"` → `monkey`, `author`, `authority`, `key_count` all redacted. Over-redaction destroys incident diagnostics. | MED |
| C7 | `core/control_contract.py:67,101` | `extra: dict[str, Any]` gets a **shallow** copy, no JSON-safety check, no secret check — yet is serialized into a persisted event. Every sibling contract validates this. | MED |
| C8 | `core/conductor.py:24` + `core/session_signal.py:27` | Byte-identical `_SECRET_PATTERNS`; neither imports `security.is_credential_shaped`. A `ghp_…` PAT that `security.py` would catch is persisted. **4 independent secret detectors.** | MED |
| C9 | `core/security.py:418-450` | `validate_api_key_format` accepts any 10-char alphanumeric — `"placeholder"` validates. | LOW |
| C10 | `core/owner_only.py:180-183` | Verifies POSIX mode bits but not ownership or inherited ACLs; the stated guarantee is stronger than what is checked. | LOW |
| C11 | `core/security.py:262-283` | `_is_credential_namespace_label` returns from inside a loop over an **unordered** `frozenset`. Not exploitable today; one added label away from `PYTHONHASHSEED`-dependent behavior. | LOW |

---

## D. Abstraction / god objects (29)

| # | Location | Issue | Sev |
| ---: | :--- | :--- | :--- |
| D1 | `orchestrator/runner.py:879` | `OrchestratorRunner`: **181 methods**, 52 attributes, 20 ctor params, 272-line `__init__`. Only 8 methods public. | HIGH |
| D2 | `orchestrator/parallel_executor.py:2621` | `ParallelACExecutor`: **119 methods**, 59 attributes, **32 ctor params**, 341-line `__init__` containing 16 branches and 6 raises. Only 2 public members. | HIGH |
| D3 | `orchestrator/parallel_executor.py:7857` | `_execute_atomic_ac` is **1,611 lines** — 110 locals, 86 conditionals, 25 returns, 7 nested closures. | HIGH |
| D4 | `orchestrator/runner.py:10804` | `_resume_session_impl` is **1,243 lines** — 70 locals, **33 returns**, nesting to depth 7. Resume correctness depends on which of 33 exits is taken. | HIGH |
| D5 | `orchestrator/parallel_executor.py:3787` | `execute_parallel` is **1,216 lines** with **126 locals** and only 1 return — every local stays live to the end. | HIGH |
| D6 | `orchestrator/runner.py:8904` | `execute_precreated_session` is **1,199 lines**, 94 locals, 32 returns. | HIGH |
| D7 | `orchestrator/runner.py:9394-9618` | `_consume_task_stream` is a 225-line closure mutating **11 `nonlocal` variables** — 11 hidden out-parameters invisible at the call site. | HIGH |
| D8 | `orchestrator/parallel_executor.py:11683` | `_load_bounded_route_resume_state` is 885 lines with **84 bare `RuntimeError`s**. Callers cannot distinguish corrupt state from policy drift from a bug. | HIGH |
| D9 | `orchestrator/runner.py:6020` | `_restore_execution_contract` is 733 lines, 36 raises — same anti-pattern on the direct-route side. | HIGH |
| D10 | `orchestrator/parallel_executor.py:4622-4645` | Nesting **depth 9**: concurrency slot → try → try/finally → shielded `CancelScope` → if. A raise at 4636 leaks the provider-effect scope permanently. | HIGH |
| D11 | `orchestrator/parallel_executor.py` | **52 sites** at nesting depth ≥ 5. | HIGH |
| D12 | `orchestrator/runner.py:11433-11700` | 12 sites at depth 5-7 in one function; a `break` at 11667 exits only the inner loop, correctness resting on three post-loop re-checks. | HIGH |
| D13 | `orchestrator/parallel_executor.py:844-859` | Binds **five private methods** of `SharedRateLimitBucket` (`_prune`, `_tokens_in_window`, `_snapshot`, `_request_wait_seconds`, `_token_wait_seconds`). | HIGH |
| D14 | `orchestrator/parallel_executor.py:3224-3442` | Wraps **seven private methods** of `ACRuntimeHandleManager` in identically-named private methods — a pass-through layer that defeats the encapsulation the manager exists to provide. Clearest extraction target. | HIGH |
| D15 | `orchestrator/runner.py:4045,4047` | `getattr(self._adapter, "_skills_dir")` and `_skill_dispatcher`, then compares a bound method's `__func__` against `CodexCommandDispatcher.dispatch`. Wrapping the dispatcher breaks portability detection. | HIGH |
| D16 | `orchestrator/runner.py` (~20 sites) | `getattr(self._adapter, "runtime_backend", None)` repeated ~20×; 65 `getattr` calls total (69 in `parallel_executor.py`). The adapter protocol is nowhere declared. Line 5732 defaults to `"unknown"`, line 1185 to `None`, for the same attribute. | HIGH |
| D17 | `orchestrator/runner.py:9700` + `parallel_executor.py:10248,10428` | Recoverable-pause payload hand-built in 3 places with **divergent `schema_version`** (1 vs 2) and different fields. No `PauseRecord` type. | MED |
| D18 | `orchestrator/runner.py:1280` | `_route_call_effort` is 368 lines containing 14 near-identical `runtime_backend` getattr dict entries. | MED |
| D19 | `orchestrator/runner.py` (11 methods) | `_message_*` / `_metadata_*` / `_duration_*` are stateless helpers invoked as `cls.…` — the class is used as a namespace, inflating it to 181 methods. | MED |
| D20 | `orchestrator/parallel_executor.py:3022` | `getattr(coordinator, "_reasoning_effort", None)` — reads another object's private field. | MED |
| D21 | `core/hitl_contract.py` + `hitl_state.py` + `hitl_resume.py` | One concern split 3 ways with **duplicated extraction layers** that have already diverged. Every resume builds the contract twice and replays the event stream twice. | MED |
| D22 | `core/seed_contract.py` + `seed_verify_gate.py` | `SeedContract.from_seed` flattens ACs, discarding `verify_command` / `expected_artifacts` / `output_assertion` — so the verify gate cannot use it and reads `Seed` directly. Two views, one lossy in exactly the fields the other needs. | MED |
| D23 | `core/directive.py:32-46` | 60 lines documenting a `StepAction → Directive` migration that then states "no caller is modified here". **4 independent `is_terminal` definitions** across the layer. | MED |
| D24 | `core/security.py` | 681 lines doing 5 unrelated jobs; `validate_path_containment` — the only security-critical function — is buried at line 639 behind 400 lines of unrelated regex heuristics. | MED |
| D25 | `core/seed.py` | 949-line schema module carrying a filesystem-portability library (`expected_artifact_path_error`, `os.pathconf`, Windows reserved-name tables) that 3 other packages import directly. | MED |
| D26 | `core/` (4 impls) | Four hand-rolled "bounded text" validators; only 2 enforce a byte ceiling, only 2 reject secrets, only 2 check invisible Unicode. | MED |
| D27 | `core/` (3 idioms) | Three model idioms for one "durable contract" concept: Pydantic frozen, frozen-slots dataclass + hand-written `to_event_data`, and **mutable** dataclass (`ACTree`). | MED |
| D28 | `core/seed.py:900` + 3 others | Four JSON-freezing implementations, two incompatible strategies. `_FrozenDict` subclasses `dict`, so `dict.__setitem__(frozen, k, v)` bypasses the poisoned mutators; `MappingProxyType` has no such hole. | MED |
| D29 | `core/hitl_state.py:105` vs `session_signal_projection.py:151` | Adjacent replay projections take **opposite** failure policies: one silently drops malformed events (a pending wait becomes invisible), the other raises permanently with no quarantine. | MED |

---

## E. Error handling & resilience (35)

Baseline: **655** `except Exception`, **118** `except BaseException`, **181** `except …: pass`, **0** bare `except:`.

| # | Location | Issue | Sev |
| ---: | :--- | :--- | :--- |
| E1 | `orchestrator/n_version_tournament.py:123` | `_git` has no `timeout=`; used by `create`/`cleanup`/`__exit__`. A wedged `git worktree add` hangs the tournament **and** its teardown. | HIGH |
| E2 | `tui/screens/lineage_detail.py:498` | `asyncio.create_task(self._perform_rewind(...))` with no reference retained. Collection mid-flight leaves the event store and worktree divergent. | HIGH |
| E3 | `tui/screens/lineage_detail.py:537-578` | Three `create_subprocess_exec` git calls with untimed `communicate()`, each in `except Exception: pass`. | HIGH |
| E4 | `providers/copilot_cli_adapter.py:803` | `if self._timeout is None: await process.wait()` — and `None` is the default (line 169). No idle guard, unlike `opencode_runtime.py:1268`. A wedged CLI blocks forever. | HIGH |
| E5 | `providers/codex_cli_adapter.py:1187` | Same unbounded `await process.wait()` branch. | HIGH |
| E6 | `providers/gjc_llm_adapter.py:400` | Same pattern; `timeout: float \| None = None` is the ctor default. | HIGH |
| E7 | `cli/commands/plugin.py:1299,1305` | `git clone --depth 1` and `git rev-parse` with no timeout and stdin not redirected → `ooo plugin add` hangs silently on a credential prompt (`capture_output=True`). | HIGH |
| E8 | `mcp/detached_worker.py:258-261` | Terminal-status write under `except Exception: pass`. The launcher waits out `_STARTUP_TIMEOUT_SECONDS` and reports launch failure for a worker that ran. Failure reason lost. | MED |
| E9 | `mcp/detached_worker.py:263-268` | Compensation failure invisible; a job can be left permanently non-terminal with no log line. | MED |
| E10 | `mcp/detached_worker.py:271-280` | `server.shutdown()` / `event_store.close()` swallowed in `finally` → DB connections leak on every worker exit. | MED |
| E11 | `mcp/job_manager.py:2426` | Terminal-event snapshot build under `except Exception: pass` → a caller can conclude a job is not terminal when it is. | MED |
| E12 | `evolution/loop_support.py:658` | `except Exception: pass` immediately before `_release_lineage_flight` — the failure is never recorded, so the next generation proceeds as if the previous merely finished. | MED |
| E13 | `evolution/loop_support.py:836` | `waiter_heartbeat` exception swallowed with no log → a lineage claim lease expires while the loop still believes it holds it. | MED |
| E14 | `auto/pipeline.py:3230` | Only the `TimeoutError` branch cancels `_cancel_ralph_status_mirror`; the trailing `except Exception: pass` leaves the mirror task uncancelled and unreferenced. | MED |
| E15 | `plugin/skills/registry.py:149` | `asyncio.create_task` from a watchdog thread with no reference. On a non-loop thread it raises; when it runs, a skill edit can be silently never reloaded. | MED |
| E16 | `plugin/skills/registry.py:402` | `stop_watcher()` in `__del__` under `except Exception: pass` → observer thread and inotify handles leak. | MED |
| E17 | `observability/logging.py:104-108` | `handler.flush()` **and** `handler.close()` both swallowed — drops exactly the buffered records needed to diagnose the triggering failure. | MED |
| E18 | `orchestrator/runner.py:1054` | `load_config()` under `except Exception: pass` with silent fallback to `_shipped_config` → the whole evolution loop runs under different tiers and budgets with no warning. | MED |
| E19 | `orchestrator/kiro_adapter.py:271` | stderr collection `except Exception: pass` then `return list(lines)` → a failed run is reported with a partial or empty error message. | MED |
| E20 | `telemetry.py:760,815` | Publish/lock failures fully swallowed; a failed `unlink` leaves a **stale `.repair.lock`** that forces every later process down the 10×20 ms wait path. | MED |
| E21 | `telemetry.py:834` | `_write_state` swallows all `_atomic_write` failures → `distinct_id` never persists, retention metrics fragment with zero signal. | MED |
| E22 | `tui/app.py:413,517,659,892,1303` | Five `get_screen(...)` blocks with `except Exception: pass`. Line 517 swallows a logging failure inside an already-degraded handler that then sleeps and retries forever. | MED |
| E23 | `orchestrator/heartbeat.py:153` | `/proc/stat` btime parse → `return None`; callers cannot distinguish "no source" from "parse bug", weakening PID-reuse detection. | MED |
| E24 | `auto/interview_driver.py:496,523,549` | Three `progress_callback` calls with `except Exception: pass` and **no log**, while `authoring_handlers.py:1627` does the same *with* `log.warning`. Inconsistent fail-open policy. | LOW |
| E25 | `cli/commands/init.py:266,659` | `event_store.close()` swallowed in error paths; the triggering exception is only `print_warning`ed, never logged with a traceback. | LOW |
| E26 | `codex/artifacts.py:499,531,571` · `hermes/artifacts.py:80,111,270,363` | Un-annotated `except BaseException` — swallowing `CancelledError`/`KeyboardInterrupt` here would make Ctrl-C ineffective mid-artifact-install. | MED |
| E27 | `core/retry.py:44-79` | `retry_async` has only **3 consumers** because it retries on exception type only and has no notion of `Result.err` — the codebase's dominant error channel. Design gap, not adapter sloppiness. | MED |
| E28 | `providers/copilot_cli_adapter.py:952` | `asyncio.sleep(2**attempt)` — unbounded growth, no cap, no jitter. | MED |
| E29 | `providers/opencode_adapter.py:491` | `asyncio.sleep(min(attempt * 2, 10))` — third distinct backoff policy. | MED |
| E30 | `providers/goose_cli_adapter.py:375` | **No sleep at all** — hot-loops the CLI `max_retries` times on JSON-format validation failure. | MED |
| E31 | `core/retry.py:52` | `wait_jitter` defaults to `0.0` and **no caller sets it** → concurrent generations retry in lockstep against a provider that just rate-limited them. | MED |
| E32 | `core/retry.py:16-31` | Substring transient matching: `"500"` matches a token count like `"15000"`; `"rate"` matches `"generate"`/`"iterate"`/`"accurate"`. Deterministic errors are retried to exhaustion. | MED |
| E33 | `core/retry.py:63-72` | `retry_async` logs nothing on retry, so its 3 consumers produce no structlog signal for retry storms — while the hand-rolled loops do. | LOW |
| E34 | `core/file_lock.py` | `.lock` files are created and never removed; no stale-lock sweep and no timeout on the POSIX path. `auto_handler.py:1273` locks per-session, so they accumulate one per session forever. | LOW |
| E35 | `core/file_lock.py:38-47,141` | If any of six `os` capability probes fails at import, `_lock_parent_authority` raises `ENOTSUP` unconditionally instead of falling back to plain `flock` — checkpointing, interview state, PM snapshots and worktree locking all hard-fail. | LOW |

---

## F. Duplication (12)

| # | Location | Issue | Sev |
| ---: | :--- | :--- | :--- |
| F1 | `copilot_permissions.py` vs `codex_permissions.py` | **~83% clone.** Identical mode alias, `_VALID_PERMISSION_MODES`, byte-identical `_PERMISSION_MODE_TO_SANDBOX`, identical resolve/build/delegate functions. Only the 3-entry flag table differs. | MED |
| F2 | `claude_permissions.py` | Third copy of the same shape, reduced form. **Zero `src/` importers** — test-only surface. | MED |
| F3 | permission trio | Only codex logs the security-relevant bypass event with full telemetry (`source`, `permission_mode`, `default_mode`, `resolved_mode`); copilot and claude log strictly less for the same event. | MED |
| F4 | `parallel_executor.py:12161` ≈ `runner.py:2116` | Route-observation replay validation duplicated, diverging in exception type **and** enum type (= B15). | HIGH |
| F5 | `parallel_executor.py:11395` ≈ `runner.py:1783` | BLOCKED fallback duplicated, diverging in failure classification (= B14). | HIGH |
| F6 | `runner.py:9455 / 11433 / 11657` | Message-consumption loop triplicated **within one file**; drift and tool-event emission present in only one copy (= B3). | HIGH |
| F7 | `parallel_executor.py:3143` vs `runner.py:1200` | `_announce_param_degradations` differs **only** in a log-event string literal. Also `_message_tool_input_preview` and `_terminate_runtime_handle` duplicated, with no shared base or mixin. | MED |
| F8 | `core/conductor.py:24` = `core/session_signal.py:27` | Byte-identical `_SECRET_PATTERNS` (= C8). Also duplicated invisible-Unicode `unicodedata.category` logic. | MED |
| F9 | `core/hitl_state.py` vs `hitl_resume.py` | Near-identical `_required_str`/`_optional_str` extraction layers that have **already diverged** (`_optional_int` raises on a float in one, passes through in the other). | MED |
| F10 | `core/owner_only.py:199-226` | `_write_atomic_unscoped` is a verbatim clone of `write_owner_only` minus one `S_IMODE` check — two copies of a durability-critical fsync/replace sequence. | LOW |
| F11 | `core/ac_tree.py:83-149` | Five `with_*` methods each re-list all nine fields by hand instead of `dataclasses.replace`; `with_atomic` has already diverged by recomputing `status`. | LOW |
| F12 | 5 retry loops | `copilot:952`, `opencode:491`, `codex:1334`, `goose:375`, `auto/pipeline:2040` — five distinct backoff policies for one concern (= E27-E33). | MED |

---

## G. Dead code & hygiene (21)

Correction to a common assumption: `secondary/`, `rlm/`, `openclaw/`, `routing/`,
and `src/ouroboros/integrations/` are **not** dead code — they are *already-deleted*
packages whose only remaining trace is stale `__pycache__` bytecode. `git ls-files`
returns zero tracked files for each.

| # | Location | Issue | Sev |
| ---: | :--- | :--- | :--- |
| G1 | `src/ouroboros/routing/__pycache__` | Deleted package; 18 `.pyc` for `complexity`/`downgrade`/`escalation`/`__init__`. Makes `routing/` look live next to the real `router/`. Stale `.pyc` can be **importable** on a matching interpreter, so a resurrected import works locally and fails in CI. | MED |
| G2 | `src/ouroboros/secondary/__pycache__` | Deleted package, 9 `.pyc`. | LOW |
| G3 | `src/ouroboros/rlm/__pycache__` | Deleted package, 20 `.pyc`. | LOW |
| G4 | `src/ouroboros/openclaw/__pycache__` | Deleted package, 5 `.pyc`. | LOW |
| G5 | `src/ouroboros/integrations/__pycache__` | Deleted package, 2 `.pyc`. Not a duplicate of root `/integrations`. | LOW |
| G6 | `project-context.md:68` | Documents `from ouroboros.routing.router import PALRouter` — a **doubly-dead** path; neither `routing` nor `PALRouter` exists. | MED |
| G7 | `tests/test-execution-plan.md:234` | Test-selection matrix still lists `src/ouroboros/routing/**`. | LOW |
| G8 | `core/security.py:549` | `truncate_input` — 0 `src/` callers (5 test refs). | LOW |
| G9 | `core/security.py:486` | `mask_sensitive_value` — 0 `src/` callers (7 test refs); `observability/logging.py` imports its sibling but not this. | LOW |
| G10 | `core/ac_tree.py:264,281,289,297,330` | `get_path`, `get_leaves`, `get_atomic_nodes`, `get_pending_nodes`, `is_cyclic` — five public methods, **0 `src/` callers**; the tests test only themselves. | LOW |
| G11 | `core/ac_tree.py:330` | `is_cyclic` is documented as a cycle check but only compares two content strings for equality — **it cannot detect a cycle**, and nothing calls it. | MED |
| G12 | `core/control_contract.py:20` | `CANONICAL_CONTROL_TARGET_TYPES` — 0 references anywhere including tests, while `target_type` is validated only for non-emptiness. | LOW |
| G13 | `core/security.py:418` | `validate_api_key_format` referenced only by the lazy-export table (= C9). | LOW |
| G14 | `telemetry.py` (1412 lines) | Largest top-level module; conceptually overlaps the existing `observability/` package. | MED |
| G15 | `src/ouroboros/*.py` (11 files) | Top-level sprawl, 3421 lines: `claude_permissions`, `codex_permissions`, `copilot_permissions`, `orchestrator_stage`, `package_profiles`, `project_map`, `ralph_loop`, `runtime_instruction_artifacts`, `sandbox`, `telemetry`, `zcode_cli_launcher`. Only `sandbox.py` (34 lines) belongs — it breaks a real cycle. | MED |
| G16 | `resul.log` | Stray typo'd file at repo root (caught incidentally by `*.log`, not an explicit rule). | LOW |
| G17 | `.ouroboros_eval_artifact.md` | `.gitignore` carries an explicit anti-regression comment: "recurring stray — has been accidentally committed before". | LOW |
| G18 | `core/__init__.py:1-8` | Docstring states the package "uses lazy re-exports so … importing submodules does not … create circular import chains". The cycles are real and permanent; the mitigation moves failures from import time to first call. | MED |
| G19 | `core/ontology_questions.py:33-35,410-412` · `seed_verify_gate.py:80` | Function-local imports as cycle workarounds — import errors surface as runtime errors. | MED |
| G20 | `core/git_workflow.py:36-46,55-84` | Workflow policy regex-scraped from prose with **no negation handling** ("do *not* create a branch" enables branch mode). Docstring claims parent-directory traversal the code does not implement, and only reads `CLAUDE.md` though this repo's instructions live in `AGENTS.md`. | MED |
| G21 | `core/seed.py:865` | An "after" validator rewrites the frozen `acceptance_criteria`, making the `else: str(criterion)` branches in `_serialize_acceptance_criteria` and `seed_verify_gate.py:40` **unreachable dead code** — two modules carry contradictory assumptions about one field. | LOW |

---

## H. Validation gaps (14)

| # | Location | Issue | Sev |
| ---: | :--- | :--- | :--- |
| H1 | `core/seed.py:460` | `AcceptanceCriterionSpec.description` has **no `min_length`** (unlike `Seed.goal`). `"   "` validates to `""`, so `derive_semantic_ac_key` hashes a **constant** — every blank AC shares one identity, and AC-keyed state cross-contaminates between unrelated criteria. | HIGH |
| H2 | `core/seed.py:412-415` | `SeedMetadata` degradation invariants are documented but **unenforced**. `degraded=False, generation_mode="partial_seed_from_evidence"` validates cleanly → a partial Seed passes the run gate and auto-RUNs, the exact outcome the field exists to prevent. | HIGH |
| H3 | `core/seed.py:416` | `decision_provenance: dict[str, int]` is a **live mutable dict on a `frozen=True` model** — the only unprotected field. Values unbounded (negatives validate), keys unvalidated despite a documented closed vocabulary. | MED |
| H4 | `core/seed.py:737` | `task_type` is a free-form `str` documented as a closed 7-value set, while the same file defines proper `Literal`s for `InvestmentLevel`. A typo silently changes execution routing. | MED |
| H5 | `core/seed.py:332` | `ContextReference.role` (`'primary'\|'reference'`) is a bare `str` — and it controls whether the runtime **modifies** that codebase. An unrecognized value falls through to a default branch. | MED |
| H6 | `core/seed.py:349` | `BrownfieldContext.project_type` (`'greenfield'\|'brownfield'`) — bare `str`. | LOW |
| H7 | `core/seed.py:299` | `OntologyField.field_type` (`string\|number\|boolean\|array\|object`) — bare `str`. | LOW |
| H8 | `core/seed.py:144` | `expected_artifact_workspace_path_error` calls `os.pathconf` and reads `os.name` → **validation depends on the host filesystem**. The same Seed passes on Linux and fails on Windows CI for reasons unrelated to its content. | MED |
| H9 | `core/seed.py:783,802` | `_coerce_string_evaluation_principles` fabricates `name=f"principle_{index}"` and duplicates prose into both `description` and `criteria`. Nothing can distinguish a fabricated name from an authored one, so output referencing "principle_3" is meaningless to the user. | LOW |
| H10 | `core/session_signal.py:148-151` | `bounded_session_signal_reply` passes `max_bytes=max(MAX_REPLY_BYTES, len(value.encode()))` — the ceiling is derived from its own input, so it **can never reject**. It looks like a validator and is a truncator. | LOW |
| H11 | `core/control_contract.py:79` | `target_type` validated only for non-emptiness while `CANONICAL_CONTROL_TARGET_TYPES` exists and is unused (= G12). | LOW |
| H12 | `core/hitl_state.py:213` | `timeout_seconds` passed through unchecked, while the sibling extraction layer validates it. | LOW |
| H13 | `core/ac_tree.py:186` | `add_node` rejects neither negative depth, nor a missing `parent_id`, nor `depth != parent.depth + 1` (= B20). | MED |
| H14 | `core/ac_tree.py:373-401` | `from_dict` writes `tree.nodes[id]` directly, bypassing the `max_depth` cap → a persisted tree of depth 40 loads cleanly. | MED |

---

## Proposed PR decomposition

Each row is an independent, separately reviewable PR.

| PR | Scope | Items | Risk |
| :--- | :--- | :--- | :--- |
| 1 | Restore drift measurement | B1, B2, B3 | low |
| 2 | Fix secret leak in log sanitization | C1/B12, C2 | low |
| 3 | Fix `file_lock` release ordering + lock leak | B5 | low |
| 4 | Make `is_on_protected_branch` fail closed | B7 | low |
| 5 | Enforce `Seed` validation invariants | H1, H2, H3, H4 | med |
| 6 | Stop discarding tournament winner's diff | B8, E1 | low |
| 7 | Fix TUI rewind dirty-tree bypass | B9, E2, E3 | low |
| 8 | Add missing subprocess timeouts | E4, E5, E6, E7 | low |
| 9 | Unify permission modules | F1, F2, F3 | med |
| 10 | Remove stale bytecode for deleted packages | G1-G7 | low |
| 11 | Fix `retry.py` transient matching + jitter | E31, E32, E33 | low |
| 12 | Restore `skill_dispatcher` on MCP worker backends | A3 | low |
| 13 | Make `session_signal` round-trippable | B10 | low |
| 14 | Fix `seed_verify_gate` fail-open | B11 | low |
| 15 | Fix `ACTree` root replacement + shared metadata | B20, B21, H13, H14 | med |
