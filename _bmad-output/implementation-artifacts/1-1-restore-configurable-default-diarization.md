# Story 1.1: Restore Configurable Default Diarization

Status: ready-for-dev

## Story

As a Wax operator,
I want diarization enabled by default through Canonical Config,
so that new recordings reliably become speaker-attributed dialog without hidden service flags or silent degradation.

## Requirements Traceability

- FR-1 — Canonical Wax configuration
- FR-2 — Diarize by default
- FR-3 — Truthful diarization outcome
- NFR-1 — Audio safety
- NFR-3 — Enrichment independence
- NFR-4 — Compatibility
- NFR-5 — Test isolation
- NFR-6 — Runtime readiness

## Acceptance Criteria

1. **Canonical path and default**

   **Given** no compatible environment override and no Wax config file
   **When** either the Wax CLI or daemon resolves transcription policy for a newly starting operation
   **Then** it looks for `$XDG_CONFIG_HOME/wax/config.toml`, falling back to `~/.config/wax/config.toml` when `XDG_CONFIG_HOME` is unset or empty
   **And** it treats a relative `XDG_CONFIG_HOME` as invalid rather than resolving it against the process working directory
   **And** `transcription.diarize` resolves to `true`
   **And** Wax creates neither the config file nor its parent directories as a side effect of reading defaults.

2. **Intentional opt-out**

   **Given** Canonical Config contains:

   ```toml
   [transcription]
   diarize = false
   ```

   **When** Wax transcribes a new item
   **Then** it passes an explicit `--no-diarization` policy to the transcriber
   **And** it publishes a valid non-diarized base Transcript
   **And** it records `requested: false`, `diarized: false`, and `state: not_requested`
   **And** it does not represent the intentional opt-out as unavailable, failed, or dependency-related.

3. **One resolver and deterministic precedence**

   **Given** more than one policy source is present
   **When** Wax resolves diarization for a transcription
   **Then** precedence is operation argument, validated legacy `WAX_DIARIZATION`, Canonical Config, built-in default
   **And** accepted environment values are case-insensitive, surrounding whitespace is ignored, true is `1|true|yes|on`, and false is `0|false|no|off`
   **And** any other non-empty value produces a field-specific configuration error instead of silently becoming true
   **And** Wax passes exactly one of `--diarization` or `--no-diarization` to the child process
   **And** a positive operation override can override a false environment value without inheriting a poisoned runtime path
   **And** the rendered and installed Wax systemd unit no longer contains `WAX_DIARIZATION=1`.

4. **Lazy, contained configuration errors**

   **Given** malformed TOML, an unreadable config file, a non-table `[transcription]` value, or a non-boolean `transcription.diarize`
   **When** transcription policy is resolved
   **Then** Wax reports a typed error naming the exact resolved file and `transcription.diarize` where applicable
   **And** it does not fall back to the built-in value
   **And** loading is transcription-scoped rather than import-time or daemon-startup work
   **And** capture, tray/state inspection, queued source audio, prior Transcripts, S3 references, and existing ledger records remain intact
   **And** the worker contains the error per item or pipeline preflight, does not terminate its thread, and does not hot-loop or repeatedly mark the queued item failed.

5. **Healthy default diarization**

   **Given** the effective setting is true, the declared runtime probe succeeds, and a bounded multi-speaker fixture is supplied
   **When** the item is transcribed
   **Then** the Transcript body contains speaker-attributed dialog
   **And** top-level frontmatter, structured in-band metadata, adapter result, and ledger agree on `diarized: true`
   **And** the durable diarization sub-outcome records `requested: true`, `state: completed`, and no failure reason.

6. **Truthful degradation without losing ASR**

   **Given** the effective setting is true
   **When** the exact Sortformer backend cannot import, the model cannot load, inference fails, or the selected transcription backend cannot support diarization
   **Then** Wax still preserves and publishes a valid base Transcript and preserves the source audio
   **And** it records a separate non-success diarization sub-outcome with `requested: true`, actual `diarized: false`, `state: unavailable|failed`, a stable reason code, and bounded actionable detail
   **And** an empty speaker-segment list is not itself used to infer whether execution was disabled, unavailable, failed, or completed
   **And** the Wax Item's base transcription is not converted into a generic process failure or retried from scratch on CPU
   **And** sibling enrichment and verified safe parking can continue
   **And** no completed Diarize pass row is written by this story.

