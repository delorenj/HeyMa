---
review-type: reality-check
artifact: ../ARCHITECTURE-SPINE.md
reviewed-at: '2026-08-12T16:15:07-04:00'
verdict: conditional-pass
reviewer: independent-architecture-gate
---

# Reality Check — HeyMa Federated Audio Control Plane

## Verdict

**CONDITIONAL PASS.** The central choice—a polyrepo system in which HeyMa owns
the integration/control plane while Vinyl and Voxxy remain autonomous
products—is supported by the repositories and live deployment. There are
separate Git histories, distinct runtimes and service lifecycles, no
cross-product implementation imports, and already-usable headless
status/control surfaces.

The architecture is not ready to be treated as an implementation contract
unchanged. Two safety rules need correction before controls are built, and the
Phase 0 product-contract work is a hard prerequisite rather than cleanup:

1. Wax restart inhibition must include an active queue job, not only an active
   microphone.
2. Vinyl needs one authoritative status contract covering both its local daemon
   and remote-serving daemon before microphone precedence or restart safety can
   be guaranteed.
3. The currently unversioned and partly contradictory product surfaces must be
   frozen before adapters are implemented.

No evidence found justifies a source monorepo today.

## Review Method and Evidence Boundary

This review used only current local reality on 2026-08-12:

- source and Git metadata from HeyMa at `b996f45`, Vinyl at `660bd8b`, and
  Voxxy at `72cf81e`;
- current user units, process state, command resolution, state mirrors, JSON
  command output, Docker state, and StatusNotifier registration;
- the exact system Python/GI/GTK/systemd packages installed on the target host.

All inspection was read-only. The system had an active Wax recording during
inspection, so no service, capture, queue, or tray mutation was attempted. Web
research was unnecessary because the question is the fit of this architecture
to this deployed host and these local products.

Assessment labels used below:

- **Verified** — directly true of current source or deployment.
- **Grounded target** — not implemented yet, but compatible with proven reality.
- **Conditional** — sound only after a named prerequisite is delivered.
- **Revise** — the written rule is incomplete or unsafe against current reality.

## Tier 1 — Must Resolve Before Adding Tray Controls

### RC-1 — Wax restart safety is under-specified

**Severity: High · Decision impact: AD-4, AD-12 · Disposition: Revise**

AD-12 disables restart only while a component reports an active microphone.
That is insufficient for Wax. `waxd` owns the background `Worker` thread, and
that thread synchronously owns the archive/transcribe/enrich/park operation
(`components/wax/src/wax/worker.py`). The deployed unit uses
`KillMode=mixed` (`components/wax/deploy/systemd/user/waxd.service:13`). A
restart while `inbox.active_item` is set can terminate a live transcription or
enrichment job even when the microphone is idle.

The companion migration plan already states the stronger invariant at
`MIGRATION-PLAN.md:22-23`, but Phase 3 and the spine fall back to the weaker
microphone-only rule.

**Required gate:** Model restart eligibility with component-defined inhibitors,
not one universal boolean. At minimum, Wax restart is disabled when any of
these is true:

- the stream is recording or finalizing;
- `inbox.active_item` is non-null;
- another Wax action is already in flight.

The disabled reason should identify the exact inhibitor. This preserves the
general AD-12 mechanism while allowing each adapter to define what “busy”
means.

### RC-2 — Vinyl has no authoritative aggregate status while both daemons run

**Severity: High · Decision impact: AD-7, AD-10, AD-12 · Disposition: Revise**

The live host runs both `vinyld.service` and `vinyld-serve.service`. Their
responsibilities are legitimately independent, but their observation surface
is not:

- `vinyl status` always connects to the single local Unix socket
  `~/.local/state/vinyl/vinyld.sock` (`bin/vinyl:47-55`,
  `vinyl/rpc.py:14-17,52-58`). It therefore reports the local daemon only.
- Both the local daemon and remote-serving daemon write the same
  `~/.local/state/vinyl/state.json` (`vinyl/rpc.py:61-65` and
  `vinyl/server.py:174-187`). Whichever writes last owns the apparent truth.
- The live mirror had no schema identifier, source identity, heartbeat, or
  update timestamp. Its mtime was hours old while both services were active.
- Server-mode activity is written only on remote session boundaries, so the
  local CLI and the shared file can disagree about which dictation path is
  active.

