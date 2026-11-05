# Dictionary: `Dictionary.yaml`, `wax dict` and dictionary-correction

Speech recognition does not know the speaker's own words, so it spells them a
new way every time (Skillix, "skill X" for Skillex; "delo dot sh" for delo.sh).
The `dictionary-correction` pass respells them against **`Dictionary.yaml`** at
the repo root (`$WAX_ROOT/Dictionary.yaml`; `WAX_DICTIONARY` overrides), the
HeyMa custom dictionary: 116 terms on 2026-11-05. The file is read on **every**
run, so an edit applies to the next transcript with no compile or restart (the
compiled pipeline freezes the pass's command and env, never the file). Verified
against the file, `wax dict list`, and `components/wax/docs/ENRICHMENT-PASSES.md`
(section "dictionary-correction"). The pass's row is in
[passes/catalog](catalog.md); the general runner is in [passes/index](index.md).

## Entry forms

```yaml
terms:
  Skillex:                                  # the word only
  Plane: "the ticket-board tool"            # the word and what it is
  pjangler: [pee jangler]                   # the word and how it gets misheard
  DeLoNET: {heard: [d low net, dlonet], about: "the home lab"}
```

Case matters: write the term exactly as it should appear. Quote an `about` that
contains a comma or a colon. `heard` lists mis-hearings you have **actually
seen**; they are accepted more readily than a sound-alike the pass finds on its
own, but Jev still checks each in context. `about` is one short line Jev sees, so
it can tell "Skillex" from "skill set". A spoken "dot" in a domain or file name
is fixed without being listed. The `# -- Look-alikes` section holds real words
that keep getting respelled into a term (see below).

## The three steps

1. **Scan** (`wax.dictionary.candidates`, deterministic). Runs of one to four
   words on one line are matched in order: `heard` (a listed mis-hearing), `exact`
   (the term's letters, wrong case or spacing), `case` (a case-only change to an
   ordinary English word), `fuzzy` (a sound-alike of at least five letters with a
   spelling ratio of 0.82 or more, `WAX_DICTIONARY_MIN_SCORE`, or 0.9 when every
   word is ordinary English), `dot` (a spoken "dot" between labels ending in a TLD
   or file suffix; `WAX_DICTIONARY_SPOKEN_DOT=0` turns it off). The scan never
   edits frontmatter, the `# Transcription:` header, `**Speaker**` labels, code,
   wiki links or URLs, and never matches letters spelled aloud, a correct
   sentence-case spelling, a run that begins or ends with a function word
   (unless that form is listed), a sound-alike ending in a number, or a domain
   term whose suffix was not heard.
2. **Judge.** Jev answers every distinct (heard text, term) pair, batched 40 at a
   time, at most 160 pairs per document. The ingress is `jev-1.13.0` through
   HeyMa's own virtual key (alias `heyma-wax-dictionary`, $0.50 per day, 120 rpm,
   `op://DeLoSecrets/sazfdn52xd3a3uyatrbo43hiei/credential`). It does not use the
   AutomaticAI gateway.
3. **Apply.** A candidate is accepted when its probability reaches the bar for
   its source. The corrected body returns as a `body_replace` compare-and-replace
   intent, so an edit made while the pass ran wins.

| Source | Bar | Variable | Measured precision |
|---|---|---|---|
| `heard` | 0.4 | `WAX_DICTIONARY_MIN_P_HEARD` | 1.00 |
| `case` | 0.65 | `WAX_DICTIONARY_MIN_P_CASE` | 0.98 |
| everything else (`exact`, `fuzzy`, `dot`) | 0.5 | `WAX_DICTIONARY_MIN_P` | exact 0.97, fuzzy 0.94, dot 1.00 |

Recall was 0.92 overall. The bars come from a blind-labelled sample of 588
candidates mined from 944 vault transcripts (2026-11-04); before the edge, number,
spelled-address and nesting rules, precision was 0.92. The scratch data was in
`/tmp/dictprobe/` and is not durable.

**Placement.** The pass is `after: [diarization]`: diarization re-renders the body
from the timed ASR and skips a body it no longer recognises (`human_edits`), so
corrections must come after it. In `transcript-standard` v12 `title-slug`,
`clipboard-copy` and `project-extraction` run after this pass and read the
corrected text; that ordering lives in the pipeline, so switching the pass off
never quarantines them. Only diarization's body edit marks
`transcripts.diarized`; this pass leaves it alone.