7. **Reproducible runtime and checkout integrity**

   **Given** Wax is installed or updated from a clean checkout
   **When** its dedicated diarization runtime is created and verified
   **Then** dependencies are installed from a committed, pinned, non-editable manifest or lock
   **And** the exact interpreter Wax will execute imports `whisperlivekit.diarization.sortformer_backend_offline` in a bounded readiness probe
   **And** the setup does not install editable `whisperlivekit` metadata pointing back at the HeyMa repository
   **And** Wax normally resolves the transcriber from the same canonical checkout as its running Wax source
   **And** an explicit `WAX_TRANSCRIBE` override remains supported but is validated
   **And** an unrelated or mismatched PATH checkout cannot be selected silently.

8. **Retry-safe truth and isolated verification**

   **Given** a Transcript is reprocessed after policy or runtime repair
   **When** the actual diarization result changes between false and true, or true and false
   **Then** frontmatter, adapter output, and the existing transcript ledger row converge on the latest actual result without overwriting the Transcript or source audio
   **And** `diarized`, `engine_model`, and the interim diarization outcome fields are updated on conflict
   **And** automated tests isolate `HOME`, `XDG_CONFIG_HOME`, `WAX_ROOT`, `WAX_VAULT`, ledger, logs, pass registry, PATH, transcriber, runtime, and lock paths from the operator's live state.

## Tasks / Subtasks

- [ ] Add Canonical Config loading and a typed transcription policy resolver (AC: 1, 3, 4)
  - [ ] Extend `components/wax/src/wax/config.py` with a typed `ConfigError`, canonical path resolution, binary-mode `tomllib` loading, and strict validation.
  - [ ] Treat unset and empty `XDG_CONFIG_HOME` as `~/.config`; reject relative XDG paths consistently with the XDG Base Directory Specification.
  - [ ] Support an absent file, absent `[transcription]`, and absent `diarize` as default-true cases without any writes.
  - [ ] Reject malformed TOML, unreadable files, a non-table `transcription`, and non-boolean `diarize` with path/field diagnostics.
  - [ ] Implement one lazy resolver with `operation > WAX_DIARIZATION > TOML > true` precedence and strict legacy boolean tokens.
  - [ ] Resolve once for each newly starting transcription; do not cache a stale value for the daemon lifetime.

- [ ] Wire the resolved policy explicitly through Wax and the transcriber (AC: 2, 3, 4)
  - [ ] Give `transcribe_adapter.transcribe()` a typed per-operation override rather than inferring policy from arbitrary `extra` arguments.
  - [ ] If direct `wax transcribe` operation flags are exposed, make `--diarization` and `--no-diarization` mutually exclusive and pass the typed value through the same resolver.
  - [ ] Append exactly one explicit child flag after rejecting contradictory caller flags.
  - [ ] Remove the `DIARIZATION_VENV=.diarization-disabled` policy hack; runtime discovery must not become an undocumented precedence layer.
  - [ ] Catch typed config failures at the transcription boundary so unrelated `wax rec`, state, queue, and daemon startup behavior remains usable.
  - [ ] Preserve the child stdout contract: successful execution emits exactly one Transcript path; diagnostics and structured metadata stay on stderr/retained logs.

- [ ] Make raw diarization execution return a truthful structured sub-outcome (AC: 5, 6, 8)
  - [ ] Replace boolean/list truthiness as the status contract with at least `requested`, actual `diarized`, `state`, `reason_code`, and bounded `detail`.
  - [ ] Use `not_requested`, `completed`, `unavailable`, and `failed` as the interim state vocabulary; reserve generic EP state/history for Story 1.2.
  - [ ] Probe the exact backend import used by execution, not only `librosa` and `nemo`.
  - [ ] Preserve distinct stable reasons for backend import/readiness, unsupported backend, model load, and inference failure.
  - [ ] Define backend-completed-with-zero-speakers independently from disabled/unavailable/failed so an empty list is not an error channel.
  - [ ] Keep a requested diarization failure non-fatal to valid base ASR; do not return a generic nonzero that triggers `bin/transcribe`'s full CPU retry.
  - [ ] Classify the Groq path explicitly as unsupported/unavailable when diarization is requested rather than silently returning an ordinary non-diarized success.
  - [ ] Preserve the current linear-memory Sortformer loop, CPU execution, overlap-based speaker rendering, structured progress, and no-sidecar contract.

