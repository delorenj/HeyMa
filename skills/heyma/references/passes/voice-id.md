# voice-id: who is speaking, and the voice registry

The `voice-id` pass fingerprints every speaker in a recording, matches each
one against the **voice registry**, renames a known voice's `Speaker N` turns
to its name, and registers a voice it has never heard: it writes a note for you
to fill in and notifies you. Added 2026-11-11, in `transcript-standard` v14 (v15 with the context step)
after `dictionary-correction`.

Code: `components/wax/src/wax/voiceprint.py` (fingerprints, matching and the
`decide` rules; numpy only, except the GPU embedder),
`components/wax/src/wax/voices.py` (the registry and `wax voices`),
`components/wax/src/wax/voice_id_pass.py` (the pass), and
`config/passes.d/{voice-id.yaml,bin/voice-id}`. Tests:
`components/wax/tests/voice_id_test.py`.

## What one run does

1. **Who spoke where.** A dual-channel call uses its ASR segments per channel
   (`channels` mode: `Me` on channel A, the caller's name or number on B). A
   diarized note uses its `**Speaker N (mm:ss):**` turns, each running to the
   next turn (`turns`). A single voice (a monolog: diarization skipped it) uses
   every timed ASR segment (`whole`, label `(whole recording)`); a note with no
   timed ASR kept (most notes before October have no `asr.json`) uses the
   whole recording, and the fingerprint drops the windows that disagree.
2. **Fingerprint.** In `.venv-diarization` (`DIARIZATION_PYTHON`), NeMo
   TitaNet-large (`nvidia/speakerverification_en_titanet_large`) on CUDA embeds
   each speaker's speech in 1.5 to 10 s windows (edges trimmed 0.15 s, at most 60
   windows spread through the recording). Windows that disagree with the
   speaker's mean (cosine < 0.35: overlap, a diarizer slip) are dropped; the
   rest average into one 192-number fingerprint. About 9 s to load the model and
   10 to 13 s per item in all. A rerun reuses the stored fingerprints and needs
   no GPU (`WAX_VOICE_REFINGERPRINT=1` forces a fresh one).
3. **Decide** (`voiceprint.decide`), what is known for certain first:

| Verdict | When | Effect |
|---|---|---|
| `owner` / `owner_new` | A call's **channel A** (our own leg, inbound and outbound), a far channel labelled with the owner's own name (him calling from another phone), or a **single-voice recording** scoring at least `owner_solo` (0.45, 0.40 against his other channel) and not clearly someone else | The owner's voice (`WAX_VOICE_OWNER`, `Jarad` in the YAML), registered named on first sight; learns every sighting |
| `caller_id` / `caller_id_new` | A call's far channel labelled with a **contact name** (not a number, `Channel B`, `Unassigned`, `Unknown...`) | That contact's voice, registered **named, with no notification**; learns every sighting. Overridden only when the voice clearly matches a different named voice (someone else on that phone) |
| `matched` | Best voice over its match bar and beating the next by the margin | Names the speaker; learns the fingerprint when the score is over `learn` |
| `new` | Every voice below its `new` bar, at least 20 s of speech, windows agreeing (consistency at least 0.5) | **Registers** a voice, writes its note and clip, and **notifies you** |
| `context` | A speaker that would be `uncertain` or `new`, where the transcript points at a named voice the voice does not rule out (see [Context](#context-when-the-voice-is-not-enough)) | Names and relabels the speaker; never registers, never learns an exemplar |
| `uncertain` | Between the bars, or two voices too close to call | Sighting recorded; nothing named or registered |
| `too_short` | Under 4 s of usable speech (or under 20 s for a stranger) | Sighting recorded |

4. **Relabel.** Only in `turns` mode, and only a generic `Speaker N` head (or a
   name this pass wrote there itself) becomes the voice's name; calls keep their
   `Me` and caller labels, and hand-written names are never touched. The body
   change is a `compare-and-replace`. Frontmatter `voices:` (clobberable) lists
   every non-ignored voice heard as `[[voice-id|Name]]` links.

**A name the pass wrote can be taken back.** Each sighting keeps the turn heads
(`mm:ss`) its diarizer label owned (`voice_sightings.turns`). Every run first
puts `Speaker N` back on those turns wherever they still show the name it wrote
(`voice_id_pass.unfold`; a hand edit is left alone), judges each diarizer label
again on its own cached fingerprint, and writes the names it stands behind now.
So two speakers it once folded into one name (the wife taken for the owner
before her voice was known) come apart, and a name it no longer believes goes
back to `Speaker N`. `evidence.unfolded` lists the labels put back. A fold with
no turn map is never put to the context (its turns mix speakers).

The owner's desk-mic voice is learned from his phone voice: once channel A has
taught the registry his call voice, voice memos score against it across
channels (median 0.52) and become owner exemplars on the room channel, after
which conversations at the desk match him directly. Process calls and memos
before conversations in any backfill.

## Context: when the voice is not enough

Step 3 leaves some speakers `uncertain` and registers others as strangers when
the recording itself says who they are: the others call her by name, the
summary says "Gershon asked", the meeting is about a project only one known
person works on. For those speakers (`voice_context.py`) the pass makes **one
Jev call per recording** (the Jev ingress, `jev-1.13.0`, key alias
`heyma-wax-voice-id`) with a `choice` per speaker over the named voices the
voice does not rule out (likelihood ratio at least 0.25, at most five) plus
`someone_else`. Jev reads the title, summary, classification, projects, who is
already identified, each unidentified speaker's lines with the turns around
them (turns near a candidate's name first), and a profile per candidate: the
`relationship` from their note, the titles, projects and kinds of recordings
they were confidently heard in. It never hears audio or sees a voice score.

The two pieces of evidence combine as

    posterior(person) ∝ P_context(person) × LR_voice(person),  posterior(nobody listed) ∝ P_context(someone_else)

where `LR_voice` is how much likelier the voice score is for the same person
than for another, fitted on 185 known-identity speakers (owner channels, caller
IDs, hand-typed turn names; each scored without its own recording): about 2 at
0.40 on the same channel, 7 at 0.50, 23 at 0.60, capped at 20. A speaker is
named (`context`) only when the posterior is at least **0.85** and the context
alone gives that person at least **0.30**: the transcript must point, the voice
must not argue. Context never registers a voice, never adds an exemplar, and
only names voices that have a name; a would-be stranger it does not name is
registered as before.

**Measured (2026-11-11, labels masked).** Jev gave the true person a median 0.29
against 0.04 for a wrong or unregistered person (AUC 0.86): cautious, but it
discriminates. At the bars above, none of 34 speakers who are nobody named got
a name, and of 70 known speakers given an uncertain voice (likelihood ratio 7)
19 were named, with one disagreement on a turn whose hand label the voice also
disputed. Most speakers stay unnamed because most transcripts do not say who is
talking; context wins where they do.

**First real run (2026-11-11, 183 recordings): 49 speakers named.** Four
reviewer agents each tried to disprove the 43 named under a generic label: 30
supported, 11 plausible, 2 wrong. Both errors were plumbing, not the bars: Jev
was asked about a label this pass had already renamed (two speakers folded into
`Jarad` before the wife's voice was known), and a mono Telnyx call was scored
as room audio. After the unfold and the telephone domain (below) and a rerun of
194 recordings: 44 context namings, both errors withdrawn, and 8 earlier
`Jarad` namings that had been asked about under that name went back to
`Speaker N`, because asked fairly the transcript did not point clearly enough.
In home recordings the owner and his wife Carrie (`voice-707163`) both score at the likelihood-ratio cap,
so there the context alone decides, and it stays cautious.

The evidence (`evidence.speakers.<label>.context`) shows each candidate's voice
score, likelihood ratio and context probability, and `someone_else`;
`evidence.context` has the Jev model and request id. A failed Jev call (no key,
ingress down) names nobody, is printed to stderr and recorded as
`evidence.context.error`, and the voice's own verdict stands.
`WAX_VOICE_CONTEXT=0` switches the step off; the bars are
`WAX_VOICE_CONTEXT_MIN_P` and `WAX_VOICE_CONTEXT_MIN_CONTEXT`. Filling in a
voice's `relationship` gives Jev more to go on.

## Calibration (2026-11-11, this vault)

Phone audio drags every voice toward one corner: raw, two different callers
scored 0.55 and a caller scored 0.74 against the owner. So each fingerprint has
its channel's average voice (`call` or `room`) subtracted before scoring
(`config/voiceprint-domains.json`, measured from 100 call and 228 room
fingerprints with at least 15 s of speech; a Telnyx recording mixed to one
track is `call` audio too). A voice's score is the mean of its
two best exemplar cosines. Each voice is scored twice, on its exemplars from the
probe's own channel against the plain bars and on its other-channel exemplars
against the `_cross` bars, and keeps whichever clears its bar by more. (Scoring
only the own channel when it had any let one odd room exemplar outvote forty
call exemplars: the owner's memos then scored 0.14 against him and the first
backfill registered his desk voice as 21 strangers.)

| Pair (centred, top-2) | Median | Spread |
|---|---|---|
| Owner against owner, calls | 0.69 | 5th percentile 0.32 |
| Same caller across calls | 0.67 | 25th percentile 0.58 |
| Different callers | 0.29 | 95th 0.56, max 0.63 |
| A caller against the owner | 0.22 | max 0.58 |
| Owner memo against owner calls (cross) | 0.52 | 0.35 to 0.60 |
| Room stranger against owner calls (cross) | 0.20 | 95th 0.42 |

| Threshold (`Thresholds`, env `WAX_VOICE_<FIELD>`) | Value |
|---|---|
| `match` / `match_cross` | 0.60 / 0.55 |
| `new` / `new_cross` | 0.40 / 0.30 |
| `margin` (in headroom over each voice's bar) | 0.08 |
| `learn` / `learn_cross` | 0.68 / 0.58 |
| `owner_solo` / `owner_solo_cross` | 0.45 / 0.40 |
| `min_match_s` / `min_new_s` / `min_consistency` | 4 / 20 / 0.5 |

**One known line, many sounds.** "Damian Miller"'s channel did not form one
cluster: across his 22 long calls the pairwise median was about 0.25, in tight
sub-clusters (0.76) by device. That is why a caller-ID'd channel learns every
sighting instead of only strong ones. Replaying all 385 calibration
fingerprints (calls, then memos, then conversations) gave one owner voice with
40 call and 36 room exemplars, one Damian voice holding all 28 of his calls, 38
new voices (about half heard once) and 72 uncertain sightings.

## Backfill (2026-11-11)

Every transcript with local audio was run once, `WAX_VOICE_NOTIFY=0`, in the
order calls (46), single-voice memos (126), conversations (253); 430 runs, no
failures. Result: 63 voices, 3 named (the owner in 279 recordings with 40 call
and 40 room exemplars, Damian Miller, Dariusz Czaplic), 60 unnamed (12 heard in
three or more recordings, 42 heard once); 145 notes had `Speaker N` turns
renamed, almost all to the owner. Sightings: 406 matched, 232 uncertain, 60
new, 52 too short, 123 owner, 28 caller ID.

- **Hand-named turns are the best naming hints.** `wax voices show <voice>`
  lists every label a voice was heard under; a note where you typed `Damian`
  or `Gershon` over a turn says who that voice is. On 2026-11-11 `Damian`
  matched `voice-825bf3` (his in-person voice; his phone voice is a separate
  cluster), `Gershon` `voice-6a12fa`, `Chaya` `voice-d38edd`, `Cait`
  `voice-91ce9f`.
- **A diarizer cluster that mixes people takes its majority voice's name.** In
  a remote meeting the diarizer split the owner into three `Speaker N` and
  folded a laptop-speaker voice into them; all three became the owner. The
  sighting keeps the original label, so `wax voices show` still tells them
  apart.
- **Order matters on a cold registry**: calls, then memos, then conversations.
- Items with no local audio (`audio_missing`) were not run; restore them first.

## The registry

| Where | What |
|---|---|
| `var/wax.db` `voices` | One row per voice: id (`voice-<6 hex>`), cached name and status, note path, model, first item and label, `merged_into` |
| `var/wax.db` `voice_exemplars` | Up to 40 fingerprints per voice and channel; one within cosine 0.92 of a kept one adds nothing |
| `var/wax.db` `voice_sightings` | Every (item, label): verdict, voice, score, runner-up, channel, seconds, fingerprint, `rendered` (the label the note shows now) and `turns` (the turn heads the diarizer label owned) |
| `~/d/Voices/<voice-id>.md` (`WAX_VOICES_DIR`) | **The truth for who a voice is.** Frontmatter `name`, `relationship`, `status` (`unnamed`, `named`, `ignore`, `merged`), where it was first heard, a few quotes |
| `~/d/Voices/<voice-id>.ogg` | Up to 15 s of the voice's most typical speech (Opus 32k), embedded in the note |

The registry is the pass's own store, the one exception to "a pass never writes
the ledger": it writes only the three `voice*` tables, under `var/voices.lock`
and one SQLite transaction, and never the `items`/`passes` rows the runner owns.
`~/d/Voices` is not a document binding, so the document sweep never enriches it.

**Name a voice** by filling `name:` in its note (Obsidian), with `wax voices
name`, or by ear in `wax voices tui` ([below](#naming-in-the-tui)). Set `status: ignore` for a TV, a hold message or a recording: it stays
matched, so it is never registered again, but is never named or linked. Naming
a voice after one that already has that name merges the two (the older keeps
its id).

**Notifications.** A run that registers unnamed voices sends one desktop
notification and one ntfy (`~/.local/bin/ntfy-alert`, topic `infra` unless
`WAX_VOICE_NTFY_URL`), never per speaker. `WAX_VOICE_NOTIFY=0` silences both;
the test suites pin it off.

## Commands

| Command | Does |
|---|---|
| `wax voices list [--json]` | Every voice, most heard first; how many are waiting for a name |
| `wax voices show <voice>` | One voice (id, id prefix or name) and every sighting |
| `wax voices name <voice> <name> [--no-sync]` | Name it (note and cache), merge namesakes, then relabel its transcripts |
| `wax voices merge <keep> <drop>` | The same person registered twice: sightings and exemplars move to `keep`; `drop`'s note is marked `merged` (nothing deleted) |
| `wax voices tui [--no-sync] [--no-play]` | Full-screen naming and merging with audio (below); the same `set_name`, `merge_namesakes` and `merge` as `name` and `merge` |
| `wax voices sync [--dry-run]` | Merge namesakes, then rerun `voice-id --live` on every transcript still showing an old label |

### Naming in the TUI

`wax voices tui` is the fast way through a pile of unnamed voices (it needs a
terminal; scripts keep using `name`, `merge`, `sync`). It is a front end: it calls
`voices.set_name` + `merge_namesakes`, `voices.merge` and the same relabel step
(`voices.relabel_state`) as the CLI, so the note, the cache and the transcripts end
up identical. Code: `components/wax/src/wax/voices_tui.py`.

- **Table.** `wax voices list`'s columns, unnamed voices first (most heard first),
  then named, `ignore`, and merged voices dimmed at the bottom. The order is fixed
  when it opens (`r` reloads); naming does not reshuffle rows under the cursor.
- **Unnamed row highlighted = listening.** Focus goes to the NAME field of that
  row and its clips loop 1, 2, ... N, 1 until you act. Moving the highlight kills
  the player at once and starts the next row's clips. A half-typed name is kept per
  row while you look at another.
- **Clips.** Up to 8 distinct ~5 s clips, cut from the item audio (`items.path`)
  at the turn heads the voice's `confident` sightings recorded (longest turns first,
  one recording at a time in rotation, centred inside the turn). A voice with no
  usable turn (channel-separated calls, audio gone) falls back to 5 s slices of its
  `<voice-id>.ogg`. Clips are cached as WAV under `var/voice-clips/` (safe to
  delete; never in the vault or `~/d/Voices`). Player: `$WAX_VOICES_PLAYER` (a
  command that takes the file path), else `pw-play`, `paplay`, `ffplay -nodisp`,
  `aplay`. No player: the status line says so and naming still works.
- **Keys, name focus** (default on an unnamed row; every printable key types, so
  `j`, `k`, `q` and Space are letters): type, Backspace, Ctrl+U clear, Ctrl+W word,
  **Enter** locks the name in (a confirmation lands on the status line, the row
  updates, the highlight moves to the next unnamed voice), Esc clears the text,
  Up/Down/PgUp/PgDn/Home/End move, Tab hands focus to the list, Ctrl+R replays,
  Ctrl+N skips to the next clip, Ctrl+C or Ctrl+Q quits.
- **Keys, list focus** (named rows, or after Tab): `j`/`k` move, Enter/`e` rename
  (named) or return to NAME (unnamed), Space/`p` start or stop playback of any
  row, `m` or Alt+M merge, `r` reload, `q` quits.
- **Merge (Alt+M, an ESC then `m`; a lone Esc is cancel).** The highlighted row
  (`-`) is folded in; a second cursor (`+`, opens on the most heard named voice)
  picks the voice kept. Up/Down/`j`/`k` move it, Space plays the candidate. Enter
  asks `fold <drop> into <keep> (<name>)? [y/n]`; `y` calls
  `voices.merge(keep, drop)`, `n` goes back to picking, Esc cancels. A voice cannot
  be folded into itself or into an already merged voice (refused on the status
  line, nothing called). Merge direction is yours: pick the named voice as the
  target, since the target's name wins.
- **Nothing blocks keys.** A name, merge or reload runs on a worker thread with its
  own ledger connection, because `voices.merge` waits on the registry lock a
  voice-id pass holds across its LLM call. While one is pending the screen keeps
  drawing (`[merging ...]`), other keys are ignored with the reason on the status
  line, and Ctrl+C still quits (the change then lands before the process exits).
  A locked ledger or vanished file in any key handler becomes an `error:` status
  line, not a crash.
- **Relabelling is in the background.** A name or merge asks one worker to rerun
  `voice-id` on the stale transcripts (requests coalesce); progress shows on the
  play line. On quit it waits for the worker and prints a summary; failures are
  rerun by `wax voices sync`. Ctrl+C while waiting stops after the transcript in
  flight (it never kills a pass mid-write) and leaves the rest for `wax voices
  sync`; a second Ctrl+C abandons the transcript in flight too. `--no-sync` skips
  relabelling, `--no-play` never plays audio.
- **Tests and evidence.** `tests/voices_tui_test.py` (model, clip planning with
  real ffmpeg, the player loop with real subprocesses); a real-terminal run is
  recorded in `components/wax/docs/evidence/heyma-19/`.

## Failures

| Code | Meaning |
|---|---|
| `audio_missing` | No local audio at `items.path` (restore it first; see the incident log's S3-only recipe) |
| `voice_id_failed` | The GPU child or the registry failed (CUDA unavailable, a bad file); stderr tail in `detail` |
| `no_speech` (skipped) | No turns, channels or timed segments to fingerprint |

The pass never calls a chat model (its only model call besides TitaNet is the Jev
decision model, for context), and no Anthropic model anywhere.