This makes “last-known microphone active stays RED” and “restart is disabled
while active” impossible to prove for remote-client dictation. It also makes a
single component-level `vinyl` restart action ambiguous because two different
services exist.

**Required gate:** Vinyl must publish `vinyl.status.v1` as either:

1. one product-owned aggregate containing named `local`, `client`, and `serve`
   subservice observations, with product `microphone_active` computed as their
   logical OR; or
2. separately addressable component instances with an explicit aggregate rule.

Each observation needs source identity and a provider timestamp or generation.
Restart actions must name their exact service target and must consult the
aggregate activity state before execution.

### RC-3 — Phase 0 contracts are not present yet

**Severity: High · Decision impact: AD-3, AD-7, AD-8, AD-10, AD-14 · Disposition: Conditional**

None of the live product outputs currently carries the named schema/version
promised by AD-7:

- Wax emits an unversioned dictionary from `wax status`.
- Vinyl emits an unversioned dictionary and has no `--json` option (the command
  is always JSON).
- Voxxy `/healthz`, `voxxy health --json`, and
  `voxxy daemon status --json` are unversioned.

Additional current-surface facts matter to adapter design:

- The Wax design document shows HTTP-over-Unix-socket `curl` examples, but the
  daemon actually sends raw JSON immediately without parsing HTTP
  (`components/wax/bin/waxd:151-176`). A live `curl --unix-socket ...
  http://wax/status` failed with “Received HTTP/0.9 when not allowed.”
- `wax status` does not query `waxd`; it recomputes state from disk and always
  returns exit 0 (`components/wax/bin/wax:183-185`). Its freshly generated
  `updated_at` therefore cannot prove daemon reachability. The durable
  `var/state.json` mirror does contain daemon/tray fields, but freshness must be
  checked separately.
- Voxxy exposes two useful but different meanings of health. `/healthz` is
  `ok` when **any** engine is ready (`app/main.py:267-282`), while
  `voxxy daemon status --json` is `degraded` when an expected local container
  or engine is missing. The live host demonstrated exactly that split:
  `/healthz` was `ok` with VoxCPM and ElevenLabs available, while daemon status
  was `degraded` because VibeVoice was absent.

The migration plan correctly puts schema release and fixtures in Phase 0. That
phase must also designate one authoritative observation command/endpoint per
product, define reachability separately from derived state, and test the exact
health semantics—not merely add a schema-name field.

## Tier 2 — Must Resolve Before Native-Tray Cutover

### RC-4 — Wax is not headless yet

**Severity: Medium · Decision impact: AD-5, AD-9 · Disposition: Conditional**

Vinyl already proves the desired separation: `vinyld.service` uses its product
environment, while `vinyl-tray.service` is a separate `/usr/bin/python3`
process. Voxxy is containerized and headless. Wax does not yet have that
separation: `waxd` imports GI/GTK, constructs `Tray`, starts the worker, owns the
status socket, and enters `Gtk.main()` in one process
(`components/wax/bin/waxd`). Stopping `waxd` to remove its indicator also stops
the queue owner.

The written “Wax gains a no-tray mode” is therefore a real cutover prerequisite,
not an existing capability. Keep both native trays enabled until the shadow
tray passes parity and Wax can suppress only indicator registration without
changing capture, worker, socket, or state ownership.

### RC-5 — Vinyl discovery is currently checkout-coupled

**Severity: Medium · Decision impact: AD-1, AD-3, AD-8 · Disposition: Conditional**

`wax` and `voxxy` resolve on `PATH`; `vinyl`, `vinyld`, and `vinyl-tray` do not.
The deployed Vinyl units execute absolute paths under
`/home/delorenj/code/vinyl`, and the native tray constructs the same checkout
and venv paths itself. Vinyl is an independent Git repository and is operable,
but its server-host installation surface is not yet a stable packaged product
surface.

The migration plan's Phase 0 requirement to install a stable `vinyl`
executable is correct. HeyMa must not encode the current checkout or venv path.

### RC-6 — Wax currently spans two HeyMa checkouts

**Severity: Medium · Decision impact: repository/deployment boundary · Disposition: Add migration gate**

The deployed `wax` and `waxd` launch from `/home/delorenj/HeyMa`, while
`~/.local/bin/transcribe` points to `/home/delorenj/code/HeyMa/bin/transcribe`.
Wax discovers `transcribe` from `PATH` unless `WAX_TRANSCRIBE` is set
(`components/wax/src/wax/transcribe_adapter.py:121-138`). Both checkouts were at
the same HeyMa commit during inspection, so live behavior was consistent by
coincidence rather than one canonical deployment boundary.