- [ ] Persist interim outcome truth without pre-implementing the Diarize EP (AC: 2, 5, 6, 8)
  - [ ] Include the structured sub-outcome in `Transcription-Metadata` and the adapter return value.
  - [ ] Persist rebuildable outcome data in Transcript frontmatter under a transcription-owned namespace such as `wax.diarization`; keep top-level `diarized` as the actual boolean.
  - [ ] Add additive transcript-ledger fields for requested/state/reason, or an equivalently queryable durable projection that distinguishes intentional opt-out from failure.
  - [ ] Migrate existing ledgers additively and keep old rows readable.
  - [ ] Fix the `ON CONFLICT(item_id)` update so `diarized`, `engine_model`, and new outcome fields converge on retry.
  - [ ] Do not register a Diarize EP, create `passes` history, replace a historical Transcript body, or add a retry queue in this story.

- [ ] Restore a reproducible dedicated diarization runtime (AC: 5, 6, 7)
  - [ ] Add a committed dependency declaration plus lock or other exact resolution artifact for the supported Python/runtime combination.
  - [ ] Pin a supported non-editable source that actually supplies the `sortformer_backend_offline` interface, or replace that private dependency with an owned stable adapter while preserving behavior.
  - [ ] Add an idempotent setup/verification command that builds from an empty environment and fails if the exact backend cannot import with the execution interpreter.
  - [ ] Replace the broken `uv pip install -e .` remediation text in `bin/transcribe`.
  - [ ] Run expensive/model-touching readiness only in a bounded subprocess during setup/preflight, never during config import, GTK startup, or once-per-second tray polling.
  - [ ] Make local and remote launch paths obey explicit policy flags and the same readiness semantics while preserving remote locking/copy behavior.

- [ ] Collapse normal Wax execution onto one checkout (AC: 7)
  - [ ] Resolve the embedded same-checkout `bin/transcribe` before PATH; retain `WAX_TRANSCRIBE` as the explicit override.
  - [ ] Validate the resolved executable and provide an actionable resolved-path diagnostic.
  - [ ] Reject a deliberately mismatched second-checkout/PATH fixture; do not rely only on matching Git HEAD because dirty files and runtimes can still diverge.
  - [ ] Preserve relocation safety and avoid hardcoding `/home/delorenj` in source.

- [ ] Remove the service-level policy pin safely (AC: 3, 7)
  - [ ] Remove only `Environment=WAX_DIARIZATION=1` from `components/wax/deploy/systemd/user/waxd.service`; retain segment, 300 MB, and 3-hour controls.
  - [ ] Extend rendering tests to prove the generated unit contains no diarization override and does not write the live user unit.
  - [ ] Document rollout verification: render/install, daemon-reload, restart only under the Wax idle/reconfiguration safety gate, then assert the live process environment has no `WAX_DIARIZATION`.

- [ ] Add hermetic regression and acceptance coverage (AC: 1–8)
  - [ ] Add `components/wax/tests/config_test.py` for path fallback, absent-file/no-write behavior, valid booleans, strict invalid values, unreadable/malformed config, and all precedence layers.
  - [ ] Extend adapter/worker tests for explicit flags, operation-true-over-env-false, config-error containment, same-checkout resolution, safe parking, sibling enrichment, and retry upserts.
  - [ ] Extend the transcription contract for healthy, missing-import, model-load-failed, inference-failed, zero-speaker-success, Groq-unsupported, and structured metadata cases.
  - [ ] Add a small, provenance-documented, bounded multi-speaker fixture or deterministic generated equivalent; keep heavyweight/model tests explicitly marked while retaining a clean-runtime smoke gate.
  - [ ] Run the existing Wax and root transcription contract suites, shell syntax validation, rendered systemd verification, clean-runtime import smoke, and the bounded multi-speaker acceptance test.

## Dev Notes

### The Defect Is Not the Default Flag

Three layers already lean toward diarization: Wax only special-cases a false `WAX_DIARIZATION`, `bin/transcribe` auto-adds `--diarization` when a venv executable exists, and `scripts/transcribe.py` defaults its BooleanOptionalAction to true. The live failure occurs later: the preflight checks `librosa` and `nemo`, but execution imports `whisperlivekit.diarization.sortformer_backend_offline`; that import fails and is reduced to an empty list. The base Transcript then publishes with `diarized: false` and the item can be marked complete.

