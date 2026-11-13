HEYMA-19 evidence: `wax voices tui` run in a real terminal.

What this is
  The real curses UI (bin/wax voices tui --no-sync) driven key by key inside a private tmux server
  (tmux -L heyma19, 100x22, then resized). Screens are `tmux capture-pane -p` text, so they are what
  a terminal showed, not the model's own layout. Alt+M is sent as tmux `M-m`, i.e. ESC then m on the
  wire; a lone Esc is sent as `Escape`.

Isolation
  Everything is under /tmp/heyma-19-scratch (make_fixture.py): WAX_ROOT, WAX_VAULT, TRANSCRIPTS,
  WAX_VOICES_DIR, generated tone audio, and a fake player (WAX_VOICES_PLAYER) that logs the clip file
  it is handed and sleeps 3.737 s. The tmux child runs under `env -i`, so no production path or
  socket is inherited. No waxd, no production ledger, vault, voices dir or S3.

Reproduce
  /usr/bin/python3 make_fixture.py /tmp/heyma-19-scratch
  ./smoke.sh            # prints everything in smoke-output.txt

Reading smoke-output.txt
  1  open: the first unnamed row is highlighted, its NAME field is focused, "[clip 1/3 ...]" shows the
     loop; player.log lists clip WAVs handed to the player, ~3.7 s apart, 1 -> 2 -> 3 -> 1.
  2  Down: the previous player is killed (one player process alive, not two) and voice-b00002 starts;
     it has no turn heads, so its clips are slices of its listening .ogg ("listening clip 0s").
  3  typing: j/k/q-style letters go into the field.   4  Enter: note updated, row named, cursor moved on.
  5-9  Alt+M selector ('-' original, '+' target), Esc cancel, confirmation prompt, 'n' back.
  10 target moved onto the original itself: refused on the status line.
  11 Alt+M, Enter, y: merged (row dimmed "merged", target gains the items and minutes).
  12-13 resize to 40x9 and 14x4: columns drop, no crash.
  14 Ctrl+C: exit code 0, zero player processes left, terminal restored.
  Relabelling is off (--no-sync) in this run because the voice-id pass needs the GPU and real audio;
  the background Relabeler is covered by tests/voices_tui_test.py (RelabelerTest) with a fake pass.

Fix stage (review follow-up)
  smoke-output.txt was re-captured after registry changes moved to a worker thread: the screens and the
  clip loop are unchanged. smoke-locked.sh / smoke-locked-output.txt hold the new case: another process
  holds the registry flock (as a voice-id pass does across its LLM call) while a merge is confirmed.
  The screen keeps drawing ("[merging ...]"), further keys are ignored with the reason on the status
  line, Ctrl+C closes the screen, the process waits for the merge ("letting it land"), and once the
  lock is released the merge lands and the exit code is 0.

Validation run 2026-11-13 (real players, real terminals, live relabel, real-registry dry run): validation-2026-11-13/README.md
