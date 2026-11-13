# HEYMA-19 `wax voices tui`: recording-safe validation, 2026-11-13

Branch `validate/hm19-tui` (from main `b42a385`). Module `components/wax/src/wax/voices_tui.py`.
Everything ran under `/tmp/heyma-validate-heyma-19/` with `env -i`; see `environment.txt` for every variable, `scripts/`
for every harness. A mocked-only test is not counted as closing a "real" gap.

## Gap matrix

| # | Gap | Result | Evidence and why |
|---|---|---|---|
| 1 | Real players decode and play the TUI's 5 s clips, silently | **PROVEN** | All four players in the fallback order played the TUI's own clip (`cut_clip`: `pcm_s16le`, 16 kHz, mono, WAV, 5.000 s), rc 0, through the TUI's own `player_command()` / `make_player()` / `ClipLoop`. `pw-play` and `paplay` into a private PipeWire null sink (recorder on its monitor: 5.000 s of audio each, loop cycled 3 clips at 5.1 s per clip); `ffplay` into SDL's disk driver (163840 B = 5.12 s); `aplay` into an ALSA file PCM (160000 B = 5.000 s). Fallback order proven by PATH dirs holding one player each (`gap1-real-players/gap1-selection.txt`). No format or order change was needed. Transport caveat below. |
| 2a | Real terminal Alt+M | **PROVEN** for kitty 0.41.1 and alacritty 0.18.0-dev | Xvfb `:119`, `xdotool key alt+m / Down / Escape / Return / End / Home / Next / ctrl+c` and `xdotool type` against the private display only. kitty's own screen text via `kitty @ get-text` (`gap2-real-terminal/kitty-real-keys-screen-text.txt`): Alt+M opens the selector (`-` original, `+` target), Escape cancels, Return shows the confirm prompt, `n` backs out, typing names the voice (note on disk updated), End/Home/PageDown move, Ctrl+C exits 0. Screenshots of both terminals: `sheet-*-postfix.png`. |
| 2b | Raw slow-ESC split sequences through a pty | **PROVEN after a fix** (bug found, see below) | `pty-split-esc-BEFORE-fix.txt`: at 100 ms between bytes the tail of `ESC [ B` was typed into the NAME field (`BZed`). After the fix (`...AFTER-fix.txt`): 20 ms and 100 ms gaps are swallowed whole (`Zed`) for CSI, SS3 and `ESC [ 6 ~`, and Alt+M split at 20/100 ms still merges (`...altm-merge-outcome-AFTER-fix.txt`). Gaps of 200 ms and more still leak: indistinguishable from typing, by design. |
| 3 | Live relabel from the TUI with the real voice-id pass | **PROVEN** | TUI without `--no-sync`: named `voice-6e760e` "Validation Voice"; the background `Relabeler` ran the real `passes.run("voice-id", live=True)` (a real subprocess, real `frontmatters` edit) on 3 isolated copies of real production items: `**Speaker 1 (00:00):**` became `**Validation Voice (00:00):**`, `voices:` became `[[voice-6e760e\|Validation Voice]]`, exit 0, "relabelled 3 of 3". Run twice: with the stored fingerprints (`gap3-tui-live-cached-fingerprints.txt`) and with `WAX_VOICE_REFINGERPRINT=1` so the GPU TitaNet child really ran (`cached: False`, scores 0.930/0.944/0.940, `gap3-gpu-evidence.txt`, `gap3-tui-live-gpu.txt`). Production note and voice note: sha256 and mtime identical before and after. |
| 4 | Real-registry read-only dry run | **PROVEN** | TUI on a `sqlite3 .backup` copy (read-only URI) of the production ledger plus a copy of `~/d/Voices`, `--no-play --no-sync`, private tmux: rendered 64 voices, 54 live unnamed waiting (55 with one merged-away). Clip planning over all 54: **39 turn-derived, 15 `.ogg` fallback, 0 none**; every one of the 15 fallbacks is a legacy sighting with `turns` NULL (4 call, 11 room). 259 planned clips all cut from the real audio, 0 failures, every WAV within 0.15 s of its planned length. Quit without naming: ledger digest (voices, sightings, exemplars) and the voices-dir digest unchanged. |