Production evidence for Wax Item `6927a6cd9fe492d7` is retained in `var/logs/6927a6cd9fe492d7/transcription.1.log`: the backend import fails, metadata reports false, and the item nevertheless reaches the base pipeline's complete state. Story 1.1 must repair both runtime readiness and truthful sub-outcome reporting.

### Interim Outcome Contract

Until Story 1.2 installs Diarize as a generic EP, use a transcription-owned durable sub-outcome. The minimum logical record is:

```json
{
  "requested": true,
  "diarized": false,
  "state": "unavailable",
  "reason_code": "backend_import_unavailable",
  "detail": "bounded actionable diagnostic"
}
```

This record is deliberately separate from base-ASR success. `completed` means the backend executed successfully; `diarized` reports whether speaker-attributed segments were actually produced. Therefore a legitimate zero-speaker completion is distinguishable from a disabled or failed invocation. Keep reason codes stable and machine-readable; sanitize and bound free-text detail.

Do not write `wax.passes.diarize` or a `passes` ledger row in this story. Story 1.2 owns registration, source-audio resolution, timed-segment persistence/body replacement, automatic/manual unification, and projection into generic EP history. Here, "retryable" means the source audio, base Transcript, requested state, and failure reason remain sufficient for Story 1.2 to retry later.

### Configuration Contract

Use standard-library `tomllib`; the component supports Python 3.11 and newer. Open TOML in binary mode. The file is read-only from Wax in this story—there is no implicit creation, migration, or settings-editing command.

Resolve config lazily at the start of each transcription operation. Both `components/wax/bin/wax` and `components/wax/bin/waxd` import transcription modules before they know which action will run, so import-time parsing would allow one malformed setting to disable recording and tray state. A configuration fault must stop only new transcription work, leave the item queued and untouched, expose a pipeline/preflight reason, and back off rather than producing a five-second failure loop.

Unknown TOML keys may be preserved/ignored for forward compatibility, but every supported key must be strictly typed. Missing `[transcription]` means defaults; a present non-table value is invalid. Follow the XDG rule that unset/empty uses `$HOME/.config` and relative base-directory values are invalid.

### Runtime and Checkout Evidence

The deployed topology currently spans two checkouts:

- `waxd` executes `/home/delorenj/HeyMa/bin/waxd`.
- PATH resolves `~/.local/bin/transcribe` to `/home/delorenj/code/HeyMa/bin/transcribe`.
- Only the second checkout contains `.venv-diarization`.
- Its Python 3.12 environment advertises `whisperlivekit 0.2.8`, but `direct_url.json` points editably to `/home/delorenj/code/HeyMa`, whose package sources were removed.
- Commit `5f82d3e` introduced auto-on diarization and setup; commit `1d21e8b` removed the root package, lockfile, setup script, and `whisperlivekit/**` without rebuilding that editable environment.

Do not solve this by pointing at the separate dirty `/home/delorenj/code/WhisperLiveKit` checkout or by reinstalling `-e .`. The committed runtime contract must work from an empty environment and pin a source/interface that really exists. Current upstream WhisperLiveKit documentation exposes Sortformer as an optional dependency, but its internal module layout is not a stable public contract; pin the known compatible interface or own an adapter rather than floating to latest.

### Existing Behavior That Must Survive

- Archive-first verified S3 handling and never-delete source policy.
- The exclusive 300 MB and 3-hour transcription gates.
- Wax queue serialization and `bin/transcribe`'s own blocking lock.
- Retained per-attempt logs and structured progress, including the separate diarization stage.
- `nice -n 15`, GPU/CPU ASR fallback, remote mode, and no duplicate full-ASR retry for a diarization-only failure.
- Duration/source-unchanged sanity checks and suspect quarantine.
- Exactly one stdout path on success; all logs and in-band metadata on stderr.
- No adjacent `.meta.json` sidecar.
- Dated Transcript naming, Wax identity/provenance frontmatter, title-slug behavior, sibling EP independence, and verified safe parking.
- No reads or writes to the operator's live config, ledger, vault, audio roots, systemd unit, or runtime from automated tests.