Before repository reorganization, define Wax's complete owned surface
(including the transcriber) and install it from one canonical checkout or
versioned installation. Otherwise the adapter contract can be stable while the
actual engine behind it silently drifts.

### RC-7 — “Logs” is not the same execution class as a bounded action

**Severity: Medium · Decision impact: AD-4, AD-12 · Disposition: Clarify**

Voxxy's existing log command replaces itself with `docker logs -f`; Wax and
Vinyl diagnostics naturally use journal following. Those are intentionally
long-lived, whereas AD-12 requires actions to have a timeout and permits only
one in-flight action per component.

Treat “Open logs” as a UI opener that launches a terminal/log viewer and
returns, or expose a bounded tail snapshot. Do not run a follow command inside
the component action slot, where it would either be killed by the timeout or
block every subsequent action.

## Tier 3 — Verified Architecture Substrate

### Repository and ownership boundaries

| Reality checked | Evidence | Result |
| --- | --- | --- |
| HeyMa, Vinyl, and Voxxy have independent histories | Separate roots and origins: `delorenj/HeyMa.git`, `delorenj/vinyl.git`, and `delorenj/voxxy` | Verified |
| Wax is currently owned by HeyMa | 49 tracked paths under `components/wax/`; no nested `.git` | Verified |
| Products do not import one another | Git-grep found no HeyMa→Vinyl/Voxxy or Voxxy→HeyMa/Vinyl implementation imports; Vinyl references Wax only in docs/roadmap and tray provenance | Verified |
| Runtimes differ materially | HeyMa tray code uses system Python/GI; Vinyl engine uses its own Python 3.14 venv and sherpa-onnx; Voxxy uses a Python 3.12 CLI plus Docker/FastAPI/GPU sidecars | Verified |
| Release/deployment lifecycles are separate | Dedicated Git repos, user units for Wax/Vinyl, and independently running Voxxy containers | Verified |

These facts support AD-1 and undermine the main proposed benefit of a source
monorepo. Current shared code is presentation behavior, not product engine
code; adapters and contracts address that without merging release units.

### Deployed process topology

At inspection:

- `waxd.service`, `vinyld.service`, `vinyld-serve.service`, and
  `vinyl-tray.service` were independently active.
- Voxxy core and VoxCPM engine containers were active; VibeVoice was absent.
- No Wax/Vinyl unit had a cross-product `Requires=` or `PartOf=` dependency.
- Wax and Vinyl indicators were both registered with the live
  StatusNotifierWatcher under GNOME/Wayland.

This directly supports a separate `heyma-tray.service` attached only to the
graphical session and carrying no product lifecycle dependencies. It also
proves that partial-product health is ordinary reality, making AD-10's
fault-isolation mandatory rather than theoretical.

### Named control-plane stack

The versions in the spine match the intended **system-Python** runtime exactly:

| Spine claim | Live proof | Result |
| --- | --- | --- |
| CPython 3.13.7 | `/usr/bin/python3 --version` | Verified |
| PyGObject 3.50.0 | `/usr/bin/python3` import and `python3-gi 3.50.0-7` | Verified |
| GTK 3.24.50 | GI runtime and `gir1.2-gtk-3.0 3.24.50-1ubuntu2` | Verified |
| AyatanaAppIndicator3 GIR 0.1 / 0.5.94-1 | Namespace import plus installed GIR/library packages | Verified |
| systemd 257.9 | `systemctl --version` reported package `257.9-0ubuntu2.5` | Verified |

The interactive shell's `python3` is currently mise Python 3.14.4, so the stack
table should be read as `/usr/bin/python3`, consistent with AD-5 and both
existing tray launchers. The technology choice is a strong fit: the exact
GI/AppIndicator stack is already proven by two live indicators, and the
control core can remain dependency-light using stdlib JSON, `tomllib`,
subprocess argv, threading/executors, and HTTP.

### Existing public controls

