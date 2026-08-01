# Evidence: HEYMA-1 — Componentize Wax and repair repository hygiene

## Issue
- Ticket: HEYMA-1
- Milestone / horizon: no active milestone
- Worker: heyma_1_implement / Codex GPT-5
- Orchestrated by: momo
- Operator feedback: organize HeyMa by subcomponent, group all Wax-owned files idiomatically, update location-dependent code, and replace committed generated skill fanouts with the Skillex registry model.

## Acceptance Criteria
1. Wax implementation, launchers, configuration, tray assets, documentation, deployment template, packaging metadata, and tests live under one component boundary.
2. Stable root launchers remain available while source and resource lookup work independently of the caller's current directory.
3. Existing live runtime audio and database directories remain at the repository root and keep their established override behavior.
4. Service deployment uses the stable launcher, the componentized daemon runs successfully, and the superseded watcher is inactive.
5. BMAD skills are declared through a portable, version-pinned Skillex manifest; generated CLI fanouts are removed and ignored, while genuine local customization is preserved separately.
6. Mise secret injection and skill synchronization work after the repository rename and from nested directories.
7. Ignore rules exclude generated/runtime material without hiding component source, tests, assets, or overrides.
8. The supplied green, red, and yellow tray images remain repository-delivered with correct ready, recording-precedence, and fault mappings.

## Repo Changes
- Branch: `heyma-1-tray-icons`
- Wax is grouped under `components/wax/{src,bin,config,assets,docs,tests,deploy}` with packaging metadata in `components/wax/pyproject.toml`.
- Stable executable compatibility shims remain at `bin/wax` and `bin/waxd`.
- Component location discovery is centralized in `components/wax/src/wax/component.py`; runtime root compatibility remains in `components/wax/src/wax/paths.py`.
- The tracked user service template is `components/wax/deploy/systemd/user/waxd.service` and targets the stable root shim.
- `.agents/skills.json` declares 75 BMAD skills from Skillex pack `6.10.2` using portable registry paths.
- `.mise/scripts/provision-bmad-skills.py` and `.mise/scripts/sync-skills.py` derive the repository root from their installed location and validate explicit root overrides.
- Generated `.agent/skills` and `.agents/skills` fanouts are removed from version control and ignored at root or nested working directories.
- The genuine review-lens customization is retained at `.skill-overrides/bmad-review/references/lens-verification-gap.md`.
- `mise.toml`, `.mise/tasks/base.toml`, `.gitignore`, and `README.md` reflect the componentized layout and renamed repository.
- Runtime audio, databases, queues, and user-owned media were not moved or rewritten.

## Verification
- `python3 -m unittest discover -s components/wax/tests -p '*_test.py' -v` → 14 tests passed.
- Running the component suite from `/tmp` → 14 tests passed, proving current-directory independence.
- `python3 -m unittest discover -s .mise/scripts/tests -p 'test_*.py' -v` → 8 tests passed, including nested-directory root resolution, traversal rejection, and registry symlink containment.
- The unrelated root regression suite → 5 tests passed.
- `python3 -m compileall` for component and skill scripts → clean.
- Packaging metadata and component launchers were independently inspected; behavioral component tests exercise the packaged source layout.
- Two provision passes and two synchronization passes → stable and idempotent; no nested fanouts recreated.
- Nested `mise hook-env` → exit code 0; 1Password injection and skill hooks complete successfully.
- `git diff --cached --check` → clean.
- Staged-scope audit → 229 paths with zero staged files from runtime media, `.project.json`, `.env.op`, `_bmad`, `_bmad-output`, Hermes, or `AGENTS.md`.
- Ignore probes → root runtime and nested generated fanouts ignored; component paths and `.skill-overrides` visible.
- `systemctl --user is-enabled waxd.service` and `systemctl --user is-active waxd.service` → enabled and active.
- Installed daemon resolution → `/home/delorenj/HeyMa/bin/waxd` launches `/home/delorenj/HeyMa/components/wax/bin/waxd`; service documentation points to the component design document.
- `audio-watcher.service` → disabled and inactive.
- `bin/wax status --json` → stream ready; pipeline intentionally stopped with scheduler disabled.
- Tray image validation → all three supplied 2000×2000 RGBA PNGs decode successfully; automated tests cover green ready, yellow fault/stopped, and red recording precedence.

## Ledger Update
- Bloodbank decisions recorded for ticket intake, repository-binding repair, review-lane mapping repair, operator-feedback hold, and the `components/wax` architecture boundary.
- Relevant decision event IDs: `2e35e648-8e1d-4152-bd03-84df0da83b15`, `fe1c0df1-3851-41ec-8838-e9b42463d64c`, and `14bd2cd7-4e6c-4575-9682-8a07cd8f14f8`.
- Ledger updated: yes

## Known Gaps
- The three operator-supplied tray images remain 2000×2000; image optimization is outside this refactor.
- The working tree contains unrelated user changes that are deliberately excluded from the staged HEYMA-1 deliverable.
- `AGENTS.md` is deliberately excluded because its working-tree replacement is session-provided project guidance rather than an implementation artifact.

## Close Recommendation
- Close recommendation: ready
- Rationale: the expanded operator feedback is implemented in the staged set, live service behavior is healthy, portability and hygiene regressions are covered, and independent scope and quality gates accepted the release candidate without drift or blocking findings.