### Story Boundaries

This story owns Canonical Config, policy precedence, explicit child flags, runtime declaration/readiness, same-checkout resolution, truthful raw outcome, base-Transcript preservation, retry-safe projection, and isolated acceptance coverage.

Deferred deliberately:

- Story 1.2 — registered Diarize EP, applicability, source-audio/timed-segment contract, body mutation, pass history, and post-hoc execution.
- Story 1.3 — `wax ep -l|--list`, `wax info -i|--item-id`, human/JSON discovery and inspection.
- Story 1.4 — `wax job -p|--enrichment-pass -i|--item-id`, durable enqueue/claim/restart/retry behavior.
- Phase 0A — Syncthing/dropoff/inbox ownership migration.

### Project Structure Notes

Expected updates:

- `components/wax/src/wax/config.py`
- `components/wax/src/wax/transcribe_adapter.py`
- `components/wax/src/wax/ledger.py`
- `components/wax/src/wax/worker.py`
- `components/wax/bin/wax`
- `components/wax/deploy/systemd/user/waxd.service`
- `components/wax/deploy/install-systemd-user`
- `components/wax/tests/portability_test.py`
- `components/wax/tests/wax_integration_test.py`
- `components/wax/tests/worker_enrichment_test.py`
- `scripts/transcribe.py`
- `bin/transcribe`
- `tests/test_transcribe_contract.py`

Expected additions may include:

- `components/wax/tests/config_test.py`
- A committed diarization dependency manifest/lock and idempotent setup/readiness command in a Wax-owned location.
- A small multi-speaker fixture or deterministic fixture generator with provenance.

Do not add canonical project skills under `.codex/skills/`; repository-scoped skill source belongs under `.agents/skills/` per `AGENTS.md`.

### Verification

Baseline observed before implementation:

- Wax component suite: 43/43 passing.
- Root transcription contract: 3/3 passing.
- Exact live backend import: failing despite installed `whisperlivekit 0.2.8` metadata.

Required fast gates:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=components/wax/src \
  /usr/bin/python3 -m unittest discover \
  -s components/wax/tests -p '*_test.py' -v

PYTHONDONTWRITEBYTECODE=1 \
  /usr/bin/python3 -m unittest -v tests/test_transcribe_contract.py

bash -n bin/transcribe

components/wax/deploy/install-systemd-user --dry-run > /tmp/waxd-story-1-1.service
systemd-analyze --user verify /tmp/waxd-story-1-1.service
```

Required runtime/acceptance gates must use newly created isolated paths, not the live venv or user config:

```bash
"$STORY_DIARIZATION_VENV/bin/python" -c \
  'from whisperlivekit.diarization.sortformer_backend_offline import load_model, init_streaming_state'
```

Also exercise one bounded healthy multi-speaker run and one missing/broken-backend run, asserting source preservation, base-Transcript preservation, outcome truth, safe parking, and zero writes outside the fixture roots.

### References

- [PRD: Canonical Settings and Default Diarization](../planning-artifacts/prd.md#41-canonical-settings-and-default-diarization)
- [Epic 1 / Story 1.1 source](../planning-artifacts/epics.md#story-11-restore-configurable-default-diarization)
- [Wax runtime config](../../components/wax/src/wax/config.py)
- [Wax transcribe adapter](../../components/wax/src/wax/transcribe_adapter.py)
- [Local transcription and Sortformer integration](../../scripts/transcribe.py)
- [Transcription wrapper/runtime selection](../../bin/transcribe)
- [Wax systemd template](../../components/wax/deploy/systemd/user/waxd.service)
- [Wax architecture and preservation rules](../../components/wax/docs/WAX-DESIGN.md)
- [Python `tomllib` documentation](https://docs.python.org/3/library/tomllib.html)
- [XDG Base Directory Specification 0.8](https://specifications.freedesktop.org/basedir/0.8/)
- [uv project lock and sync semantics](https://docs.astral.sh/uv/concepts/projects/sync/)
- [uv project layout and committed lockfiles](https://docs.astral.sh/uv/concepts/projects/layout/)
- [WhisperLiveKit Sortformer installation guidance](https://github.com/QuentinFuxa/WhisperLiveKit)

## Dev Agent Record

### Agent Model Used

### Debug Log References

### Completion Notes List

### File List