| Product | Read surface proven | Safe daily controls proven | Boundary caveat |
| --- | --- | --- | --- |
| Wax | `wax status`, `wax queue`, raw socket JSON, `var/state.json` | recording toggle/start/stop, pipeline status, queue inspection, open paths via desktop | freeze one authoritative versioned status; implement no-tray mode |
| Vinyl | local Unix RPC, `vinyl status`, `state.json` | toggle/start/stop/cancel | install CLI on PATH and aggregate local/server status |
| Voxxy | public `/healthz`, `voxxy health --json`, `voxxy daemon status --json` | restart core, engine selection, health, logs | define partial-engine health semantics in `voxxy.health.v1` |

No adapter needs to import product packages or read a product database. AD-3
is therefore grounded once the current surfaces are made stable and versioned.

## Decision-by-Decision Gate

| Decision | Assessment | Reality-check conclusion |
| --- | --- | --- |
| AD-1 Federated product architecture | **Verified / Conditional** | Separate repos, runtimes, consumers, and deployments are real. Vinyl packaging must mature, but that is not a reason to merge source. |
| AD-2 HeyMa owns integration control plane | **Grounded target** | No aggregate owner exists today; placing adapters, UX, config, and stack tests in HeyMa avoids changing product ownership. |
| AD-3 Adapter-mediated integration | **Conditional** | Public surfaces exist and no imports/databases are needed. Phase 0 must choose and freeze authoritative surfaces first. |
| AD-4 Bounded tray authority | **Verified fit** | Every proposed daily action has an existing safe product or OS surface; destructive Voxxy reset and voice deletion can remain excluded. Restart and logs need the RC-1/RC-7 refinements. |
| AD-5 Standalone aggregate tray | **Conditional** | System-Python/AppIndicator fit is proven. Vinyl and Voxxy are independent of their trays; Wax requires no-tray support before cutover. |
| AD-6 Inward dependency direction | **Grounded target** | Appropriate for sharing one model between CLI and GTK without importing adapters into core. Nothing current conflicts. |
| AD-7 Dual versioned contracts | **Required, not current** | None of the three product schemas exists yet. This must be provider-owned Phase 0 work. |
| AD-8 Built-in discovery and overrides | **Conditional** | First-party fixed adapters fit three known products. Wax/Voxxy are discoverable; Vinyl is not yet on PATH. No plugin loader is justified. |
| AD-9 Products operate headlessly | **Conditional** | Vinyl daemon and Voxxy already do. Wax still couples tray construction and worker/status ownership in `waxd`. |
| AD-10 Fault-isolated observation | **Revise prerequisite** | Concurrent bounded polling is technically sound and partial failure is live reality. Vinyl cannot yet supply authoritative aggregate microphone state. |
| AD-11 Disposable aggregate projection | **Verified fit** | Product-owned state already exists; a second database would create dual ownership with no demonstrated need. In-memory last-known state is sufficient for v1. |
| AD-12 Safe action execution | **Revise** | argv/no-shell, timeout, allowlist, and one-flight rules fit. Restart inhibition must be component-specific and include Wax active queue work. |
| AD-13 Products own credentials | **Verified fit** | Wax owns MinIO configuration, Vinyl owns pairing tokens, and Voxxy owns API-key/1Password handling. HeyMa needs only non-secret discovery data. |
| AD-14 Consumer-driven compatibility tests | **Grounded target** | Essential under independent releases. Wax has tests; Vinyl has no provider contract suite; named schemas and cross-product fixtures do not yet exist. |

## Required Disposition Before Build Approval

The spine may retain the federated architecture, hexagonal control-core shape,
system-Python tray, normalized model, and phased rollout. Build approval should
be gated on these explicit dispositions:

1. Amend safe-action policy to use component-defined restart inhibitors and
   include Wax active queue work.
2. Choose Vinyl's aggregate-vs-instance status design and ship an authoritative
   `vinyl.status.v1` before implementing its HeyMa adapter.
3. Expand Phase 0 to test daemon reachability, status freshness, and documented
   transport semantics—not only JSON shape/version.
4. Select one authoritative Voxxy health meaning and preserve per-engine detail
   without conflating “service can synthesize” with “every preferred engine is
   ready.”
5. Put Vinyl on PATH, implement Wax no-tray mode, and collapse Wax's two-checkout
   deployment before native-tray cutover.
6. Define log access as a bounded snapshot or external viewer launch rather
   than a long-running component action.

With those gates incorporated, the architecture is reality-grounded and safe
to proceed. None of the findings changes the repository decision: Vinyl and
Voxxy should remain standalone products controlled through HeyMa adapters.