Not proven, said plainly:

- Gap 1 transport. `pw-play`/`paplay` went to a private PipeWire graph, not the operator's; `ffplay` used SDL's disk driver, not
  its Pulse/ALSA backend; `aplay` used an ALSA file PCM, not the real PCM stack. Decode and playback of the clip is proven
  for each binary; the last hop to a real speaker was deliberately never exercised (a meeting may be live).
- Gap 2 emulators. Only kitty and alacritty: xterm, urxvt, xfce4-terminal, konsole and wezterm are not installed, and gnome-terminal
  was not run because it activates over the user's session D-Bus and could open a window on the operator's display. kitty's
  default `Alt` handling sends ESC+key (what the TUI expects); a terminal configured otherwise was not tried.
- Gap 3 paths not taken: the Jev/context branch (every speaker was a confident match, so no gateway call was made), registering a
  new voice, a multi-speaker transcript, and relabel after a merge (only after a name). The transcripts store capture was off
  (`WAX_TRANSCRIPTS=0`) as isolation, so the pass's snapshot capture was not exercised.
- The live-recording scenario itself: waxd's own sweep running at the same time as the TUI's relabel was not tried (no waxd).

## Bugs found by this validation, and fixed (commits on this branch)

1. **A split arrow key leaked into the NAME field** (`0b5df2e`). `run_curses` waited 80 ms for each byte after ESC; with 100 ms
   between the bytes of `ESC [ B` the `B` was typed (`BZed`, `6~Zed` for PageDown). New `EscapeReader`: first wait 150 ms (the price
   of a lone Esc, plus ncurses' 50 ms), each later byte of `ESC [`/`ESC O` 250 ms. Regression
   `test_escape_reader_waits_longer_for_the_tail_of_an_arrow_key_than_for_its_first_byte`. Before/after pty tables are in
   `gap2-real-terminal/`.
2. **A failing pass scribbled over the curses screen** (`d864764`). The relabel runs the real passes in-process; a failed pass logs a
   warning to stderr, which landed on the table and stayed (`gap3-tui-live-attempt2-stderr-bleed.txt`: the screen shows
   `oice-id result_apply_1/3 (32e3d0bcde6e3f89fa1d62 ...` fragments over the rows). New `StderrCapture` points fd 2 at a temp file for the
   session; the lines print after the terminal is restored under "messages written while the screen was up"
   (`gap3-tui-live-attempt3-fix-check-frontmatters-missing.txt`: clean screen, summary `relabelled 0 of 3`, exit 1). Regression
   `test_stderr_written_while_the_screen_is_up_is_held_back_and_shown_after`.
3. The hub (`skills/heyma/references/passes/voice-id.md`) now states both behaviours, in the same commit as the doc evidence.

The cause of the failure that exposed (2), kept here because it is real and not fixed: `passes._frontmatters_command` looks for
`$WAX_FRONTMATTERS`, then `~/code/frontmatters/.venv/bin/frontmatters` (does not exist on this host), then `frontmatters` on PATH. A `wax voices tui`
or `wax voices name` started from a shell without `frontmatters` on PATH fails every relabel with `result_apply_failed`. The TUI now
reports it clearly; the lookup itself is outside this ticket.

Observations, not fixed:
- Frontmatter rewrites by the pass change timestamp spelling in the note (`at: 2026-10-29T11:22:37.856Z` becomes
  `2026-10-29 11:22:37.856000+00:00`, see `gap3-note-diff.txt`); this is the known `frontmatters set` behaviour in the project memory.
- ESC then `m` more than ~150 ms apart is two keys, so `m` is typed into NAME (`m/named` in the 200 ms merge row). Inherent.
- `tmux send-keys End` (TERM=xterm-256color) sends `ESC [ 4 ~`, which terminfo does not list, so End did nothing in the old tmux
  harness (`gap4-tui-run.txt` step 5). Real terminals send `ESC O F` and End works (kitty and alacritty runs). A harness artefact, not a TUI bug.
- After moving from an unnamed row to a named one the status line can still say "playing 3 clip(s) of ..." although the player stopped.

## Dead ends, kept honest (files are in the folders)

- `gap3-tui-live-WRONG-ROW-attempt1.txt`: my first live run pressed Down 15 times on a ledger I had pruned to 3 items (every voice then had 0
  items and the order changed), so it named `voice-3b550b` instead of `voice-6e760e`. Only the isolated copy was affected; the
  fixture was rebuilt and the script now aborts unless the cursor row is `voice-6e760e`.
- A first private PipeWire used the stock `pipewire.conf` by mistake (env var ignored) and a second had no session manager
  (streams stayed `suspended`, one probe hung until killed); `scripts/start_private_audio.txt` has the working recipe (policy-only WirePlumber).
  Two `pkill -f` calls matched my own shell's command line and killed it; two `pkill -x pw-play/paplay` calls were broader than
  they should have been (no such processes of anyone else were running; pids are now matched through `/proc/<pid>/environ`).
- Display `:99` already existed (another session's Xvfb); I used `:119` and never signalled `:99`.

## Production was not touched; and what changed on its own

Baseline (`baseline/baseline-*.txt`, taken first) and after (`baseline/after-*.txt`, taken last):

- waxd: pid 5494, started Thu Nov 12 12:46:06, before and after. Never signalled, restarted or reconfigured. No systemd unit,
  tray, encoder or Candybar job touched. No mic or capture device opened, no sound on the desktop sink (default sink unchanged), no
  xdotool on `:0`/`:1`.
- Production reads only: `wax status`, files, and `file:...?mode=ro` SQLite URIs (a `.backup` into /tmp).
- **The recording ended during the run, by the operator.** Baseline `wax status`: `stream recording`, rid `20261113-143735-0cca00`
  (started 14:37, hotkey). After: `stream ready`. It stopped at 15:29:26 EST, when `journalctl --user` shows
  `Started app-gnome-wax\x2dtoggle ... launched by gsd-media-keys` followed by the `wax-20261113-143735-0cca00.scope` ending; it
  finalized normally (`session.ended`, 3110.3 s, 11,688,026 B, S3 size+md5 verified, transcribed, `complete`; ledger transitions seq
  5369-5375). I issued no `wax rec`, no signal and no write to the stream/ledger; the only things I ran near that minute were
  private-sink player probes. So "recording state unchanged" does not hold, and the cause was the hotkey, not this validation.
- Ledger row counts, baseline then after (`baseline/*-ledger-counts.txt`): `voices` 64 and 64 (unchanged), `voice_exemplars` 358 to 359,
  `voice_sightings` 952 to 957, `items` 934 to 936, `transcripts` 675 to 677, `outbox` 30910 to 30995, `passes` 4992 to 5010. The deltas are
  waxd's own work on the two items that arrived (the finished recording and a Telnyx call, both `complete` per the journal) plus
  its sweeps; none came from this run, which only ever opened the ledger read-only.
- Production voice note and transcript note used as fixtures: identical sha256 and mtime before and after
  (`gap3-live-relabel/gap3-prod-before.txt`, `gap3-prod-after.txt`).
- Everything I started was stopped: private pipewire, wireplumber, Xvfb `:119`, kitty, alacritty, tmux servers. Output files stay in
  `/tmp/heyma-validate-heyma-19/` (fixtures, WAVs, screenshots).

## Reproduce

`scripts/` holds the harnesses; paths inside are the `/tmp/heyma-validate-heyma-19` ones I used. Order: `pwenv.sh` + `start_private_audio.txt`
and `loop_real.py` (gap 1); `term_launch.sh`, `X`, `drive_kitty_text.sh`, `pty_esc.py` (gap 2); `build_real3.py`, `env_real3.sh`,
`prove_isolation.py`, `live_relabel.sh` (gap 3); `census.py`, `fallback_reasons.py`, `real4_tui.sh` (gap 4).
Tests: `cd components/wax && python3 -m pytest tests/voices_tui_test.py` (53 passed).