**Results.** Nothing to correct is a completed run with no body change and no
Jev call. An empty note is `skipped`/`empty_document`. A missing or unparseable
dictionary fails with `dictionary_missing` or `dictionary_invalid`; it never runs
as if the dictionary were empty. A Jev failure fails with a `provider_*` code
([reason codes](../../subskills/doctor/references/reason-codes.md)). `evidence`
records the dictionary path, sha and term count, the candidate, asked and dropped
counts, request ids, the bars and up to 60 `corrections` rows.

## Calibration: what the false positives really were

**Jev was not the main source of false positives.** They came from scan edges and
dictionary entries, and all are now scan rules:

| Cause | Example | Where to fix |
|---|---|---|
| A run that swallowed a neighbouring function word | "Dami and" became Damian; "in Telephoria" | the scan's function-word edge rule |
| A `heard:` form that is a fragment of the term | "jangler" produced "P pjangler" and "project pjangler" | remove the form from the term's `heard:` |
| A spelled-out address | letters said aloud | the scan's spelled-address rule |

**How to apply this.** When a bad correction is reported, look first at the run's
edges and at the term's `heard:` list, and only then at the thresholds. Do not
lower or raise the Jev bars to fix what are really scan or entry problems. Do not
add common words or term-suffix fragments to `heard:`. A child's name (Dami,
Domi) sits near Damian, so **Damian must not get loose `heard` forms**. Re-measure
if the Jev model pin changes (`jev-1.13.0` is the build the bars were measured
on).

**The look-alike trick.** When a real word keeps being respelled into a term,
add that word as its **own term** in the `# -- Look-alikes` section. The exact
match then wins over the sound-alike. Verified 2026-11-04: Skrillex became
Skillex at p 0.87 until `Skrillex: "the electronic musician (not Skillex)"` was
added (commit `fc3d732`, which also dropped the `hama` form and noted that Candybar is not the snack).

The full backfill on 2026-11-04 made 3,847 replacements with about 10 false
positives, all reverted. The incident entry is in the
[incident log](../../subskills/doctor/references/incident-log.md).

## `wax dict`: grow it without hand-editing

```bash
wax dict add Kapture --about "the browser automation daemon"   # add a word
wax dict heard Kapture "cap sure"      # list a mis-hearing you actually saw
wax dict show [Kapture] [--no-scan]    # how it fares in the vault, now
wax dict fix Kapture [--dry-run] [--limit N]   # re-run the pass on notes that may misspell it
wax dict list [filter]
wax dict remove TERM [--heard FORM]
```

`add` options: `--about`, `--heard` (repeatable), `--section NAME`, `--update`,
`--force`, `--dry-run`, `--no-scan`, `--no-commit`. It writes one line in the
file's own style (a trailing "Added with wax dict" section by default), proves the
new file loads and only that entry changed, replaces it atomically under
`var/dictionary.lock`, then **commits and pushes `Dictionary.yaml` alone**
(`--no-commit` or `WAX_DICT_COMMIT=0` to skip). It then scans the vault and
reports how often the term is already right, what the pass would already offer
Jev (those need no `heard:`), and the **near misses** (fuzzy 0.72 up to the bar):
a near miss that really is the term is the only `heard:` form worth listing.

The linter refuses without `--force` a single ordinary English word and a
fragment of the term ("jangler" for pjangler). It warns on phrases of ordinary
words ("candy bar" caught a snack) and on forms that begin or end with a function
word, and errors on collisions with another term or its `heard:`. Code is
`components/wax/src/wax/dictedit.py`.

`wax dict fix` runs the pass on each note the scan found: a Wax item through
`passes.run`, any other note through the manual `dictionary-only` pipeline. To
redo one finished item: `wax ep run dictionary-correction <item>`. To preview
without writing, run `components/wax/config/passes.d/bin/dictionary-correction NOTE.md`
directly.

## Doctor and failure modes

`wax doctor` has two probes: `jev api key (dictionary-correction)` (a free key
lookup, covering a block or a spent budget) and `custom dictionary` (the file
parses, with its term and mis-hearing counts). A dictionary edit commits and
pushes on its own; if `git unpushed` lists `Dictionary.yaml`, land it. Per-request
Jev spend is in the ingress's LiteLLM spend log, not the response.
