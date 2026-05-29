# AgentOS Release Readiness

This note records a dated, non-normative triage snapshot of the live #961
roadmap state. It is intentionally not a second SSOT: if this document
disagrees with [#961](https://github.com/Q00/ouroboros/issues/961), #961 wins.
Do not use this file as release-tag evidence until the snapshot below has been
refreshed against the current #961 issue graph and `origin/main`.

Snapshot used for this pass:

- Snapshot timestamp: 2026-05-29 09:19 KST.
- Local `main` baseline: `837e56a88aad9214e41dc4b948f551510a50f755`
- Refresh note: `origin/main` currently resolves to
  `a2e2411bbd6a9cca9533e2fcda7d881c6acb4056`; this readiness note remains a
  dated #961 triage snapshot and must be refreshed before release tagging.
- Issue scan: public GitHub HTML was reachable, but `gh issue list` could not
  reach `api.github.com` from this environment. The issue source list below is
  compiled from the #961 SSOT body plus the public open-issues HTML page.
- Open PR scan: #1279 and #1280 are approved, clean, and green
- Live refresh note: `gh issue view 961 --repo Q00/ouroboros` could not reach
  `api.github.com` from this environment, and `git fetch origin main --prune`
  could not update `.git/FETCH_HEAD` due local filesystem permissions. The
  available baseline is the local `main` snapshot above, where `origin/main`
  resolves to the same commit.

## Canonical Issue Review Method

This pass reviewed the release-relevant canonical representatives rather than
duplicating #961 as implementation work. The relevant set is:

- Release authority and external gates: #961, #1279, #1280, #1258.
- Track C representatives: #830, #892, #920, #925, #939, #946, #956, #960.
- `ooo auto` authorities and open child/follow-up issues: #772, #809, #1157,
  #1170, #1234, #1254, #1256, #1263, #579, #637, #640, #673, #674, #678, #688,
  #692.
- Linked current/future canonical issues: #518, #573, #575, #614, #615, #725,
  #813, #814, #815, #816, #817, #818, #819, #831, #578, #1139, #1239.

For each issue, the tables below record current status, release impact, and
release disposition. Closed or folded child issues are reviewed separately in
the folded table so they do not create duplicate release work.

## Single Source Issue List

This table is the compiled source list for #961 release-readiness triage. It
keeps the issue graph in one place so later slices can select work without
re-reading every linked issue.

### Open Child / Canonical Issue Enumeration

This is the explicit open-issue inventory for the #961 release-readiness pass.
It includes #961's surviving canonical representatives plus open child or peer
canonical issues that #961 delegates to for Track B, runtime reliability,
plugin/tool integration, observability, security/permissions, recovery, and
release-policy disposition. Direct GitHub HTML checks confirmed #961, #925,
#939, #946, #960, #1157, and #1170 as open during this pass; `gh issue view`
could not reach `api.github.com`, so the rest of the open inventory is carried
forward from the #961 body, linked issue pages, and the prior open-issues HTML
scan recorded for this snapshot.

| Source bucket | Open issues | Why included |
| --- | --- | --- |
| Release authority / policy gate | #961, #1258 | #961 is the AgentOS release SSOT. #1258 is the owner-disposition gate for `ooo auto` throughput claims. |
| Open Track C canonical representatives | #925, #939, #946, #960 | These are the open surviving AgentOS substrate surfaces: long-running runtime reliability, plugin permissions/audit, projection vocabulary, and HITL approval authority. |
| `ooo auto` canonical authority | #772, #1157, #1170 | #772 is the older tactical epic, #1157 is the current `ooo auto` SSOT sibling to #961, and #1170 is the minimal canonical acceptance-test slice used as release evidence. |
| `ooo auto` open child/follow-up issues | #1234, #1254, #1256, #1263, #579, #637, #640, #673, #674, #678, #688, #692 | These are the open child or follow-up items that can affect retry/resume truthfulness, mandatory MCP dispatch, provenance, packaged-skill integration, Product-or-Die policy, and product-completion claims. |
| Linked open canonical/design issues | #518, #573, #575, #614, #615, #725, #813, #814, #815, #816, #817, #818, #819, #831, #578, #1139, #1239 | These are linked canonical or design issues that remain open but are not release-critical unless #961, #1157, or verification promotes a narrow failing slice. |

Open issue count in this release-readiness inventory: 38, excluding PR gates
and closed/folded issues. The direct live checks also showed #920 and #956 as
closed even though they remain canonical historical representatives in #961, so
they stay in the folded/evidence tables rather than the open enumeration above.

| Bucket | Issues / PRs | Source and triage use |
| --- | --- | --- |
| Release authority / external gates | #961, #1279, #1280, #1258 | #961 is the SSOT. PRs and owner-disposition issues are external gates, not duplicate local implementation targets. |
| #961 canonical Track C representatives | #830, #892, #920, #925, #939, #946, #956, #960 | Canonical AgentOS substrate surfaces from #961. Open representatives feed release slices; closed representatives provide verification context only. |
| Open `ooo auto` authority and child/follow-up issues | #772, #1157, #1170, #1234, #1254, #1256, #1263, #579, #637, #640, #673, #674, #678, #688, #692 | Product and runtime-completion lane. Release triage prioritizes truthful resume/retry, mandatory MCP dispatch, grounding/provenance, and documented policy gates. |
| Closed/folded `ooo auto` RFC authority | #809 | Strategic RFC context only; accepted work is represented by current `auto` code and open authority/child issues above. |
| Other open linked canonical/design issues | #518, #573, #575, #614, #615, #725, #813, #814, #815, #816, #817, #818, #819, #831, #578, #1139, #1239 | Keep as current-next or design/future depending on release-path risk; do not promote broad design work unless #961 or a concrete regression requires it. |
| Closed/folded Track C children from #961 | #921-#924, #930-#938, #940-#945, #947-#955, #957-#959, #963-#968 | Reviewed through #961 closure/fold table; each maps to an active home and creates no duplicate release work. |

## Duplicate And Overlap Register

The triaged issue set has no open issue that should be closed as an exact
duplicate during this local release-readiness pass. It does contain intentional
overlap between canonical surfaces. The table below assigns one release owner
for each overlap so follow-up slices do not reimplement the same behavior under
multiple issue numbers.

| Overlap cluster | Canonical owner for release slicing | Overlapping / folded issues | Disposition |
| --- | --- | --- | --- |
| Release authority vs implementation work | #961 | #1279, #1280, #1258 | #961 stays the SSOT. #1279/#1280 are merge/defer gates and #1258 is an owner-disposition gate, not local implementation duplicates. |
| Historical `ooo auto` epic/RFC vs current product authority | #1157 | #772, #809, #678 | Use #1157 for release claims and current slices. #772/#809 remain historical/broad context; #678 is follow-up planning unless #1157 promotes a narrow slice. |
| `ooo auto` resume, retry, dispatch, and handoff safety | #1157, sliced by concrete bug | #579, #637, #673, #674, #688 | Keep these as separate bug-class checks because they verify different failure modes. Do not create a second umbrella issue; promote only the failing concrete path to P0. |
| Product-or-Die policy vs dependent auto-fill implementation | #1256 | #1263 | #1263 is intentionally blocked behind #1256. Do not implement #1263 as release work until the #1256 policy decision lands. |
| Runtime lifecycle, watchdog, and malformed-turn reliability | #925 for current runtime risk; #518/#578 for broader design | #831, #575 | #831 is the concrete malformed-turn risk under #925. #518/#578/#575 remain broader lifecycle/control-journal design unless a current advertised path regresses. |
| Projection read model vs Workflow IR planning graph | #946 for observed records; #956 for planned graph | #930, #932, #933, #936-#938, #941, #943, #944, #947, #948, #950-#954, #957, #959, #968 | Keep the boundary from `projection-v1-scope.md` and `workflow-ir-v1.md`: #946 reads emitted events, #956 validates planned workflow shape. Folded children create no new release slices. |
| Evidence/verifier semantics vs projection/evaluation display | #830/#978 evidence spine; #1234 for verifier matching cleanup | #931, #938, #948, #952, #968 | Evidence policy and verifier acceptance stay under the evidence/verifier owners. Projection issues may display evidence but must not redefine acceptance semantics. |
| Plugin permission/audit contract vs plugin ecosystem management | #939 for release contract; #725 for ecosystem manager design | #934, #949 | Release readiness checks core plugin lifecycle, permission, hook, and audit invariants under #939. #725 remains a future UserLevel plugin-manager surface. |
| HITL authority vs projection/plugin/pre-mutation safety | #960 | #942, #955, #958 | HITL WAIT/RESUME authority belongs to #960. Projection and plugin issues can expose or respect the state, but must not define new authority semantics. |
| Runtime profile, guidance provenance, and effort policy | #573 for profile vocabulary; #614/#615 for future policy | #923 | #923 was folded into these design parents. Keep profile docs/tests separate from external-guidance and reasoning-budget policy work. |
| Multi-agent deliberation and interview lateral-review backlog | #813-#819 as design backlog | #924, #817 | #924 folded into the debate backlog. #817 is the interview recovery/lateral-review member of that backlog, not a release blocker unless #961 or #1157 promotes it. |
| Backend/adapter expansion outside the AgentOS release core | Future integration owners | #1139, #1239, #692 | These are adjacent integration/product incidents, not duplicates of the AgentOS substrate gate. Keep them out of release-critical slices unless current docs or behavior depend on them. |

Release triage rule: when a new failure appears in an overlap cluster, attach
the release slice to the canonical owner above and link the overlapping issue as
context. Only open or implement a second slice when it has a distinct failing
acceptance criterion and verification method.

## Dependency And Blocker Map

The dependency graph below turns the triage inventory into release-ordering
edges. It separates three kinds of blockers:

- **Code blocker:** a failing implementation or test gate that must be fixed in
  this repo before the release claim is true.
- **External blocker:** a maintainer merge/defer or owner disposition decision
  outside this local implementation pass.
- **Policy/design blocker:** a product or architecture decision that prevents
  dependent code from being release-critical until accepted.

### Release dependency edges

| Edge | Dependency / blocker | Dependent issue(s) | Blocker type | Release handling |
| --- | --- | --- | --- | --- |
| #961 -> all AgentOS release claims | #961 is the SSOT for scope, sequencing, and exceptions. | Every issue in this document. | External authority | Reconcile this document against #961 before tagging; if they disagree, #961 wins. |
| #1279/#1280 -> final release declaration | Approved AgentOS/`ooo auto` PR lane must be merged or explicitly deferred. | #961 final readiness claim; any release notes that depend on those PRs. | External blocker | Do not duplicate implementation locally; require maintainer merge/defer disposition. |
| #1258 -> `ooo auto` throughput claims | Owner must accept, close, or defer the throughput risk. | #1157, #1170, #1234, #1254, #579, #637, #640, #673, #674, #678, #688. | External blocker | Release can proceed only if throughput claims are withheld or #1258 is dispositioned. |
| #1256 -> #1263 | Product-or-Die policy must be accepted before aggressive auto-fill behavior is implemented. | #1263. | Policy/design blocker | Keep #1263 out of release-critical code slices until #1256 lands. |
| #1157 -> concrete `ooo auto` slices | #1157 owns the current `ooo auto` product authority and remaining gap narrative. | #1170, #1234, #1254, #579, #637, #640, #673, #674, #678, #688, #692. | Scope dependency | Promote a child issue to P0 only when #1157 or verification shows an advertised-path failure. |
| #1170 -> `ooo auto` evidence claim | The L0 canonical scenario is the minimum acceptance signal. | #1157 release evidence and `ooo auto` readiness notes. | Code blocker if failing | Passing targeted canonical tests make this evidence-only; failure blocks or must be deferred in #961. |
| #637 -> packaged `ooo auto` pipeline trust | Mandatory MCP dispatch must not be bypassed. | #1157, #673, #674, #688. | Code blocker if reproduced | Current integration coverage is evidence; a reproduced bypass promotes #637 to P0 code gate. |
| #579/#674/#688 -> recovery wording and resume safety | Handoff idempotency, MCP/CLI resume bounds, and truthful retry/resume output must stay aligned. | #1157, #1170, release UX/docs. | Code blocker if failing | Treat as one recovery-safety chain for verification, but keep distinct issue owners. |
| #640/#1254 -> auto observability evidence | Provenance and `auto.interview.*` EventStore wiring must be visible for supported paths. | #1157, #1170, #1234. | Code blocker if evidence disappears | Passing EventStore/provenance tests make these current-next follow-ups, not blockers. |
| #925 -> long-running runtime reliability | Long-running MCP/start/status/result and malformed-turn paths must not hang advertised workflows. | #831 and any release-supported MCP background operation. | Code blocker if reproduced | #831 is the concrete known risk; disclose or fix if the affected path remains release-supported. |
| #518/#575/#578 -> broader lifecycle/watchdog design | Durable replay, outbox semantics, and unified watchdog controls remain future design unless current paths regress. | #925, #1157, #674, #688. | Policy/design unless verification fails | Do not block release on broad design work while current AgentProcess/watchdog tests pass. |
| #939 -> plugin/tool boundary readiness | Core plugin lifecycle, permission, hook, and audit contracts must remain enforced. | #725 and any plugin-derived projection/export work. | Code blocker if contract tests fail | #725 remains future ecosystem management; #939 is the release contract owner. |
| #946 -> projection/read-model observability | Projection records must be rebuildable read models over EventStore/journal facts. | #932, #933, #936, #941, #943, #944, #947, #950, #951, #953, #954 and projection consumers. | Code blocker if projection verification fails | Additive projection gaps are P1/P2; failure of current projection tests becomes a release blocker. |
| #956 -> planned workflow/conformance substrate | Workflow IR owns planned graph shape and conformance, separate from #946 observed records. | #930, #937, #957, #959 and #946 boundary tests. | Code blocker if conformance fails | Closed/folded issue; current conformance tests are the release evidence. |
| #946 <-> #956 boundary | IR plans what should run; projection reports what happened. | #938, #948, #952, #968. | Boundary blocker if tests/docs drift | Do not embed projection records into IR or move acceptance semantics into projection. |
| #830/#978 -> verifier/evidence semantics | Evidence schema, retry routing, and TraceGuard-style acceptance semantics own verdict meaning. | #1234, #946 verdict display, #956 schema refs. | Code blocker if verifier tests fail | Projection and IR may reference evidence; they must not redefine acceptance policy. |
| #960 -> HITL approval authority | WAIT/RESUME and approval authority must stay under the HITL contract. | #942, #955, #958, plugin/pre-mutation safety surfaces. | Code blocker if advertised HITL authority fails | Keep broader HITL persistence/resume as P1/P2 unless release docs over-promise it. |
| #573/#614/#615 -> runtime policy vocabulary | Profile taxonomy, external guidance provenance, and reasoning-budget policy remain design gates. | #923 and future runtime policy work. | Policy/design blocker | Do not promote to release code work while locked docs/tests remain coherent. |
| #813-#819 -> deliberation/lateral-review backlog | Symposium/debate/lateral-review work is not on the current AgentOS release path. | #924, #817. | Policy/design blocker | Keep as design backlog unless #961 or #1157 promotes a concrete advertised-path bug. |
| #1139/#1239/#692 -> adjacent integration/product incidents | Backend expansion, interview adapter enhancement, and Discord gateway incident work are outside the core release gate. | Related docs or future product slices only. | Scope dependency | Do not block AgentOS readiness unless current release docs or behavior depend on them. |

### Dependency rollup by release bucket

| Bucket | Issues | Dependency status |
| --- | --- | --- |
| External gates | #961, #1279, #1280, #1258 | Only release-critical remaining blockers identified in this snapshot if verification passes. |
| Independent current-next surfaces | #925, #939, #946, #960 | Can proceed independently; none should wait for `ooo auto` throughput or Product-or-Die policy decisions. |
| `ooo auto` product chain | #1157, #1170, #1234, #1254, #579, #637, #640, #673, #674, #678, #688, #692 | Governed by #1157; #1258 blocks stronger throughput claims; #637 and #1170 become code blockers only on reproduced verification failure. |
| Policy/design-wait items | #1256, #1263, #518, #573, #575, #614, #615, #725, #813, #814, #815, #816, #817, #818, #819, #578, #1139, #1239 | Do not consume release implementation time unless owners promote a narrow slice or verification shows current behavior regressed. |
| Evidence-only folded items | #830, #892, #920, #956, #809, plus the closed/folded Track C issues enumerated in the folded table below | Create no new dependency edges; they point at their active homes in the folded table below. |

Release-blocking conclusion for this snapshot: no unresolved local code
dependency is known after triage. The remaining release blockers are external
or policy gates unless the verification pack promotes one of the current-next
surfaces to a P0 code gate.

## Release Scope And Blocking Status

This table is the Sub-AC 3 handoff: it records the candidate release scope and
blocking or non-blocking status for #961 and every open child/canonical issue
in the #961 release-readiness inventory. "Blocking" means the issue blocks the
release declaration in this snapshot. "Non-blocking" means the issue remains
open but should not stop the candidate while the tied verification remains
green and release notes avoid over-claiming unsupported behavior.

| Issue | Release scope | Blocking status for this candidate |
| --- | --- | --- |
| #961 | In scope as roadmap SSOT and final release authority. | Blocking external gate if #961 conflicts with this snapshot, lacks required dispositions, or changes the accepted release gate. Do not implement #961 directly. |
| #1258 | In scope only for `ooo auto` throughput claim disposition. | Blocking external gate for improved-throughput claims; non-blocking for release if those claims are withheld or the owner explicitly defers/accepts/closes the risk. |
| #925 | In scope for long-running MCP/runtime stability, recovery, cancel/retry, and malformed-turn risk. | Non-blocking current-next while runtime verification passes; promote to blocking code gate on a reproduced advertised-path hang or unsafe recovery state. |
| #939 | In scope for core plugin lifecycle, permission, hook, runtime-boundary, and audit invariants. | Non-blocking current-next while plugin contract tests pass; promote to blocking code gate on permission/audit/hook regression. |
| #946 | In scope for projection/read-model observability needed by release evidence and recovery diagnostics. | Non-blocking current-next while projection verification passes; promote to blocking code gate if required Run/Stage/Step/Artifact/Verdict evidence cannot be reconstructed. |
| #960 | In scope for HITL WAIT/RESUME approval authority and safety wording. | Non-blocking current-next while supported HITL authority works and docs stay bounded; promote to blocking code gate on authority failure or over-claim. |
| #772 | Out of release implementation scope as broad historical `ooo auto` epic. | Non-blocking; use #1157 and concrete child issues for any release slice. |
| #1157 | In scope as the current `ooo auto` product authority and gap narrative. | Non-blocking while documented gaps are truthful and child verification passes; promote only a narrow failing child to blocking code gate. |
| #1170 | In scope as the minimal canonical `ooo auto` acceptance evidence. | Non-blocking if the L0 scenario passes or is explicitly deferred; blocking code gate if it fails without accepted disposition. |
| #1234 | In scope for verifier matching truthfulness in release evidence. | Non-blocking while targeted verifier tests pass; blocking code gate if false evidence is accepted or valid evidence is rejected. |
| #1254 | In scope for `auto.interview.*` EventStore and provenance evidence. | Non-blocking while supported paths emit required events; blocking code gate if release-supported auto paths lose required evidence. |
| #1256 | In scope only as Product-or-Die policy decision context. | Non-blocking policy/design-wait unless owners make the policy mandatory for this candidate. |
| #1263 | Out of release implementation scope until #1256 policy is accepted. | Non-blocking design-wait; do not implement dependent auto-fill behavior for this candidate. |
| #579 | In scope for handoff idempotency and recovery truthfulness. | Non-blocking current-next while contract verification passes; blocking code gate if resume/retry duplicates work or loses state. |
| #637 | In scope for mandatory packaged `ouroboros_auto` MCP dispatch. | Non-blocking current-next while integration coverage passes; blocking code gate if packaged entrypoints bypass MCP. |
| #640 | In scope for provenance and repository-grounding visibility. | Non-blocking current-next unless supported status/provenance rendering hides required evidence. |
| #673 | In scope for packaged-skill driver/brake forwarding. | Non-blocking current-next unless supported packaged invocation drops user controls. |
| #674 | In scope for MCP auto resume bounds and CLI recovery alignment. | Non-blocking current-next unless output gives unsafe or false resume/retry guidance. |
| #678 | In scope as broad `ooo auto` product-completion planning context. | Non-blocking; #1157 must promote a concrete slice before it can block. |
| #688 | In scope for truthful interview-start timeout retry/resume wording. | Non-blocking current-next while resume-capability tests pass; blocking code gate if wording regresses. |
| #692 | Adjacent to release scope only if current docs or behavior depend on the Discord gateway assumptions. | Non-blocking in this candidate; keep separate unless owner promotes a concrete affected path. |
| #518 | Out of release implementation scope as broad AgentProcess lifecycle roadmap. | Non-blocking design/future while current AgentProcess evidence passes. |
| #573 | Out of release implementation scope beyond locked profile taxonomy docs/tests. | Non-blocking design/future unless current profile docs/tests drift. |
| #575 | Out of release implementation scope as broader ControlJournal/outbox design. | Non-blocking design/future unless current recovery/control verification loses durable state. |
| #614 | Out of release implementation scope as external-guidance provenance policy. | Non-blocking policy/design-wait unless owners make it a release claim. |
| #615 | Out of release implementation scope as reasoning-budget policy design. | Non-blocking policy/design-wait until a verifiable release contract is accepted. |
| #725 | Out of release implementation scope as UserLevel plugin-manager ecosystem work. | Non-blocking design/future while #939 covers the core plugin contract gate. |
| #813 | Out of release implementation scope as multi-agent deliberation backlog. | Non-blocking design/future unless #961 or #1157 promotes a narrow slice. |
| #814 | Out of release implementation scope as multi-agent deliberation backlog. | Non-blocking design/future unless #961 or #1157 promotes a narrow slice. |
| #815 | Out of release implementation scope as multi-agent deliberation backlog. | Non-blocking design/future unless #961 or #1157 promotes a narrow slice. |
| #816 | Out of release implementation scope as multi-agent deliberation backlog. | Non-blocking design/future unless #961 or #1157 promotes a narrow slice. |
| #817 | Out of release implementation scope as interview stagnation/lateral-review enhancement. | Non-blocking design/future unless advertised interview milestone behavior depends on it. |
| #818 | Out of release implementation scope as multi-agent deliberation backlog. | Non-blocking design/future unless #961 or #1157 promotes a narrow slice. |
| #819 | Out of release implementation scope as multi-agent deliberation backlog. | Non-blocking design/future unless #961 or #1157 promotes a narrow slice. |
| #831 | In scope as the concrete malformed-turn MCP interview risk under #925. | Non-blocking current-next while the affected path is verified or disclosed; blocking code gate if a supported path hangs or returns unrecoverable malformed state. |
| #578 | Out of release implementation scope as unified watchdog-control design. | Non-blocking design/future while current directive/watchdog verification passes. |
| #1139 | Out of release implementation scope as future backend support. | Non-blocking unless release docs claim that backend as supported. |
| #1239 | Out of release implementation scope as future interview adapter enhancement. | Non-blocking unless current release behavior or docs depend on the enhancement. |

## #961 Review

#961 release-readiness classification: **Release-critical external gate /
roadmap SSOT**.

Rationale: #961 defines the AgentOS release checklist and owns the final
readiness decision, but it is not itself an implementation target. Local code
work must be selected from a narrow child/canonical issue, a reproduced
candidate failure, or an approved release-readiness slice with its own
acceptance criteria. Treating #961 as the code slice would collapse policy,
merge, owner-disposition, and implementation work into one untestable task.

Blocking implication: #961 blocks the release declaration when its checklist,
labels, or owner comments disagree with this snapshot, or when it has not
recorded the required external dispositions. It does not block by requiring
duplicate local implementation of approved PR work or unresolved policy/design
items. Those remain external gates or design-wait items until an owner promotes
a concrete failing slice.

| Field | Triage |
| --- | --- |
| Current status | Open SSOT roadmap for AgentOS release readiness. The local #961-aligned evidence says the baseline metrics gate is captured and Track A wiring is complete enough to use as a release gate input. |
| Scope | Coordinate AgentOS readiness only: fat-harness evidence, runtime/process wiring, projection/IR boundaries, plugin/tool integration readiness, verification evidence, and disposition of child/canonical issues. |
| Out of scope | Do not implement #961 directly. Implementation must land through narrow child issues, approved PRs, or local release-readiness fixes with their own acceptance criteria. |
| Blockers | Approved PR lane must be resolved or explicitly deferred (#1279/#1280 at this snapshot). #1258 needs owner disposition before claiming improved `ooo auto` throughput. Live GitHub status refresh is unavailable in this environment and must be repeated before tagging. |
| Release impact | #961 is the release authority. If it disagrees with this document or later owner comments, #961 wins. A release can proceed only when code gates pass and remaining items are external/policy dispositions rather than unresolved release-critical implementation defects. |
| Next action | Keep #961 as the roadmap SSOT, merge or defer approved AgentOS/`ooo auto` PRs, get owner disposition on #1258, rerun the verification pack on the candidate commit, and record any exceptions in #961 or release notes. |

## Release Gate

Do not call an AgentOS release ready until all of these are true:

1. #961 still shows `baseline-metrics-captured` and the Track A wiring gate is
   complete.
2. Current open PRs that carry approved AgentOS or `ooo auto` fixes are either
   merged or explicitly deferred in #961.
3. `needs-design` / `needs-approval` issues are not treated as implicit code
   blockers unless #961 or their own body marks them as release blocking.
4. The local verification pack below passes on the candidate commit.
5. Any exception to the gate is recorded in #961 or in the release notes with a
   link to the owning issue.

## Required Verification Pack

The verification pack is the release candidate evidence bundle. It has four
layers:

1. **Universal repo gates:** formatting, typing, and the full test suite that
   every release candidate must pass.
2. **AgentOS smoke gates:** focused tests for the current #961 release-critical
   surfaces before spending time on the full suite.
3. **Slice-specific gates:** targeted tests or manual checks tied to a promoted
   narrow release slice.
4. **External-gate evidence:** links or release-note entries proving that
   maintainer merge/defer and owner-disposition gates were resolved outside
   local code.

Each verification-pack entry must name the covered release slice, the owning
issue or gate, the acceptance criterion it proves, and the command or manual
evidence used. A slice moves from current-next to release-blocking only when
its verification entry fails on the candidate commit or #961 explicitly
promotes it.

### Verification Pack Slice Coverage

| Pack entry | Covered release-critical slice | Owner / source | Acceptance criterion | Verification method |
| --- | --- | --- | --- | --- |
| Universal repo gates | Candidate-wide implementation health. | #961 release gate | The candidate has no unexplained lint, type, or test regression. | `uv run ruff check .`; `uv run mypy src`; `uv run pytest`. |
| AgentOS smoke: `ooo auto` evidence and verifier path | Canonical `ooo auto` release evidence, mandatory dispatch, provenance, and verifier truthfulness. | #1157, #1170, #1234, #1254, #637 | Supported `ooo auto` paths emit truthful evidence, preserve mandatory MCP dispatch, and do not accept false verifier results. | Focused `uv run pytest` commands under "AgentOS-focused smoke"; promote a failing issue to P0 code gate if unreconciled. |
| AgentOS smoke: long-running runtime and recovery | Start/status/result behavior, recovery/cancel/retry truthfulness, and malformed-turn risk. | #925, #579, #674, #688, #831 | Supported runtime paths must not hang, duplicate work, lose recoverable state, or print unsafe resume/retry guidance. | AgentProcess and orchestration smoke tests under "AgentOS-focused smoke"; manual retry/resume evidence if a release note exception is needed. |
| AgentOS smoke: plugin/tool integration and permissions | Plugin lifecycle, hook, runtime-boundary, permission, and audit invariants. | #939 | Supported plugin/tool paths enforce permission and audit contracts and do not bypass runtime boundaries. | `tests/integration/plugin` and `tests/unit/plugin`; failure promotes #939 to P0 code gate. |
| AgentOS smoke: projection and workflow evidence boundary | Projection read models, Workflow IR conformance, and the boundary between observed records and planned graph shape. | #946, #956, #830/#978 | Release evidence can reconstruct required Run/Stage/Step/Artifact/Verdict facts without moving acceptance semantics into projection or IR. | `tests/conformance/workflow_ir` plus projection/verifier tests covered by the full suite. |
| AgentOS smoke: HITL authority and safety | WAIT/RESUME approval authority and safety wording for supported HITL paths. | #960 | Supported HITL authority remains explicit, bounded, and truthful; docs must not over-claim unverified persistence. | Full-suite HITL tests and any release-note exception linked to #960. |
| Selected local slice: health diagnostic copyability | `ooo health` / `ouroboros status health` release setup and recovery UX. | Local release-readiness slice tied to #961 | Health diagnostics preserve existing statuses and exit semantics, keep long details copyable in full, avoid secret disclosure, and reflect backend/LLM overrides. | Commands listed in "Selected Release Slice Verification Method" plus manual `ouroboros status health` check. |
| External-gate evidence | Remaining non-code blockers. | #961, #1279, #1280, #1258 | Approved PR work is merged or explicitly deferred, and `ooo auto` throughput claims are withheld or owner-dispositioned. | GitHub/#961 or release-note links recorded in "Remaining External Release Blockers". |

### Verification Evidence Locations And Names

Store release-candidate evidence under a candidate-specific run directory so
later reviewers can connect every pass/fail claim to the exact commit and
slice:

```text
.ouroboros/release-evidence/agentos/<YYYYMMDD>-<short-main-sha>/
```

For this snapshot, the expected directory prefix is:

```text
.ouroboros/release-evidence/agentos/20260529-837e56a/
```

Use one subdirectory per verification-pack slice. File names must keep the
slice id, gate id, and attempt number stable:

```text
<slice-id>/<NN>-<gate-id>-attempt<M>.<ext>
```

Allowed extensions:

- `.log` for raw command output, including stdout and stderr.
- `.json` for structured command output or exported status payloads.
- `.md` for human summary notes, manual observations, and exception
  dispositions.
- `.png` for screenshots when the evidence is visual.

Do not store secrets in evidence. Redact API keys, tokens, credential values,
and private paths that are not needed to reproduce the result. If a command
requires a sandbox workaround, record the environment prefix in the summary,
for example `HOME=/private/tmp/ouroboros-home`,
`UV_CACHE_DIR=/private/tmp/ouroboros-uv-cache`, `UV_OFFLINE=1`, or an unset
inherited variable.

| Slice id | Covered pack entry | Evidence location | Required evidence files |
| --- | --- | --- | --- |
| `universal-repo-gates` | Universal repo gates | `.ouroboros/release-evidence/agentos/<candidate>/universal-repo-gates/` | `01-ruff-check-attempt1.log`, `02-mypy-src-attempt1.log`, `03-pytest-attempt1.log`, plus `summary.md` with final pass/fail disposition. |
| `ooo-auto-evidence-verifier` | `ooo auto` evidence and verifier path | `.ouroboros/release-evidence/agentos/<candidate>/ooo-auto-evidence-verifier/` | One `.log` per focused pytest command and `summary.md` listing covered issues #1157, #1170, #1234, #1254, and #637. |
| `runtime-recovery` | Long-running runtime and recovery | `.ouroboros/release-evidence/agentos/<candidate>/runtime-recovery/` | AgentProcess/orchestration pytest logs, any manual retry/resume transcript as `.md`, and `summary.md` listing any #925/#579/#674/#688/#831 disclosure. |
| `plugin-permissions` | Plugin/tool integration and permissions | `.ouroboros/release-evidence/agentos/<candidate>/plugin-permissions/` | Plugin integration/unit test logs and `summary.md` stating whether any #939 permission, hook, audit, or runtime-boundary exception remains. |
| `projection-workflow-boundary` | Projection and workflow evidence boundary | `.ouroboros/release-evidence/agentos/<candidate>/projection-workflow-boundary/` | Workflow IR conformance logs, projection/verifier logs when run separately, and `summary.md` confirming observed projection facts did not redefine evidence semantics. |
| `hitl-authority` | HITL authority and safety | `.ouroboros/release-evidence/agentos/<candidate>/hitl-authority/` | HITL-focused test logs when run separately, or `summary.md` pointing to the full-suite log section and any #960 release-note exception. |
| `health-diagnostic-copyability` | Selected local `ooo health` slice | `.ouroboros/release-evidence/agentos/<candidate>/health-diagnostic-copyability/` | `01-status-unit-attempt1.log`, `02-status-health-e2e-attempt1.log`, `03-status-ruff-attempt1.log`, `04-mypy-src-attempt1.log`, `05-manual-health-output-attempt1.md`, and `summary.md`. |
| `external-gates` | Remaining non-code blockers | `.ouroboros/release-evidence/agentos/<candidate>/external-gates/` | `summary.md` with links or copied dispositions for #961, #1279, #1280, and #1258. Use one additional `<issue-or-pr>-disposition.md` file per gate if the disposition is too long for the summary. |

The final release handoff should not paste full logs into this document.
Instead, record the candidate directory, the final `summary.md` outcome for
each slice, and any remaining exception in #961 or release notes. If a slice is
rerun after a fix, keep the earlier attempt files and increment `attempt<M>` so
the recovery path remains auditable.

Run these before tagging a release candidate:

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

For a faster AgentOS-focused smoke before a full run:

```bash
uv run pytest \
  tests/unit/auto/test_interview_driver_event_store_wiring.py \
  tests/unit/orchestrator/test_parallel_executor.py \
    -k 'command_claim or gradle or node_id or fabrication or tests_passed'

uv run pytest \
  tests/integration/orchestrator/test_agent_process_three_surface_acceptance.py \
  tests/integration/plugin \
  tests/unit/plugin \
  tests/conformance/workflow_ir
```

## Selected Release Slice Verification Method

Selected slice: `ooo health` / `ouroboros status health` diagnostic
truthfulness for release setup and recovery. This is the only local
release-readiness implementation slice selected from the current triage because
it is narrow, directly user-facing, and does not duplicate #961, #1279, #1280,
or #1258 external gates.

Acceptance criteria for this slice:

1. The health table still reports configuration, database, runtime backend, and
   credential checks with existing `ok`, `warning`, and `error` exit semantics.
2. Long diagnostic details such as config paths, database paths, missing CLI
   paths, and credential-file paths remain copyable in full after table
   rendering; a terminal-width fold or table truncation must not be the only
   source of truth.
3. Health output must not print secret values. It may name the relevant
   environment variable or credential provider, but not the configured API key.
4. Runtime and LLM environment overrides used by AgentOS release setup remain
   reflected in the effective backend checks.

Verification commands:

```bash
uv run pytest tests/unit/cli/test_status.py
uv run pytest tests/e2e/test_cli_commands.py -k status_health
uv run ruff check src/ouroboros/cli/commands/status.py tests/unit/cli/test_status.py
uv run mypy src
```

Manual verification, when preparing the final release candidate:

```bash
ouroboros status health
```

The manual output passes when the Rich table is readable and each row with a
detail also has a following plain-text line in the form
`<check name>: <status> - <full detail>`. Use that plain-text detail line as the
copy/paste evidence in #961 or release notes when a user's local health check
fails.

## Issue Inventory And Priority

Priority key:

- P0 external gate: release decision or merge/disposition outside this local
  implementation pass.
- P0 code gate: release-critical local code or verification defect.
- P1 current-next: narrow slice worth doing next, but not a stop-ship item if
  explicitly deferred.
- P2 design/future: keep open for owner planning; do not implement during this
  release-readiness pass.
- Done/folded: closed, merged, or represented by a canonical parent.

Priority assignment criteria, in order:

1. Long-running stability, recovery, cancel, retry, or malformed-turn behavior
   in an advertised release path is P0 code gate if currently failing, P1 if
   open but bounded by passing verification, and P2 if it is broad future
   lifecycle design.
2. Tool/plugin integration defects that can bypass permissions, auditability,
   hook contracts, or plugin/runtime boundaries are P0 code gate when
   reproduced, P1 while contract tests pass but umbrella work remains, and P2
   for ecosystem-manager design outside the core release path.
3. Observability and projection gaps are P0 code gate only when they prevent
   release verification or recovery evidence; otherwise current projection
   vocabulary follow-up is P1 and additive exports/read models are P2.
4. Security, permissions, HITL authority, and policy issues are P0 external
   gate when they require maintainer/product disposition, P0 code gate when a
   tested safety invariant fails, P1 when release docs could over-advertise a
   partially tested path, and P2 when the issue is unresolved policy design.
5. UX and documentation items are P1 only when they affect a release-supported
   workflow; otherwise they remain P2 unless the documentation is materially
   false under the contributing severity rubric.

Canonical priority rollup:

| Priority | Canonical issues |
| --- | --- |
| P0 external gate | #961, #1279, #1280, #1258 |
| P0 code gate | None identified in the local snapshot; promote any failed verification-pack item here. |
| P1 current-next | #925, #939, #946, #960, #1157, #1170, #1234, #1254, #579, #637, #640, #673, #674, #678, #688, #692, #831 |
| P2 design/future | #772, #1256, #1263, #518, #573, #575, #614, #615, #725, #813, #814, #815, #816, #817, #818, #819, #578, #1139, #1239 |
| Done/folded | #830, #892, #920, #956, #809, #921-#924, #930-#938, #940-#945, #947-#955, #957-#959, #963-#968 |

### Mutually Exclusive Issue Classification

Each identified issue or PR is assigned to exactly one release-readiness
category below. The categories are operational buckets for this release pass;
conditional promotion rules are described after the table, but they do not make
an issue belong to multiple categories in this snapshot.

| Category | Issues / PRs | Count |
| --- | --- | ---: |
| P0 external gate | #961, #1279, #1280, #1258 | 4 |
| P0 code gate | None identified in the local snapshot | 0 |
| P1 current-next | #925, #939, #946, #960, #1157, #1170, #1234, #1254, #579, #637, #640, #673, #674, #678, #688, #692, #831 | 17 |
| P2 design/future | #772, #1256, #1263, #518, #573, #575, #614, #615, #725, #813, #814, #815, #816, #817, #818, #819, #578, #1139, #1239 | 19 |
| Done/folded | #830, #892, #920, #956, #809, #921, #922, #923, #924, #930, #931, #932, #933, #934, #935, #936, #937, #938, #940, #941, #942, #943, #944, #945, #947, #948, #949, #950, #951, #952, #953, #954, #955, #957, #958, #959, #963, #964, #965, #966, #967, #968 | 42 |

Classification invariant for this snapshot: the table assigns 82 total items
with no duplicate assignments. The open issue inventory is 38 issues excluding
PR gates and closed/folded issues; adding the 2 PR gates and 42 done/folded
items yields the 82-item release-readiness review set.

### Open Child / Canonical Classification Rationale

This matrix is the per-open-issue handoff for #961's child and canonical issue
graph. Each open issue is assigned to exactly one release-readiness bucket in
this snapshot. The promotion trigger is the condition that would change the
classification before tagging a release candidate.

| Issue | Bucket | Rationale | Promotion or release trigger |
| --- | --- | --- | --- |
| #961 | P0 external gate | Roadmap SSOT for AgentOS readiness; it defines scope, sequencing, and accepted exceptions. | Blocks release declaration on any conflict with this snapshot. Reconcile in #961, not by implementing #961 directly. |
| #1258 | P0 external gate | Owner/product disposition is required before making improved `ooo auto` throughput claims. | Release notes must withhold throughput claims until the owner accepts, closes, or explicitly defers the risk. |
| #925 | P1 current-next | Open runtime/MCP reliability umbrella for long-running start/status/result, cancel/retry, and malformed-turn risks. | Promote to P0 code gate if an advertised MCP/runtime path hangs, loses recovery state, or returns false retry/resume guidance. |
| #939 | P1 current-next | Open plugin lifecycle, permission, hook, and audit umbrella; core contract coverage exists but follow-up surface remains. | Promote to P0 code gate on permission bypass, missing audit evidence, hook-contract regression, or plugin/runtime boundary failure. |
| #946 | P1 current-next | Open projection/read-model vocabulary surface needed for observability and recovery evidence. | Promote to P0 code gate if release verification cannot reconstruct required Run/Stage/Step/Artifact/Verdict facts. |
| #960 | P1 current-next | Open HITL WAIT/RESUME approval-authority surface; safety-relevant but broader persistence is not fully advertised as complete. | Promote to P0 code gate if supported approval, WAIT, or RESUME authority fails, or if release docs over-claim unsupported HITL persistence. |
| #772 | P2 design/future | Broad historical `ooo auto` epic is superseded for release handling by #1157 and narrower children. | Promote only if #1157 or #961 names a concrete child slice as release-blocking. |
| #1157 | P1 current-next | Current `ooo auto` product authority; owns gap narrative and release evidence but does not itself force all future product work into this candidate. | Promote a narrow child to P0 if canonical acceptance, recovery, dispatch, provenance, or product-completion evidence fails. |
| #1170 | P1 current-next | Minimal canonical `ooo auto` acceptance-test slice used as release evidence. | Promote to P0 code gate if the candidate cannot pass or explicitly defer the L0 canonical scenario. |
| #1234 | P1 current-next | Verifier matching cleanup has release relevance, but targeted tests cover the current candidate behavior. | Promote to P0 code gate if verifier matching accepts false evidence or rejects valid release evidence. |
| #1254 | P1 current-next | EventStore `auto.interview.*` wiring/provenance affects observability and recovery evidence. | Promote to P0 code gate if supported auto paths stop emitting required interview/provenance events. |
| #1256 | P2 design/future | Product-or-Die is an unresolved product/policy invariant, not a settled implementation contract. | Promote to P0 external gate if owners require it for release; promote dependent code only after the policy is accepted. |
| #1263 | P2 design/future | Aggressive auto-fill implementation depends on the unresolved #1256 policy decision. | Promote only after #1256 lands and the remaining work is a narrow, verifiable implementation slice. |
| #579 | P1 current-next | Handoff idempotency is recovery-relevant, but current contract tests are the release evidence. | Promote to P0 code gate if resume/retry duplicates execution, loses state, or produces an unsafe handoff. |
| #637 | P1 current-next | Mandatory packaged `ouroboros_auto` MCP dispatch is release-relevant for supported `ooo auto` paths. | Promote to P0 code gate if packaged entrypoints can bypass MCP while claiming the supported path. |
| #640 | P1 current-next | Provenance and repository-grounding improve trust in generated auto answers; current release paths remain bounded by tests/docs. | Promote to P0 code gate if supported status/provenance rendering hides required evidence. |
| #673 | P1 current-next | Packaged-skill driver/brake forwarding affects user control of `ooo auto` entrypoints. | Promote to P0 code gate if supported packaged invocation drops driver or brake controls. |
| #674 | P1 current-next | MCP auto resume bounds must stay aligned with CLI recovery semantics. | Promote to P0 code gate if retry/resume output becomes false or unsafe after partial runs. |
| #678 | P1 current-next | Broad autopilot follow-up tracker remains useful planning context under #1157. | Promote only when #1157 identifies a concrete release-blocking child slice. |
| #688 | P1 current-next | Truthful resume/retry wording affects user recovery after interview-start timeouts. | Promote to P0 code gate if output says `Resume:` where only retry is valid, or hides a valid resume path. |
| #692 | P1 current-next | Discord gateway incident follow-up is adjacent to product reliability but outside the core release gate. | Promote only if current AgentOS or `ooo auto` release docs/behavior still depend on the affected gateway assumptions. |
| #518 | P2 design/future | Broad AgentProcess spawn/pause/resume/cancel/replay roadmap extends beyond the current verified runtime slice. | Promote if current AgentProcess acceptance evidence regresses or owners select a narrow lifecycle slice for release. |
| #573 | P2 design/future | Profile taxonomy cleanup is future planning while locked docs/tests remain coherent. | Promote after owners accept a narrower schema/config migration contract or current docs/tests drift. |
| #575 | P2 design/future | ControlJournal/outbox design is broader recovery architecture, not required while current control paths verify. | Promote if verification shows current recovery/control paths lose durable state. |
| #614 | P2 design/future | External guidance provenance is a policy/security design gate for future prompt guidance. | Promote only when owners make guidance provenance a release claim or current behavior depends on it. |
| #615 | P2 design/future | Reasoning-budget policy is future runtime-cost governance, not a current code gate. | Promote only after policy acceptance defines a verifiable release contract. |
| #725 | P2 design/future | UserLevel plugin manager expands ecosystem operations beyond the core #939 plugin contract. | Promote if #961 makes plugin-manager operations part of the release-supported surface. |
| #813 | P2 design/future | Multi-agent deliberation backlog item outside the current AgentOS release path. | Promote only if #961 or #1157 selects a concrete deliberation slice. |
| #814 | P2 design/future | Multi-agent deliberation backlog item outside the current AgentOS release path. | Promote only if #961 or #1157 selects a concrete deliberation slice. |
| #815 | P2 design/future | Multi-agent deliberation backlog item outside the current AgentOS release path. | Promote only if #961 or #1157 selects a concrete deliberation slice. |
| #816 | P2 design/future | Multi-agent deliberation backlog item outside the current AgentOS release path. | Promote only if #961 or #1157 selects a concrete deliberation slice. |
| #817 | P2 design/future | Interview stagnation/lateral-review enhancement is useful UX/recovery work but not required for the current candidate. | Promote if an advertised interview milestone path depends on this enhancement. |
| #818 | P2 design/future | Multi-agent deliberation backlog item outside the current AgentOS release path. | Promote only if #961 or #1157 selects a concrete deliberation slice. |
| #819 | P2 design/future | Multi-agent deliberation backlog item outside the current AgentOS release path. | Promote only if #961 or #1157 selects a concrete deliberation slice. |
| #831 | P1 current-next | Concrete malformed-turn MCP interview risk under #925; release-relevant for long-context MCP interview paths. | Promote to P0 code gate if the supported path hangs, returns malformed tool-use state, or cannot provide truthful recovery. |
| #578 | P2 design/future | Unified watchdog controls are design context while current directive-mapping/runtime tests pass. | Promote if watchdog/directive verification regresses in a release-supported path. |
| #1139 | P2 design/future | Future backend support is an optional integration surface, not a core AgentOS blocker. | Promote only if release docs claim this backend as supported. |
| #1239 | P2 design/future | Future interview adapter enhancement is product expansion outside the core release gate. | Promote only if current release behavior or docs depend on the adapter enhancement. |

### Classification Boundary Rules And Tie-Breakers

Use the most specific blocking classification that matches the current
candidate. The buckets are mutually exclusive for release handling even when an
issue is relevant to more than one AgentOS surface.

1. **#961 authority wins.** If #961 or an owner comment on #961 classifies an
   issue differently from this document, update this document or treat #961 as
   the release gate until the conflict is resolved.
2. **External gates outrank code work.** If the next action is maintainer
   merge/defer, owner disposition, or product/policy approval, classify the
   item as a P0 external gate even when the affected behavior is technically
   important. Do not duplicate approved PR work or policy-dependent work in a
   local release slice.
3. **Reproduced candidate failures outrank roadmap labels.** If verification
   fails in an advertised release path and the fix is local to this repo,
   classify the issue as a P0 code gate, even if its issue label or prior
   disposition was current-next or design/future.
4. **Narrow failing child beats broad parent.** When a broad canonical issue
   and a concrete child both cover the same risk, attach the release slice to
   the narrow issue with a measurable failing acceptance criterion. Keep the
   parent as context unless #961 explicitly makes the parent the gate.
5. **Current behavior beats future capability.** If existing documented or
   advertised behavior is at risk, classify as P0 code gate or P1 current-next
   based on verification. If the issue only adds new capability, new backend
   support, ecosystem management, export shape, or deliberation UX, classify it
   as P2 design/future unless #961 promotes it.
6. **Safety and truthfulness beat polish.** Permission/audit bypasses, HITL
   authority failures, false resume/retry guidance, missing recovery evidence,
   and long-running hangs outrank additive UX or documentation cleanup. Promote
   UX/docs only when the current candidate would otherwise over-claim support.
7. **Passing bounded verification keeps an item current-next.** A
   release-relevant issue remains P1 current-next when it has a narrow useful
   slice but the candidate's advertised behavior passes the tied verification
   method.
8. **Closed or folded issues cannot block directly.** Treat them as
   Done/folded evidence under their active canonical owner. Reopen blocking
   status only through the active owner or a newly reproduced candidate failure.

When an issue still fits multiple buckets after those rules, use this final
ordering: P0 external gate, P0 code gate, P1 current-next, P2 design/future,
Done/folded. Use P0 external over P0 code only when the immediate release
decision is outside local implementation; otherwise use P0 code for reproduced
local defects.

### Current-Next Classification Criteria

Current-next means "next release-adjacent engineering work after the candidate
gate is satisfied." It is narrower than future roadmap work, but it is not a
stop-ship label by itself. Use this bucket when all of these are true:

1. The issue maps to a live #961 or #1157 surface for runtime stability,
   recovery/cancel/retry, mandatory MCP dispatch, plugin/tool integration,
   observability, security/permissions, HITL authority, or release-supported
   UX/documentation.
2. The issue has a narrow verifiable slice, or can be reduced to one without
   reopening broad AgentOS architecture.
3. The current candidate has passing or bounded verification for the advertised
   behavior, so the item is not a known P0 code gate at this snapshot.
4. The issue does not depend on an unresolved external merge, owner
   disposition, or policy decision before useful code or verification work can
   proceed.

Promote a current-next item to P0 code gate only when its tied verification
method fails on the candidate commit or when #961 explicitly marks the failing
path as release-blocking. Promote it to P0 external gate instead when the
remaining action is maintainer merge/defer, owner disposition, or product
policy acceptance rather than local code.

Demote an item from current-next to P2 design/future when it lacks a concrete
candidate-facing failure mode, depends on unresolved policy/design approval, or
only adds new backend, ecosystem, export, deliberation, or UX surface area that
the current release does not advertise. Closed or folded items are never
current-next; they remain evidence-only under their canonical owner.

### Design-Wait Classification Criteria

Design-wait means "keep the issue open for owner/product/architecture
decision, but do not spend release implementation time on it yet." It is a
release triage boundary, not a judgment that the work is unimportant. Use this
bucket when one or more of these are true:

1. The issue requires an unresolved product, policy, security, or architecture
   decision before implementation can be correct.
2. The issue broadens a future substrate or ecosystem surface beyond the
   current #961 release candidate's advertised behavior.
3. The issue's release-relevant risk is already represented by a narrower
   current-next or P0 owner, so implementing the broad parent would duplicate
   the release slice.
4. The issue adds optional backend, plugin-manager, export, deliberation,
   runtime-policy, or UX capability that is not needed to keep the current
   candidate truthful.
5. The issue is blocked by an upstream design issue, owner approval, or policy
   gate whose outcome could materially change the implementation contract.

Design-wait is the default disposition for unresolved roadmap issues that have
no candidate-facing failing behavior. The issue can remain open and visible in
release notes as future work, but the release gate should not require local code
for it while current verification remains green.

Promote a design-wait item only through an explicit trigger:

1. Promote to P0 code gate when verification reproduces a defect in an
   advertised release path and the defect has a narrow local fix.
2. Promote to P0 external gate when the remaining blocker is owner acceptance,
   maintainer merge/defer, or policy approval needed for a release claim.
3. Promote to P1 current-next when owners accept the design boundary and the
   remaining work can be expressed as a narrow, independently verifiable slice.

Keep the item in design-wait when the only available action is to design a
larger architecture, expand a future ecosystem, add optional integrations, or
increase product ambition beyond the current #961 release gate. Closed/folded
issues are not design-wait; they are evidence-only under the active canonical
owner.

Current design-wait examples in this inventory:

| Issue cluster | Why design-wait |
| --- | --- |
| #1256 -> #1263 | Product-or-Die policy must be accepted before dependent auto-fill implementation can be release work. |
| #518, #575, #578 | Broader lifecycle, outbox, and watchdog-control design remains future work while current AgentProcess/watchdog verification passes. |
| #573, #614, #615 | Runtime profile, external-guidance provenance, and reasoning-budget policy need accepted policy/design boundaries before release code slices. |
| #725 | UserLevel plugin-manager work expands ecosystem management beyond the core #939 plugin contract gate. |
| #813-#819 | Multi-agent deliberation and lateral-review backlog is not on the current AgentOS release path unless #961 or #1157 promotes a concrete bug. |
| #1139, #1239 | Future backend and interview-adapter enhancements are optional integration/product surfaces, not current release blockers. |

## Release-Critical Classification

This section defines the release-critical decision rule used by the table
below. It is intentionally narrower than "important to AgentOS": release
critical means the item blocks the AgentOS release declaration for the current
candidate, not that the item is strategically important or should be closed.

An issue or PR is release-critical when at least one of these conditions is
true:

1. #961 names it as a current release gate, or owner comments in #961 conflict
   with the local readiness snapshot.
2. It is an approved/open PR or owner-disposition issue that must be merged,
   deferred, accepted, or closed before the release claim is truthful.
3. Verification reproduces a defect in an advertised release path for
   long-running runtime stability, recovery, cancel, retry, malformed-turn
   handling, mandatory MCP dispatch, plugin permissions/audit, HITL authority,
   or observability needed for recovery evidence.
4. The required verification pack fails on the candidate commit without an
   accepted release-note exception or owner disposition.
5. Documentation or release notes would otherwise over-claim support for a
   partially implemented or unverified path.

An issue is non-critical for this candidate when it is one of these:

1. Closed, folded, or represented by a canonical parent that already owns the
   active release surface.
2. Broad design, policy, or ecosystem work that has not been accepted as a
   current release gate by #961 or the owning issue.
3. Additive observability, projection, export, backend, adapter, or UX work
   whose current release-supported behavior remains covered by passing tests.
4. Historical roadmap or RFC context superseded by #961, #1157, or a narrower
   child issue.

Conditional P0 promotion is evidence-based. A non-critical item becomes a P0
code gate only when the verification method tied to its rationale fails or a
maintainer explicitly marks the issue as blocking. If the failure is a
policy/merge/owner-decision problem rather than local code, classify it as a
P0 external gate and do not duplicate the implementation locally.

The table below applies that binary classification. P1 items are intentionally
classified as non-critical for this snapshot unless the verification pack
reproduces the risk described in the rationale.

| Issue/PR | Classification | Brief rationale |
| --- | --- | --- |
| #961 | Release-critical external gate | SSOT for AgentOS release readiness; any conflict with this local snapshot blocks release declaration. |
| #1279 | Release-critical external gate | Approved AgentOS/`ooo auto` PR lane must be merged or explicitly deferred before final readiness. |
| #1280 | Release-critical external gate | Approved AgentOS/`ooo auto` PR lane must be merged or explicitly deferred before final readiness. |
| #1258 | Release-critical external gate | Owner disposition is required before claiming improved `ooo auto` throughput. |
| #830 | Non-critical | Historical fat-harness/evidence authority; current release evidence lives in shipped code and baseline metrics. |
| #892 | Non-critical | Status-map context only; #961 is the active sequencing authority. |
| #920 | Non-critical | Closed fat-harness roadmap; current tests and baseline metrics are the release evidence. |
| #925 | Non-critical, conditional P0 if reproduced | Long-running runtime/MCP stability is release-relevant, but no current failing advertised path is known in this snapshot. |
| #939 | Non-critical, conditional P0 if reproduced | Plugin permission/audit regressions would block, but current contract slices/tests keep remaining umbrella work current-next. |
| #946 | Non-critical, conditional P0 if verification fails | Projection/read-model observability is release-relevant, but remaining work is additive while projection tests pass. |
| #956 | Non-critical | Closed Workflow IR substrate; conformance tests are evidence, not new release work. |
| #960 | Non-critical, conditional P0 if authority fails | HITL safety would block if advertised approval/resume authority failed; current broader work remains current-next. |
| #772 | Non-critical | Broad `ooo auto` epic is superseded for release triage by #1157 and concrete child issues. |
| #809 | Non-critical | Closed RFC context; accepted ideas are represented by current `auto` code and #1157. |
| #1157 | Non-critical | Product authority for the `ooo auto` lane, but release can proceed with documented gaps and #1258 disposition. |
| #1170 | Non-critical, conditional P0 if canonical scenario fails | L0 canonical scenario is release evidence; it blocks only if the candidate cannot pass or explicitly defer it. |
| #1234 | Non-critical | Verifier matching cleanup is covered by targeted tests on main; remaining action is owner cleanup/narrowing. |
| #1254 | Non-critical | EventStore wiring/provenance regressions are covered by tests; remaining work is cleanup or policy-dependent follow-up. |
| #1256 | Non-critical | Product-or-Die is an unresolved policy/design gate, not a required implementation blocker for this candidate. |
| #1263 | Non-critical | Dependent implementation is deferred until #1256 is accepted. |
| #579 | Non-critical, conditional P0 if recovery regresses | Handoff idempotency matters for recovery, but current contract tests are the release evidence. |
| #637 | Non-critical, conditional P0 if bypass reproduced | Mandatory MCP dispatch would block if bypassed; current integration coverage makes it current-next. |
| #640 | Non-critical | Provenance/grounding is observability follow-up unless supported paths hide provenance. |
| #673 | Non-critical | Packaged-skill driver/brake forwarding is current-next runtime integration cleanup. |
| #674 | Non-critical | Resume-bounds alignment is recovery cleanup unless verification shows unsafe or false resume guidance. |
| #678 | Non-critical | Broad autopilot follow-up tracker; no stop-ship slice unless #1157 promotes one. |
| #688 | Non-critical, conditional P0 if wording regresses | Truthful resume/retry wording affects recovery UX; current resume-capability tests are evidence. |
| #692 | Non-critical | Discord gateway incident follow-up is outside core AgentOS readiness unless current docs depend on it. |
| #518 | Non-critical | Broad lifecycle roadmap; current AgentProcess acceptance evidence covers release-critical behavior. |
| #573 | Non-critical | Profile taxonomy cleanup is future planning while locked docs/tests remain coherent. |
| #575 | Non-critical | ControlJournal/outbox design is future work unless current recovery paths lose durable state. |
| #614 | Non-critical | External guidance provenance is a future policy/security design gate. |
| #615 | Non-critical | Reasoning-budget policy is future runtime policy work. |
| #725 | Non-critical | UserLevel plugin manager is ecosystem roadmap work, separate from core plugin release contracts. |
| #813 | Non-critical | Multi-agent deliberation backlog; not on the current AgentOS release path. |
| #814 | Non-critical | Multi-agent deliberation backlog; not on the current AgentOS release path. |
| #815 | Non-critical | Multi-agent deliberation backlog; not on the current AgentOS release path. |
| #816 | Non-critical | Multi-agent deliberation backlog; not on the current AgentOS release path. |
| #817 | Non-critical | Interview stagnation/lateral-review enhancement; not required unless advertised milestone behavior depends on it. |
| #818 | Non-critical | Multi-agent deliberation backlog; not on the current AgentOS release path. |
| #819 | Non-critical | Multi-agent deliberation backlog; not on the current AgentOS release path. |
| #831 | Non-critical, conditional P0 if advertised path hangs | Malformed-turn MCP interview risk should be retested/disclosed, but is not a known current release blocker. |
| #578 | Non-critical | Unified watchdog controls remain design context while current directive-mapping tests pass. |
| #1139 | Non-critical | Future backend support, not a core AgentOS release blocker. |
| #1239 | Non-critical | Future interview adapter enhancement, not a core AgentOS release blocker. |
| #921 | Non-critical | Closed/folded into active `ooo auto` authorities. |
| #922 | Non-critical | Closed/folded into lifecycle/control parents. |
| #923 | Non-critical | Closed/folded into runtime policy/design parents. |
| #924 | Non-critical | Closed/folded into the multi-agent deliberation backlog. |
| #930 | Non-critical | Closed/folded into #956 Workflow IR. |
| #931 | Non-critical | Closed/folded into shipped #830 evidence substrate. |
| #932 | Non-critical | Closed/folded into #946 projection readiness. |
| #933 | Non-critical | Closed/folded into #946 projection readiness. |
| #934 | Non-critical | Closed/folded into #939 plugin contract readiness. |
| #935 | Non-critical | Closed/folded into #920/#961 fat-harness evidence. |
| #936 | Non-critical | Closed/folded into #946 service-boundary projection vocabulary. |
| #937 | Non-critical | Closed/folded into #956 Workflow IR graph primitives. |
| #938 | Non-critical | Closed/folded into #946/#956 replay and evaluation validation. |
| #940 | Non-critical | Closed/folded into #920/#946 workspace-provider requirements. |
| #941 | Non-critical | Closed/folded into #946 envelope vocabulary. |
| #942 | Non-critical | Closed/folded into #960/#939 authority and permission risk surfaces. |
| #943 | Non-critical | Closed/folded into #946 optional export work. |
| #944 | Non-critical | Closed/folded into #946 replay projection. |
| #945 | Non-critical | Closed/folded into #920 future extension scope. |
| #947 | Non-critical | Closed/folded into #946 run-capsule vocabulary. |
| #948 | Non-critical | Closed/folded into #946/#956 eval-suite validation. |
| #949 | Non-critical | Closed/folded into #939 permission audit readiness. |
| #950 | Non-critical | Closed/folded into #946 context hierarchy metadata. |
| #951 | Non-critical | Closed/folded into #946 projection-derived harness inspector. |
| #952 | Non-critical | Closed/folded into #946/#956 benchmark validation discipline. |
| #953 | Non-critical | Closed/folded into #946 context archival. |
| #954 | Non-critical | Closed/folded into #946 execution handoff metadata. |
| #955 | Non-critical | Closed/folded into #946/#960 pre-mutation safety. |
| #957 | Non-critical | Closed/folded into #956 durable lifecycle events. |
| #958 | Non-critical | Closed/folded into #960 durable HITL primitive. |
| #959 | Non-critical | Closed/folded into #956 conformance harness. |
| #963 | Non-critical | Closed/folded back into canonical AgentOS surfaces. |
| #964 | Non-critical | Closed/folded back into canonical AgentOS surfaces. |
| #965 | Non-critical | Closed/folded back into canonical AgentOS surfaces. |
| #966 | Non-critical | Closed/folded back into canonical AgentOS surfaces. |
| #967 | Non-critical | Closed/folded back into canonical AgentOS surfaces. |
| #968 | Non-critical | Closed/folded into #946/#956 benchmark/plugin validation discipline. |

### Classification Rationale And Blocking Implications

This matrix records the release-handling rationale behind the classifications
above. It is the handoff view for deciding whether a classification changes the
candidate gate.

| Classification | Issues / PRs | Rationale | Release-blocking implication |
| --- | --- | --- | --- |
| Release-critical external gate | #961 | #961 is the roadmap SSOT and can override this local snapshot. | Release declaration is blocked if #961's checklist, labels, or owner comments disagree with this document. Reconcile in #961 rather than implementing #961 directly. |
| Release-critical external gate | #1279, #1280 | Approved PR lane is outside this local implementation pass. | Final readiness is blocked only until maintainers merge or explicitly defer these PRs; do not duplicate their implementation locally. |
| Release-critical external gate | #1258 | `ooo auto` throughput is an owner/product claim, not a local code fact that this pass can settle. | Release notes must avoid improved-throughput claims until the owner accepts, closes, or defers the issue. |
| Conditional P0 code gate / P1 current-next | #925, #831 | Long-running MCP/runtime stability and malformed-turn behavior are release-relevant, but no failing advertised path is recorded in this snapshot. | Non-blocking while runtime verification passes. Promote to P0 code gate if start/status/result, cancel/retry, or long-context interview paths hang or lose truthful recovery. |
| Conditional P0 code gate / P1 current-next | #939 | Plugin lifecycle, permission, hook, and audit invariants are core AgentOS safety surfaces. | Non-blocking while plugin contract tests pass. Promote to P0 code gate on any permission bypass, audit omission, hook-contract regression, or plugin/runtime boundary failure. |
| Conditional P0 code gate / P1 current-next | #946 | Projection/read-model work supports observability and recovery evidence. | Non-blocking while current projection docs/tests pass. Promote to P0 code gate only if release verification cannot reconstruct required Run/Stage/Step/Artifact/Verdict evidence. |
| Conditional P0 code gate / P1 current-next | #960 | HITL approval authority is safety-relevant, but broader persistence/resume work is not fully advertised as release-complete. | Non-blocking with accurate docs. Promote to P0 code gate if release-supported approval, WAIT, or RESUME authority fails or docs over-claim unsupported behavior. |
| Conditional P0 code gate / P1 current-next | #1157, #1170, #1234, #1254, #579, #637, #640, #673, #674, #678, #688, #692 | These are current `ooo auto` product, evidence, recovery, dispatch, provenance, and UX slices. | Non-blocking when documented gaps remain truthful and targeted verification passes. Promote the narrow failing child to P0 if canonical acceptance, mandatory MCP dispatch, EventStore provenance, resume/retry truthfulness, or packaged-skill controls regress. |
| P2 design/future | #772, #1256, #1263, #518, #573, #575, #614, #615, #725, #813, #814, #815, #816, #817, #818, #819, #578, #1139, #1239 | These items either require product/architecture approval or expand future substrate, ecosystem, backend, policy, or deliberation capabilities beyond the current #961 candidate. | Not release-blocking unless #961 or an owner promotes a narrow release slice, or verification proves the current advertised behavior depends on the unresolved design. |
| Done/folded evidence | #830, #892, #920, #956, #809, #921-#924, #930-#938, #940-#945, #947-#955, #957-#959, #963-#968 | These issues are closed, folded, or represented by active canonical owners and current tests/docs. | No direct release block. Use them as evidence/context only; reopen release work through the active owner or a reproduced candidate failure. |

## Triage Summary: Owners And Next Actions

Owner labels below are role owners for release handoff, not GitHub assignee
claims. If a row has a concrete next action, that action is the owner for this
release pass until a maintainer assigns a person.

Coverage rule: every issue in the canonical review set above has exactly one
row here. Folded children also get rows so release owners can see why they do
not create duplicate work.

| Issue/PR | Owner or concrete next action |
| --- | --- |
| #961 | Release owner keeps this as the SSOT and reconciles any conflict before tagging. |
| #1279 | Maintainer action: merge or explicitly defer the approved PR before final readiness. |
| #1280 | Maintainer action: merge or explicitly defer the approved PR before final readiness. |
| #1258 | Product/release owner action: accept, close, or defer the throughput risk before claiming improved `ooo auto` throughput. |
| #830 | No action; folded substrate authority for fat-harness evidence. |
| #892 | No action; context only, with #961 as the active sequencing SSOT. |
| #920 | No action; preserve as historical fat-harness context unless verification regresses. |
| #925 | Runtime owner action: keep as the current-next long-running MCP/runtime stability follow-up and disclose any known advertised-path hang. |
| #939 | Plugin owner action: keep as the current-next plugin lifecycle/permission/audit umbrella; block only on a discovered contract-test regression. |
| #946 | Observability owner action: keep as the current-next projection/read-model follow-up; block only on failing projection verification. |
| #956 | No action; closed Workflow IR substrate, verified through current conformance tests. |
| #960 | HITL owner action: keep as the current-next approval/resume contract follow-up and avoid over-advertising unsupported HITL persistence. |
| #772 | No release action; broad `ooo auto` epic remains future context behind #1157 and concrete slices. |
| #809 | No action; accepted ideas are represented by #1157 and current `auto` recovery/profile behavior. |
| #1157 | `ooo auto` product owner action: keep as product authority; document remaining gaps and link #1258 disposition. |
| #1170 | Verification owner action: use the L0 canonical scenario as release evidence; block or explicitly defer if it fails. |
| #1234 | Verifier owner action: close or narrow after owner review if targeted verifier matching tests keep passing. |
| #1254 | Observability owner action: close or narrow after owner review if EventStore wiring tests keep passing. |
| #1256 | Policy owner action: decide the Product-or-Die invariant before enabling dependent behavior. |
| #1263 | No release action; defer until #1256 is accepted. |
| #579 | Runtime owner action: keep as handoff-idempotency contract context; promote only if current resume/retry verification regresses. |
| #637 | Runtime owner action: keep mandatory MCP dispatch covered by integration tests; block only if packaged `ooo auto` can bypass MCP again. |
| #640 | Observability owner action: keep provenance/grounding visible in auto status; release-impacting only if provenance disappears from supported paths. |
| #673 | Runtime owner action: keep packaged-skill driver/brake forwarding in the `ooo auto` follow-up lane. |
| #674 | Runtime owner action: keep MCP auto resume bounds aligned with CLI semantics. |
| #678 | Product owner action: keep autopilot follow-up slices deferred unless #1157 promotes them. |
| #688 | Runtime UX owner action: preserve truthful resume/retry wording for interview-start timeout paths. |
| #692 | Product owner action: keep Discord gateway incident follow-up separate from core AgentOS release readiness unless it affects current `ooo auto` docs. |
| #518 | Runtime lifecycle owner action: slice durable replay/pause/resume only after current AgentProcess acceptance evidence regresses or owners promote it. |
| #573 | Runtime profile owner action: leave broader taxonomy cleanup to planning while the locked profile taxonomy doc/tests remain coherent. |
| #575 | Recovery/observability owner action: promote only if verification shows current control/recovery paths lose durable state. |
| #614 | Policy/security owner action: keep as future external guidance provenance design. |
| #615 | Runtime policy owner action: keep as future reasoning-budget policy design. |
| #725 | Plugin ecosystem owner action: keep as UserLevel plugin-manager roadmap work, separate from core plugin contract readiness. |
| #813 | Design owner action: keep in the multi-agent deliberation backlog unless #961 or #1157 promotes it. |
| #814 | Design owner action: keep in the multi-agent deliberation backlog unless #961 or #1157 promotes it. |
| #815 | Design owner action: keep in the multi-agent deliberation backlog unless #961 or #1157 promotes it. |
| #816 | Design owner action: keep in the multi-agent deliberation backlog unless #961 or #1157 promotes it. |
| #817 | Interview UX/recovery owner action: keep as future stagnation/lateral-review work unless the advertised milestone path depends on it. |
| #818 | Design owner action: keep in the multi-agent deliberation backlog unless #961 or #1157 promotes it. |
| #819 | Design owner action: keep in the multi-agent deliberation backlog unless #961 or #1157 promotes it. |
| #831 | Runtime UX owner action: retest or disclose the long-context MCP interview malformed-turn risk if that path remains in release scope. |
| #578 | Runtime policy owner action: leave unified watchdog controls as design context while current directive mapping tests pass. |
| #1139 | Integration owner action: keep as future backend support, not a core AgentOS release blocker. |
| #1239 | Interview product owner action: keep as future adapter enhancement, not a core release blocker. |
| #921 | No action; folded into #772 and #809. |
| #922 | No action; folded into #518 and #575. |
| #923 | No action; folded into #573, #614, and #615. |
| #924 | No action; folded into #813-#819. |
| #930 | No action; folded into #956. |
| #931 | No action; folded into shipped #830 substrate. |
| #932 | No action; folded into #946. |
| #933 | No action; folded into #946. |
| #934 | No action; folded into #939. |
| #935 | No action; folded into #920/#961 fat-harness evidence. |
| #936 | No action; folded into #946. |
| #937 | No action; folded into #956. |
| #938 | No action; folded into #946/#956. |
| #940 | No action; folded into #920/#946. |
| #941 | No action; folded into #946. |
| #942 | No action; folded into #960/#939. |
| #943 | No action; folded into #946. |
| #944 | No action; folded into #946. |
| #945 | No action; folded into #920 future extension scope. |
| #947 | No action; folded into #946. |
| #948 | No action; folded into #946/#956. |
| #949 | No action; folded into #939. |
| #950 | No action; folded into #946. |
| #951 | No action; folded into #946. |
| #952 | No action; folded into #946/#956. |
| #953 | No action; folded into #946. |
| #954 | No action; folded into #946. |
| #955 | No action; folded into #946/#960. |
| #957 | No action; folded into #956. |
| #958 | No action; folded into #960. |
| #959 | No action; folded into #956. |
| #963 | No action; folded back into canonical AgentOS surfaces. |
| #964 | No action; folded back into canonical AgentOS surfaces. |
| #965 | No action; folded back into canonical AgentOS surfaces. |
| #966 | No action; folded back into canonical AgentOS surfaces. |
| #967 | No action; folded back into canonical AgentOS surfaces. |
| #968 | No action; folded into #946/#956 validation discipline. |

### Release Authorities And External Gates

| Issue/PR | Current status | Priority | Release impact | Release disposition |
| --- | --- | --- | --- | --- |
| #961 | Open SSOT roadmap for AgentOS sequencing. | P0 external gate | Blocks release declaration if its labels, checklist, or owner comments disagree with this local snapshot. | Treat as the release authority, not an implementation target. If this document conflicts with #961, #961 wins. |
| #1279 | Open PR at the local snapshot; approved/green per prior scan. | P0 external gate | Blocks final readiness only as a merge/defer decision, because the implementation already lives outside this pass. | Merge or explicitly defer before claiming final readiness. |
| #1280 | Open PR at the local snapshot; approved/green per prior scan. | P0 external gate | Blocks final readiness only as a merge/defer decision, because the implementation already lives outside this pass. | Merge or explicitly defer before claiming final readiness. |
| #1258 | Open `needs-approval` release risk in the local issue inventory. | P0 external gate | Blocks claims about improved `ooo auto` throughput until the owner accepts the risk, closes it, or defers it in release notes. | Owner must accept, close, or defer before release notes claim improved `ooo auto` throughput. |

### External Gate Details

| Gate | Current status | Owner | Unblock condition |
| --- | --- | --- | --- |
| #1279 approved PR merge | Open approved PR at the local snapshot; prior scan recorded it as clean and green. Local release-readiness work must not duplicate its implementation. | Maintainer/release owner with merge authority for Q00/ouroboros. | Merge #1279 into `main`, or explicitly defer it in #961 or release notes before claiming final AgentOS readiness. |
| #1280 approved PR merge | Open approved PR at the local snapshot; prior scan recorded it as clean and green. Local release-readiness work must not duplicate its implementation. | Maintainer/release owner with merge authority for Q00/ouroboros. | Merge #1280 into `main`, or explicitly defer it in #961 or release notes before claiming final AgentOS readiness. |
| #1258 owner disposition | Open `needs-approval` release risk at the local snapshot. It is a policy/product gate for `ooo auto` throughput claims, not a local implementation target for this pass. | Product/release owner responsible for #1157/#961 `ooo auto` release claims. | Explicitly accept the throughput risk, close #1258 as no longer release-blocking, or defer it in #961/release notes. Until then, release notes must withhold improved-throughput claims. |

### #961 Canonical Track C Representatives

| Issue | Current status | Priority | Release impact | Release disposition |
| --- | --- | --- | --- | --- |
| #830 | Historical RFC v2 for Thin Skill + Fat Harness invariants. | Done/folded | Release evidence depends on the shipped fat-harness and verifier substrate, not on reopening this historical RFC. | Treat as the substrate authority behind the fat-harness gate; no direct release implementation. |
| #892 | High-level AgentOS status map referenced by #961. | Done/folded | No independent release gate; it informs sequencing only where #961 still points to it. | Use as context only; #961 is the current sequencing SSOT. |
| #920 | Closed in the live GitHub page; originally the `ooo run` fat-harness execution-path roadmap. | Done/folded | No current stop-ship impact unless fat-harness verification regresses. | No local implementation slice. Preserve as historical context for fat-harness acceptance and baseline metrics. |
| #925 | Open, tier-1 runtime/MCP reliability issue; owns net-new long-running MCP/runtime questions including malformed `stop_reason=tool_use` and start/status/result consistency. | P1 current-next | Potential release risk for long-running stability, recovery, cancel, and retry behavior in advertised MCP/runtime paths. | Keep as the main runtime stability follow-up. The release can proceed only if no known long-running hang remains in the advertised path, or if the risk is disclosed. |
| #939 | Open plugin lifecycle, permissions, hooks, and audit umbrella. | P1 current-next | Potential release risk for tool/plugin integration and permission/audit regressions; not a blocker while current contract tests pass. | Core plugin contract slices exist on main; remaining work is umbrella/design and follow-up hook/runtime coverage. Not a stop-ship blocker unless a permission/audit regression is found. |
| #946 | Open, now labeled `tier-2-unblocked`; owns Run/Stage/Step/Artifact/Verdict projection vocabulary. | P1 current-next | Impacts observability and release diagnostics; not a stop-ship blocker if current projection tests and docs pass. | Projection substrate is present on main. Remaining slices are additive observability/read-model work, not a release blocker if verification passes. |
| #956 | Closed in the live GitHub page; typed Workflow IR and conformance substrate. | Done/folded | Current conformance tests are release evidence; the closed issue itself does not block release. | Treat current `workflow_ir` code, docs, and conformance tests as the release evidence. No further release slice selected here. |
| #960 | Open, tier-1 HITL WAIT/RESUME approval contract. | P1 current-next | Safety and authority-gate risk if unreleased docs promise broader HITL resume/approval behavior than current tests cover. | Safety-relevant but still broader than this release pass. Release needs disclosure if pending HITL persistence/resume is advertised beyond currently tested flows. |

### `ooo auto` Child And Canonical Issues

| Issue | Current status | Priority | Release impact | Release disposition |
| --- | --- | --- | --- | --- |
| #772 | Open tactical EPIC for `ooo auto` end-to-end completion. | P2 design/future | No direct release block; broad epic scope is too large for the AgentOS candidate gate. | Superseded in day-to-day release readiness by #1157 and concrete slices. Keep as historical epic; do not implement directly. |
| #809 | Closed strategic RFC for domain-agnostic self-healing E2E. | Done/folded | No direct release block; current impact is through implemented `auto` recovery/profile behavior and #1157. | Its accepted ideas are represented by #1157 lanes and current `auto` code. No direct release slice. |
| #1157 | Open meta SSOT for `ooo auto` autonomous completion. | P1 current-next | Release-affecting product authority for `ooo auto`; remaining gaps must be documented, and throughput claims depend on #1258. | Product authority for the `ooo auto` lane. Release can proceed if remaining gaps are documented and #1258 is dispositioned. |
| #1170 | Open L0 canonical acceptance-test slice; `cli-todo` is the minimum current scenario. | P1 current-next | Release evidence gate for the minimum `ooo auto` scenario; failing canonical acceptance should block release or be explicitly deferred. | Use as release evidence, not broad harness expansion. Keep remaining scenario/evidence cleanup out of this pass unless the canonical test itself fails. |
| #1234 | Open in the local inventory; verifier command/test matching regressions are covered by current tests on main. | P1 current-next | Low residual release risk while targeted verifier matching tests pass; issue cleanup remains. | Treat as mostly implemented; close or narrow after owner review. |
| #1254 | Open in the local inventory; `auto.interview.*` EventStore wiring and regressions are present on main. | P1 current-next | Low residual release risk while EventStore wiring tests pass; failures would affect observability and recovery evidence. | Treat as mostly implemented; remaining scope should be issue cleanup or a follow-up to #1256. |
| #1256 | Open design gate for the Product-or-Die invariant. | P2 design/future | Policy/design gate only; should not block the release unless owners decide Product-or-Die is mandatory for this candidate. | Policy/RFC decision. Do not implement dependent behavior until accepted. |
| #1263 | Open dependent aggressive auto-fill implementation slice. | P2 design/future | No release block because it depends on unresolved #1256 policy. | Deferred until #1256 is accepted. Not release-critical for AgentOS readiness. |
| #579 | Open handoff idempotency contract issue in the `ooo auto` lane. | P1 current-next | Release risk only if resume/retry handoff loses idempotency or duplicates execution. | Current contract tests are release evidence; keep as follow-up unless verification regresses. |
| #637 | Open bug-class issue for mandatory `ouroboros_auto` MCP dispatch. | P1 current-next | Release-critical if packaged `ooo auto` can bypass the MCP pipeline silently. | Current integration coverage is release evidence; promote to P0 only on reproduced bypass. |
| #640 | Open provenance / repository-grounding follow-up for `ooo auto` interview answers. | P1 current-next | Affects observability and trust in auto-generated answers; release-critical only if supported paths hide provenance. | Keep as current-next observability follow-up and verify supported status/provenance rendering. |
| #673 | Open packaged-skill forwarding issue for driver and brake arguments. | P1 current-next | Can affect user-visible control of `ooo auto` from packaged skill entrypoints. | Retain as current-next runtime integration cleanup; block only if supported packaged invocation drops controls. |
| #674 | Open resume-bounds alignment issue between MCP auto and CLI semantics. | P1 current-next | Can affect recovery guidance and retry safety after partial `ooo auto` runs. | Retain as current-next recovery slice; verify CLI/MCP wording before final release notes. |
| #678 | Open autopilot follow-up implementation tracker. | P1 current-next | Broad product-completion follow-up; not a stop-ship item unless #1157 promotes a slice. | Keep as current-next planning context behind #1157. |
| #688 | Open truthful resume semantics issue for interview-start timeouts. | P1 current-next | User-facing recovery risk if output says `Resume:` where only `Retry:` is true, or vice versa. | Current resume-capability tests are release evidence; disclose or fix if they fail. |
| #692 | Open Discord gateway rewrite incident follow-up tracker. | P1 current-next | Release-impacting only if current `ooo auto` docs or behavior still depend on affected gateway assumptions. | Keep separate from core AgentOS readiness unless owner promotes a concrete slice. |

### Other Open Linked Issues From #961

| Issue | Current status | Priority | Release impact | Release disposition |
| --- | --- | --- | --- | --- |
| #518 | Open lifecycle roadmap for AgentProcess spawn/pause/resume/cancel/replay. | P2 design/future | Long-running lifecycle risk is covered by current AgentProcess acceptance evidence; broader durable replay can remain future work. | Current AgentProcess acceptance coverage is release evidence; durable widening remains future sliced work. |
| #573 | Open profile taxonomy/config contract design issue referenced by #961. | P2 design/future | No release block while the locked profile taxonomy doc and tests remain coherent. | Profile schema work has landed in slices; leave broader taxonomy cleanup to owner planning. |
| #575 | Open ControlJournal delivery/outbox design issue referenced by #961. | P2 design/future | Recovery/observability risk only if current control paths lose durable state under verification. | Not a release blocker unless a current control/recovery path loses durable state under verification. |
| #614 | Open external guidance contract design gate. | P2 design/future | Security/policy concern for future prompt guidance provenance; no release block for current local candidate. | Keep as policy/design. No local release code slice selected. |
| #615 | Open reasoning budget / cognitive effort policy design gate. | P2 design/future | Runtime-cost policy concern for future agents; no release block for current candidate. | Keep as future runtime policy. No local release code slice selected. |
| #725 | Open UserLevel plugin manager RFC/design parent. | P2 design/future | Plugin ecosystem readiness concern, separate from core plugin contract tests required for this release. | Related to plugin ecosystem operations, not core release readiness for this pass. |
| #813 | Open/linked Symposium and debate design item. | P2 design/future | Multi-agent deliberation enhancement; no AgentOS release block unless #961 or #1157 promotes it to a concrete slice. | Keep in the design backlog. |
| #814 | Open/linked Symposium and debate design item. | P2 design/future | Multi-agent deliberation enhancement; no AgentOS release block unless #961 or #1157 promotes it to a concrete slice. | Keep in the design backlog. |
| #815 | Open/linked Symposium and debate design item. | P2 design/future | Multi-agent deliberation enhancement; no AgentOS release block unless #961 or #1157 promotes it to a concrete slice. | Keep in the design backlog. |
| #816 | Open/linked Symposium and debate design item. | P2 design/future | Multi-agent deliberation enhancement; no AgentOS release block unless #961 or #1157 promotes it to a concrete slice. | Keep in the design backlog. |
| #817 | Open/linked interview stagnation and lateral review design item. | P2 design/future | UX/recovery enhancement with existing advisory tests; no release block unless the affected interview milestone path is advertised as complete. | Keep in the design backlog. |
| #818 | Open/linked Symposium and debate design item. | P2 design/future | Multi-agent deliberation enhancement; no AgentOS release block unless #961 or #1157 promotes it to a concrete slice. | Keep in the design backlog. |
| #819 | Open/linked Symposium and debate design item. | P2 design/future | Multi-agent deliberation enhancement; no AgentOS release block unless #961 or #1157 promotes it to a concrete slice. | Keep in the design backlog. |
| #831 | Open MCP/interview malformed-turn UX bug referenced by #925. | P1 current-next | Runtime reliability and UX risk for long-context MCP interview paths; disclose or retest if that path is in release scope. | Document as the runtime reliability risk to revisit after release if the affected long-context MCP path remains in scope. |
| #578 | Open RuntimeControls watchdog contract issue. | P2 design/future | Long-running stability context; current directive-mapping tests cover the release evidence slice. | Keep as design/future unless current watchdog/directive verification regresses. |
| #1139 | Open future backend support item in the local inventory. | P2 design/future | Backend expansion only; no core AgentOS release block. | Integration enhancement, not a core AgentOS release blocker. |
| #1239 | Open future interview adapter item in the local inventory. | P2 design/future | Interview adapter enhancement only; no release block for current candidate. | Product enhancement, not a release blocker. |

### Closed/Folded Track C Issues From #961

These issues were reviewed through #961's closure table and should not create
new release work. Their active home is the canonical representative listed in
#961.

| Issue | Current status | Priority | Release impact | Active home / disposition |
| --- | --- | --- | --- | --- |
| #921 | Closed/folded | Done/folded | No direct release impact; covered by active `ooo auto` authorities. | #772 + #809 (`ooo auto` self-healing restatement). |
| #922 | Closed/folded | Done/folded | No direct release impact; covered by lifecycle/control parents. | #518 + #575 (control/lifecycle restatement). |
| #923 | Closed/folded | Done/folded | No direct release impact; covered by runtime policy/design parents. | #573 + #614 + #615 (runtime policy/replay input restatement). |
| #924 | Closed/folded | Done/folded | No direct release impact; covered by the multi-agent deliberation design backlog. | #813-#819 (multi-agent deliberation restatement). |
| #930 | Closed/folded | Done/folded | No direct release impact; Workflow IR evidence is under #956. | #956 (Workflow IR duplicate). |
| #931 | Closed/folded | Done/folded | No direct release impact; typed evidence and retry routing are represented by shipped #830 substrate. | #830 typed evidence and retry routing already shipped. |
| #932 | Closed/folded | Done/folded | No direct release impact; projection readiness is assessed through #946. | #946 RunSnapshot/safe-resume read model. |
| #933 | Closed/folded | Done/folded | No direct release impact; projection readiness is assessed through #946. | #946 local trace/eval projection read model. |
| #934 | Closed/folded | Done/folded | No direct release impact; plugin readiness is assessed through #939 tests and docs. | #939 plugin scaffold/validation/contract-test acceptance slice. |
| #935 | Closed/folded | Done/folded | No direct release impact; fat-harness readiness is assessed through #920/#961 evidence. | #920 HarnessRunner/invocation lifecycle absorbed into fat-harness path. |
| #936 | Closed/folded | Done/folded | No direct release impact; service-boundary projection work is under #946. | #946 service-boundary requirements absorbed into projection vocabulary. |
| #937 | Closed/folded | Done/folded | No direct release impact; graph primitives are under #956 Workflow IR. | #956 WorkflowGraph primitives folded into Workflow IR. |
| #938 | Closed/folded | Done/folded | No direct release impact; replay/eval validation is verified through #946/#956 surfaces. | #946 + #956 replayable eval/environment simulation validation. |
| #940 | Closed/folded | Done/folded | No direct release impact; workspace-provider requirements are absorbed by fat-harness/projection surfaces. | #920 + #946 workspace-provider requirements. |
| #941 | Closed/folded | Done/folded | No direct release impact; envelope vocabulary is assessed through #946. | #946 Action/Observation envelope vocabulary. |
| #942 | Closed/folded | Done/folded | No direct release impact; authority and permission risks are assessed through #960/#939. | #960 + #939 risk/authority gate. |
| #943 | Closed/folded | Done/folded | No direct release impact; optional export remains an additive derived view. | #946 optional OTel/export derived view. |
| #944 | Closed/folded | Done/folded | No direct release impact; replay projection is assessed through #946. | #946 checkpoint/context replay projection. |
| #945 | Closed/folded | Done/folded | No direct release impact; remote workspace support is future extension work. | #920 remote-workspace future extension. |
| #947 | Closed/folded | Done/folded | No direct release impact; run-capsule vocabulary is assessed through #946. | #946 Run Capsule projection/export vocabulary. |
| #948 | Closed/folded | Done/folded | No direct release impact; eval-suite validation is assessed through #946/#956 tests. | #946 + #956 eval-suite validation. |
| #949 | Closed/folded | Done/folded | No direct release impact; plugin permission audit readiness is assessed through #939. | #939 step-level plugin permission audit. |
| #950 | Closed/folded | Done/folded | No direct release impact; context hierarchy metadata is under #946. | #946 context hierarchy projection metadata. |
| #951 | Closed/folded | Done/folded | No direct release impact; harness inspector work is under #946. | #946 projection-derived harness inspector. |
| #952 | Closed/folded | Done/folded | No direct release impact; benchmark discipline is assessed through #946/#956. | #946 + #956 benchmark validation discipline. |
| #953 | Closed/folded | Done/folded | No direct release impact; context archival is under #946. | #946 ContextPackProvider/context archival. |
| #954 | Closed/folded | Done/folded | No direct release impact; handoff metadata is under #946. | #946 execution handoff metadata. |
| #955 | Closed/folded | Done/folded | No direct release impact; pre-mutation safety is assessed through #946/#960. | #946 + #960 ChangeSetLedger and pre-mutation safety. |
| #957 | Closed/folded | Done/folded | No direct release impact; durable lifecycle events are under #956. | #956 durable node lifecycle events. |
| #958 | Closed/folded | Done/folded | No direct release impact; HITL primitive readiness is under #960. | #960 durable HITL primitive. |
| #959 | Closed/folded | Done/folded | No direct release impact; conformance evidence is under #956. | #956 conformance harness. |
| #963 | Closed/folded | Done/folded | No direct release impact; post-SSOT cleanup is folded into canonical surfaces. | Post-SSOT trajectory/export lens folded back into canonical projection surfaces. |
| #964 | Closed/folded | Done/folded | No direct release impact; post-SSOT cleanup is folded into canonical surfaces. | Post-SSOT cleanup folded back into canonical AgentOS surfaces. |
| #965 | Closed/folded | Done/folded | No direct release impact; post-SSOT cleanup is folded into canonical surfaces. | Post-SSOT cleanup folded back into canonical AgentOS surfaces. |
| #966 | Closed/folded | Done/folded | No direct release impact; post-SSOT cleanup is folded into canonical surfaces. | Post-SSOT cleanup folded back into canonical AgentOS surfaces. |
| #967 | Closed/folded | Done/folded | No direct release impact; post-SSOT cleanup is folded into canonical surfaces. | Post-SSOT cleanup folded back into canonical AgentOS surfaces. |
| #968 | Closed/folded | Done/folded | No direct release impact; benchmark/plugin validation is assessed through #946/#956. | Benchmark harness/plugin idea folded back into #946/#956 validation discipline. |

## Candidate Release Slices

This ranking applies the #961 priority order to narrow slices that can be
implemented or verified locally without duplicating the #961 meta issue, the
approved PR lane, or owner/policy gates. External gates remain ranked above all
local work for the release declaration, but they are not implementation slices.

## Release-Critical Narrow Slice Boundaries

This section is the repository handoff for release-critical execution scope.
It names the narrow slices that can affect the AgentOS candidate, separates
local implementation work from external or policy gates, and records the
verification boundary for each slice. A slice becomes P0 release-critical code
work only when its trigger reproduces on the candidate commit or when #961
explicitly promotes it. Otherwise it remains current-next or design/future
work and must not be expanded during this release pass.

| Slice | In scope for this release pass | Out of scope / boundary | Acceptance signal | Verification / disposition |
| --- | --- | --- | --- | --- |
| `ooo health` copyable diagnostics | Preserve full setup and recovery details for config, database, runtime backend, and credential health checks after Rich table rendering. | No redesign of health-check discovery, backend selection, config storage, or credential loading. Do not change secret-redaction policy except to keep it at least as strict. | Existing health table and exit semantics remain; each diagnostic row with detail also emits a full plain-text line; no secret values are printed. | Selected local implementation slice. Verify with `uv run pytest tests/unit/cli/test_status.py -k health`, `uv run pytest tests/e2e/test_cli_commands.py -k status_health`, and the full release pack. |
| MCP malformed-turn and long-running runtime recovery | Verify supported MCP interview/start/status/result paths do not hang and return truthful retry, resume, or bounded-failure states. | No broad AgentProcess lifecycle replay redesign, no new outbox/control-journal architecture, and no implementation of #518/#575/#578 unless a supported path fails. | Supported runtime paths complete or expose recoverable bounded state; advertised recovery wording remains true. | Current-next under #925/#831. Promote to P0 only on reproduced hang, unrecoverable malformed state, or false recovery output. |
| Packaged `ooo auto` MCP dispatch and controls | Confirm packaged entrypoints dispatch through the mandatory MCP path, preserve driver/brake controls, and report truthful job/session state. | No broad completion-product redesign, no throughput claim implementation, and no Product-or-Die auto-fill work while #1258/#1256 remain unresolved. | Packaged invocation cannot bypass MCP while claiming the release path; resume/retry and control forwarding stay observable. | Current-next under #1157/#1170/#637/#673/#674/#688. Promote only if targeted auto/integration tests reproduce a bypass or false state. |
| Plugin permission, hook, lifecycle, and audit contract | Verify core plugin operations enforce permissions, hook contracts, runtime boundaries, and audit evidence. | No UserLevel plugin-manager ecosystem implementation from #725 and no broad marketplace or remote plugin workflow expansion. | Contract tests pass for plugin lifecycle, permission checks, hook validation, lock/digest, and audit surfaces. | Current-next under #939. Promote to P0 on permission bypass, unaudited mutation, hook contract regression, or runtime-boundary violation. |
| Projection and Workflow IR evidence boundary | Verify projection remains a rebuildable read model over emitted events and Workflow IR remains the planned graph/conformance substrate. | Do not move acceptance semantics into projection, embed projection records into IR, or widen projection exports beyond current release evidence. | Run/Stage/Step/Artifact/Verdict facts can be reconstructed; IR conformance remains separate from observed projection records. | Current-next under #946 with #956 evidence. Promote only if projection or conformance verification fails for release-supported evidence. |
| Release wording for throughput and policy-dependent claims | Keep release notes and docs from claiming improved `ooo auto` throughput or Product-or-Die auto-fill behavior without owner disposition. | No local implementation of #1258, #1256, or #1263; these require owner/product decisions. | Release notes either omit those claims or link an explicit accept/close/defer disposition. | External/policy gate. Resolve in #961, #1258, #1256, or release notes; no code test. |

The only selected local implementation slice in this snapshot is `ooo health`
copyable diagnostics. The other slices are intentionally bounded verification
or documentation gates. They should be implemented locally only after their
verification method fails or #961 changes the release gate.

### Explicit Acceptance Criteria By Slice

These criteria are the repository handoff for each release-critical execution
slice boundary. Passing criteria keep the slice non-blocking for this
candidate; a failed criterion promotes the narrow owning slice to P0 code gate
unless the required action is explicitly external or policy-owned.

#### `ooo health` Copyable Diagnostics

Acceptance criteria:

1. `ooo status health` preserves the existing Rich health table and summary
   behavior for interactive users.
2. Each health check with diagnostic detail also emits a plain-text line after
   the table in the form `<check name>: <status> - <full detail>`.
3. Plain-text detail lines are independent of Rich terminal width and contain
   complete config, database, credential-provider, and runtime CLI details.
4. The command keeps existing exit semantics: all-healthy exits zero; warning
   or error states still produce the existing nonzero failure behavior.
5. The output does not print API keys, bearer values, tokens, passwords, or
   configured secret values.

Verification:

- `uv run pytest tests/unit/cli/test_status.py -k health`
- `uv run pytest tests/e2e/test_cli_commands.py -k status_health`
- Full release pack: `uv run ruff check .`, `uv run mypy src`,
  `uv run pytest`

#### MCP Malformed-Turn And Long-Running Runtime Recovery

Acceptance criteria:

1. Supported MCP interview/start/status/result paths terminate with either a
   completed result or a bounded failure state; they do not hang indefinitely.
2. Malformed-turn failures return actionable retry, resume, cancel, or
   unsupported-state guidance instead of silent looping.
3. Recovery output is truthful: it does not claim resume is available when only
   retry is safe, and it does not hide a valid resume path.
4. Status/result calls remain usable after a failed or cancelled background
   operation.
5. Any unsupported long-running behavior is documented as a bounded release
   limitation rather than implied as supported.

Verification:

- Targeted MCP interview/runtime regression tests selected for the reproduced
  path.
- `uv run pytest tests/integration/orchestrator/test_agent_process_three_surface_acceptance.py`
- Full release pack before final readiness.

#### Packaged `ooo auto` MCP Dispatch And Controls

Acceptance criteria:

1. Packaged `ooo auto` entrypoints dispatch through the mandatory
   `ouroboros_auto` MCP path when claiming the supported release workflow.
2. Driver/brake controls supplied by the user are preserved through packaged
   skill dispatch.
3. Start, resume, retry, status, and result surfaces report truthful job and
   session state.
4. The supported path emits the required `auto.interview.*` and provenance
   evidence for release-supported flows.
5. The slice does not implement improved-throughput claims, Product-or-Die
   policy, or dependent auto-fill behavior while #1258/#1256 remain
   unresolved.

Verification:

- `uv run pytest tests/unit/auto tests/integration/auto -k 'mcp or packaged or dispatch or resume'`
- `uv run pytest tests/unit/auto/test_interview_driver_event_store_wiring.py`
- Full release pack before final readiness.

#### Plugin Permission, Hook, Lifecycle, And Audit Contract

Acceptance criteria:

1. Core plugin lifecycle operations enforce declared permissions before
   privileged actions run.
2. Hook validation rejects malformed or unauthorized hook definitions.
3. Runtime-boundary checks prevent plugins from crossing unsupported execution
   surfaces without an explicit contract.
4. Plugin operations that mutate state or execute hooks leave audit evidence
   sufficient for release diagnostics.
5. Lockfile, digest, and validation behavior remains stable for installed
   plugin packages.

Verification:

- `uv run pytest tests/integration/plugin tests/unit/plugin`
- Full release pack before final readiness.

#### Projection And Workflow IR Evidence Boundary

Acceptance criteria:

1. Projection records remain rebuildable read models over emitted EventStore or
   journal facts.
2. Run, Stage, Step, Artifact, and Verdict facts needed for release evidence
   can be reconstructed from current events.
3. Workflow IR remains the planned graph and conformance substrate, separate
   from observed projection records.
4. Projection code does not redefine verifier acceptance semantics or move
   approval authority out of the owning evidence/HITL surfaces.
5. Boundary docs stay consistent with `projection-v1-scope.md`,
   `workflow-ir-v1.md`, and the conformance tests.

Verification:

- `uv run pytest tests/integration/test_mechanical_eval_projection.py tests/conformance/workflow_ir`
- Full release pack before final readiness.

#### Release Wording For Throughput And Policy-Dependent Claims

Acceptance criteria:

1. Release notes and readiness docs do not claim improved `ooo auto`
   throughput until #1258 is accepted, closed, or explicitly deferred by the
   owner.
2. Release notes and readiness docs do not claim Product-or-Die auto-fill
   behavior until #1256 is accepted and #1263 is implemented or explicitly
   scoped.
3. Any deferred throughput, policy, or auto-fill claim links the owning issue
   and states the disposition outcome.
4. #1279/#1280 remain recorded as merge/defer gates rather than duplicated
   local implementation work.
5. External or policy exceptions are recorded in #961 or release notes before
   final readiness is claimed.

Verification:

- Documentation review against #961, #1258, #1256, #1263, #1279, and #1280.
- No code test is required for this external/policy gate.

### Required Automated Commands And Expected Outcomes

Use `UV_CACHE_DIR=/private/tmp/ouroboros-uv-cache` in this sandbox if the
default uv cache is not writable. A release candidate must use the same command
set on the candidate commit and record any accepted exception in #961 or the
release notes.

| Slice | Required automated command | Expected outcome |
| --- | --- | --- |
| `ooo health` copyable diagnostics | `uv run pytest tests/unit/cli/test_status.py -k health` | Passes. Unit coverage proves the Rich table remains present, detail lines are full and deterministic, exit semantics are unchanged, and secret values are not printed. |
| `ooo health` copyable diagnostics | `uv run pytest tests/e2e/test_cli_commands.py -k status_health` | Passes. The packaged CLI health path remains callable and preserves the release-supported behavior. |
| MCP malformed-turn and long-running runtime recovery | `uv run pytest tests/integration/orchestrator/test_agent_process_three_surface_acceptance.py` | Passes. AgentProcess CLI/MCP/plugin surfaces remain bounded and observable; any reproduced hang or false recovery state promotes the narrow failing path to P0. |
| MCP malformed-turn and long-running runtime recovery | Targeted MCP interview/runtime regression command for the reproduced path | Passes when a concrete path is selected. If no reproduced path exists, this remains a conditional command to add with the bug fix rather than a broad redesign mandate. |
| Packaged `ooo auto` MCP dispatch and controls | `uv run pytest tests/unit/auto tests/integration/auto -k 'mcp or packaged or dispatch or resume'` | Passes. Supported auto dispatch, packaged-path, resume, and state-reporting tests show no MCP bypass or false job/session status. |
| Packaged `ooo auto` MCP dispatch and controls | `uv run pytest tests/unit/router/test_packaged_auto_dispatch.py tests/unit/auto/test_interview_driver_event_store_wiring.py` | Passes. Packaged `ooo auto` dispatch remains routed through the mandatory MCP path and emits required interview/provenance evidence. |
| Plugin permission, hook, lifecycle, and audit contract | `uv run pytest tests/integration/plugin tests/unit/plugin` | Passes. Plugin lifecycle, permission, hook, lock/digest, validation, and audit contracts remain stable. |
| Projection and Workflow IR evidence boundary | `uv run pytest tests/integration/test_mechanical_eval_projection.py tests/conformance/workflow_ir` | Passes. Projection remains rebuildable from emitted facts and Workflow IR conformance stays separate from observed read-model records. |
| Release wording for throughput and policy-dependent claims | Documentation review against #961, #1258, #1256, #1263, #1279, and #1280 | Passes only when release notes/readiness docs withhold unresolved throughput and Product-or-Die claims or link explicit owner dispositions. This is an external/policy gate, not a code-test command. |
| All code slices | `uv run ruff check .` | Passes with no lint failures, or any failure is documented as an accepted release disposition in #961. |
| All code slices | `uv run mypy src` | Passes with no type failures, or any failure is documented as an accepted release disposition in #961. |
| All code slices | `uv run pytest` | Passes for the full suite, or any failure is documented with owner, root cause, and accepted release disposition in #961. |

### Required Manual Checks And Expected Outcomes

Manual checks are required where the release risk is about operator-facing
truthfulness, issue disposition, or documentation boundaries rather than only
testable code behavior. Record the observed outcome in #961, release notes, or
this readiness document before declaring the candidate ready.

| Slice | Required manual check | Expected outcome |
| --- | --- | --- |
| `ooo health` copyable diagnostics | Run `ouroboros status health` in a narrow terminal or with `COLUMNS=72`, using an isolated home and at least one long diagnostic detail such as a missing runtime CLI path. Inspect both the Rich table and the post-table plain-text detail lines. | The table remains readable; every row with detail also has a full line in the form `<check name>: <status> - <full detail>`; the command keeps the expected exit code; no secret values are printed. |
| MCP malformed-turn and long-running runtime recovery | For any reproduced #831/#925 path, start the affected MCP/background operation, request status/result, then exercise cancel or retry/resume if the operation does not complete normally. | The operation either completes, returns a bounded truthful failure, or exposes a usable cancel/retry/resume state. It must not hang silently, duplicate work, lose the session/job identifier, or print unsupported recovery instructions. |
| Packaged `ooo auto` MCP dispatch and controls | Invoke the packaged `ooo auto` path used in release docs and compare the visible job/session state, driver/brake controls, and handoff/resume wording against the MCP `ouroboros_auto` path. | The packaged path visibly routes through the supported MCP workflow, preserves user controls, reports the real session/job state, and does not claim unsupported throughput or completion behavior. |
| Plugin permission, hook, lifecycle, and audit contract | Exercise a representative plugin install/use/remove or fixture-backed plugin workflow and inspect permission prompts, hook behavior, generated audit evidence, and any denied operation. | Mutating or boundary-crossing plugin operations are gated by the expected permission contract, hooks run in the documented order, denied operations fail closed, and audit evidence is sufficient to explain what ran. |
| Projection and Workflow IR evidence boundary | After a representative AgentOS run or evaluation, inspect the Run/Stage/Step/Artifact/Verdict projection output and compare it with the Workflow IR/conformance artifact used for the same flow. | Projection records are rebuildable observations over emitted facts, Workflow IR remains the planned/conformance shape, and neither surface redefines verifier acceptance semantics. Missing required evidence promotes the narrow missing fact to a P0 code gate. |
| HITL authority and safety | Exercise or review a supported WAIT/RESUME approval path and inspect user-facing wording, approval ownership, and any persisted state shown after resume. | Approval authority is explicit and bounded; resume wording is truthful about what is persisted and what still needs human action; release docs do not over-claim unverified HITL persistence. |
| Release wording for throughput and policy-dependent claims | Review release notes, README/docs links, and #961 status against #1258, #1256, #1263, #1279, and #1280 before tagging. | Approved PR work is merged or explicitly deferred; `ooo auto` throughput claims are withheld unless #1258 is dispositioned; Product-or-Die/auto-fill claims are withheld unless #1256/#1263 are accepted and implemented or explicitly deferred. |

| Rank | Candidate narrow slice | Owning issue surface | Priority axis | Release-critical trigger | Acceptance criteria | Verification method | Disposition |
| ---: | --- | --- | --- | --- | --- | --- | --- |
| 1 | Preserve copyable `ooo health` diagnostics when Rich table output is narrow. | Local release-readiness fix under #925/#946 recovery-observability risk. | Recovery and observability for setup/runtime diagnosis. | Release users cannot see or copy the full config, database, credential, or runtime CLI-path detail needed to recover a failed setup. | `ooo status health` still renders the Rich health table, emits an untruncated plain detail line for each check with detail text, exits nonzero on error, and never prints secret values. | `uv run pytest tests/unit/cli/test_status.py -k health`; `uv run pytest tests/e2e/test_cli_commands.py -k status_health`; final pack: `uv run ruff check .`, `uv run mypy src`, `uv run pytest`. | **Selected highest-priority feasible slice.** It is small, local, already tied to a release-supported command, and removes a concrete diagnostic recovery failure without waiting for external gates. |
| 2 | Re-test malformed-turn / long-context MCP interview behavior and document any advertised-path hang. | #925 and #831. | Long-running stability, recovery, cancel/retry. | MCP interview start/status/result can hang or produce unrecoverable malformed-turn output in a supported path. | Supported MCP long-running paths either complete, return a truthful retry/resume state, or disclose a bounded unsupported condition. | Targeted MCP interview/runtime tests plus `uv run pytest tests/integration/orchestrator/test_agent_process_three_surface_acceptance.py`. | Next if rank 1 is already fixed or verification reproduces the #831 risk. |
| 3 | Reconfirm mandatory packaged `ooo auto` MCP dispatch cannot be bypassed. | #637 with #1157/#1170 context. | Tool/plugin integration and recovery truthfulness. | Packaged `ooo auto` can skip the MCP pipeline while claiming the supported release path. | Packaged entrypoints dispatch through `ouroboros_auto`, preserve driver/brake inputs, and expose truthful job/session state. | `uv run pytest tests/unit/auto tests/integration/auto -k 'mcp or packaged or dispatch or resume'`. | Current-next unless a bypass reproduces. |
| 4 | Reconfirm plugin lifecycle, permission, hook, and audit contract coverage. | #939. | Tool/plugin integration, security, permissions. | A plugin path can mutate state, run hooks, or cross the runtime boundary without the expected permission/audit evidence. | Core plugin operations enforce permissions, emit audit evidence, and keep hook contracts stable. | `uv run pytest tests/integration/plugin tests/unit/plugin`. | Current-next while tests pass; P0 code gate on contract regression. |
| 5 | Reconfirm projection/read-model evidence can reconstruct release diagnostics. | #946 with #956 boundary. | Observability and recovery evidence. | Release verification cannot reconstruct Run/Stage/Step/Artifact/Verdict facts from emitted events. | Projection remains a rebuildable read model over EventStore/journal facts and does not redefine Workflow IR acceptance semantics. | `uv run pytest tests/integration/test_mechanical_eval_projection.py tests/conformance/workflow_ir`. | Current-next unless projection verification fails. |
| 6 | Tighten release wording around `ooo auto` throughput and Product-or-Die claims. | #1258, #1256, #1263, #1157. | UX/documentation and policy gates. | Release notes claim improved throughput or policy-dependent auto-fill behavior before owner/product disposition. | Release notes withhold throughput and Product-or-Die claims unless owners disposition the gates. | Documentation review against #961/#1258/#1256; no code test. | External/policy gate, not a local code slice. |

Selection rationale: rank 1 is the highest-priority local implementation
slice because it fixes a candidate-facing recovery defect in a release-supported
command and is independently testable. The higher-level release blockers
(#1279/#1280 merge/defer and #1258 owner disposition) are external gates, so
duplicating them locally would violate the #961 boundary. The remaining code
candidates are important but become release-critical only if their tied
verification methods reproduce a failure.

## Selected Slice Acceptance Criteria

Selected slice: preserve copyable `ooo health` diagnostics when terminal width
or Rich table layout would otherwise truncate release-recovery details. This is
the narrow release-readiness implementation slice for the current pass because
it improves a supported diagnostic command without duplicating #961, approved
PR work, or policy gates.

The slice is complete only when all of these acceptance criteria pass:

1. `ooo status health` preserves the existing Rich health table and summary
   behavior for interactive users.
2. For every health check that has diagnostic detail, the command also emits a
   plain-text detail line containing the complete, untruncated detail string so
   users can copy paths, database locations, config locations, credential hints,
   and runtime CLI names from narrow terminals.
3. Plain-text detail output is deterministic enough for tests and support
   handoffs: each line identifies the owning check, appears after the table
   output, and does not depend on terminal width.
4. Error semantics are unchanged: a failing or degraded health state still
   exits nonzero, while an all-healthy state exits zero.
5. Secret handling is unchanged or stricter: diagnostic output may identify
   missing credential names or locations, but it must not print API keys,
   tokens, passwords, bearer values, or other configured secret values.
6. Existing CLI health tests cover the new untruncated detail output and the
   unchanged exit-code behavior.
7. Existing e2e CLI health coverage continues to pass, proving the command
   remains usable through the packaged command path.

Acceptance verification for this slice:

### `ooo health` truncation reproduction

The specific truncation point is the `System Health` table's `Runtime backend`
diagnostic row when the runtime CLI lookup fails with a long configured path.
At baseline, `_health_row()` flattened the check name and diagnostic detail into
one table label. The path begins truncating in that label after the
`claude CLI not found:` prefix, and the command emits no second diagnostic
block that preserves the full path.

Reproduction baseline:

- Commit: `837e56a88aad9214e41dc4b948f551510a50f755`
- Source tree: clean `HEAD` export at
  `/private/tmp/ouroboros-health-repro-head-837e56a`
- Working directory:
  `/private/tmp/ouroboros-health-repro-head-837e56a`
- Python: `Python 3.14.3`
- CLI entrypoint: `ouroboros status health` (`ooo` is not installed as a
  console script in this environment; `ouroboros` is the packaged entrypoint)
- Terminal width: `COLUMNS=72`
- Home/config root:
  `/private/tmp/ouroboros-health-repro-home-hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh`
- Runtime override:
  `OUROBOROS_CLI_PATH=/private/tmp/ouroboros-health-repro-missing-cli-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/claude`

Exact invocation:

```bash
PYTHONPATH=/private/tmp/ouroboros-health-repro-head-837e56a/src \
HOME=/private/tmp/ouroboros-health-repro-home-hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh \
NO_COLOR=1 \
ouroboros config init

COLUMNS=72 \
PYTHONPATH=/private/tmp/ouroboros-health-repro-head-837e56a/src \
HOME=/private/tmp/ouroboros-health-repro-home-hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh \
NO_COLOR=1 \
OUROBOROS_CLI_PATH=/private/tmp/ouroboros-health-repro-missing-cli-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/claude \
ouroboros status health
```

Observed output from the health command:

```text
                                 System Health
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳┓
┃ Name                                                                        ┃┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇┩
│ Configuration — /private/tmp/ouroboros-health-repro-home-hhhhhhhhhhhhhhhhh… ││
│ Database — missing; will be created on first run: data/ouroboros.db (/priv… ││
│ Runtime backend — claude CLI not found: /private/tmp/ouroboros-health-repr… ││
│ Credentials — anthropic key is still a template value                       ││
└─────────────────────────────────────────────────────────────────────────────┴┘
```

Observed exit code: `1`.

The config path, database path, and runtime CLI path are truncated with `…`,
and the `HEAD` implementation emits no following plain-text diagnostic lines.
That means the Rich table is the only source of truth for the failure details,
and the user cannot copy the complete paths from a narrow terminal.

Expected release-ready output keeps the table readable and then emits full
copyable detail lines after the table. For the failing runtime block, the
required plain-text fallback is:

```text
Runtime backend: error - claude CLI not found: /private/tmp/ouroboros-health-repro-missing-cli-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/claude
```

Actual baseline output only shows the truncated table row:

```text
Runtime backend — claude CLI not found: /private/tmp/ouroboros-health-repr…
```

Trigger / avoidance characterization:

- The loss happens during Rich table rendering, before shell redirection,
  subprocess capture, log collection, or issue-copy workflows see the output.
  Once Rich emits `…`, captured stdout contains only the shortened text and
  the full path cannot be reconstructed from the command output.
- The trigger is a narrow effective console width plus a long diagnostic
  string in the table row. In this reproduction, `COLUMNS=72` is enough to
  truncate the config path, database path, and runtime CLI path. `NO_COLOR=1`
  changes color handling only; it does not prevent width-based truncation.
- A TTY is not required to reproduce the problem. The shared CLI console uses
  Rich with terminal formatting enabled, so subprocess-based tests and captured
  stdout still follow Rich's measured or default width. Capture faithfully
  records the already-rendered table; it does not keep a hidden full-detail
  payload.
- Wider output can avoid the visible table truncation when the terminal or
  capture environment gives Rich enough columns for the longest row. The unit
  CLI runner sets `COLUMNS=240`, which avoids accidental table truncation for
  ordinary fixtures but is not a release guarantee for user terminals, CI logs,
  or support transcripts.
- Short diagnostics avoid the issue because the table row fits within the
  available width. Rows without details also avoid it. The release fix must
  therefore cover the general condition: any health check with a detail string,
  not only missing runtime CLI paths.
- The release-ready avoidance path is the post-table plain-text detail line.
  It is emitted through `typer.echo`, is independent of Rich table layout, and
  remains fully copyable in terminal output, subprocess stdout capture, and
  log transcripts. These lines are the diagnostic source of truth when the
  table is narrow.

```bash
uv run pytest tests/unit/cli/test_status.py -k health
uv run pytest tests/e2e/test_cli_commands.py -k status_health
```

### `ooo health` fixed-output manual verification

Manual verification on the fixed working tree used an isolated temporary home,
a narrow Rich console width, and a deliberately missing long runtime CLI path
so the command exercised the representative release-readiness diagnostics:
configuration path, database path, runtime backend recovery path, and credential
state.

Exact setup and invocation:

```bash
UV_CACHE_DIR=/private/tmp/ouroboros-uv-cache \
HOME=/private/tmp/ouroboros-health-manual-WTRAr3/home-hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh \
NO_COLOR=1 \
uv run ouroboros config init

COLUMNS=72 \
UV_CACHE_DIR=/private/tmp/ouroboros-uv-cache \
HOME=/private/tmp/ouroboros-health-manual-WTRAr3/home-hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh \
NO_COLOR=1 \
OUROBOROS_CLI_PATH=/private/tmp/ouroboros-health-manual-WTRAr3/missing-cli-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/claude \
uv run ouroboros status health
```

Observed result:

- Exit code: `1`, because the runtime backend check correctly reports the
  missing configured CLI path as an error.
- The Rich table remained readable at `COLUMNS=72`.
- Every diagnostic row with detail also emitted a full plain-text copy line
  after the table:

```text
Configuration: ok - /private/tmp/ouroboros-health-manual-WTRAr3/home-hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh/.ouroboros/config.yaml
Database: warning - missing; will be created on first run: data/ouroboros.db (/private/tmp/ouroboros-health-manual-WTRAr3/home-hhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhhh/.ouroboros/data/ouroboros.db)
Runtime backend: error - claude CLI not found: /private/tmp/ouroboros-health-manual-WTRAr3/missing-cli-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx/claude
Credentials: warning - anthropic key is still a template value
```

Manual disposition: pass. The fixed command preserves copyable diagnostics for
configuration, database, runtime recovery, and credential state while retaining
the expected nonzero error exit and without printing credential secret values.

Release-candidate verification still requires the full pack:

```bash
uv run ruff check .
uv run mypy src
uv run pytest
```

Failure handling: if any selected-slice acceptance criterion fails, classify
the defect as a P0 code gate under the local #925/#946 recovery-observability
risk and fix it before calling the AgentOS candidate ready. If the selected
slice passes but #1279/#1280 or #1258 remain unresolved, the remaining blocker
is external/policy disposition rather than duplicate local implementation work.

## Current Readiness Assessment

The current `main` branch has strong core signals: #961 baseline evidence is
captured, AgentProcess/plugin/Workflow IR checks pass, and the verifier and
auto-interview observability regressions have targeted passing tests.

The branch should not be described as fully release ready until the approved
open PR lane (#1279/#1280 at this snapshot) is resolved and #1258 receives an
explicit owner decision. If #1258 is deferred, say so in the release notes
because it directly affects the `ooo auto` AgentOS experience.
