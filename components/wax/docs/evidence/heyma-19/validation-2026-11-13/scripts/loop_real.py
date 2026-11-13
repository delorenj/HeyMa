"""Drive the TUI's real clip pipeline: plan_clips -> cut_clip -> player_command (auto fallback order) -> ClipLoop
with a REAL player binary. usage: loop_real.py <mode> where mode = which single player is on PATH, or all/none.
Audio goes only where the environment sends it (private PipeWire null sink / SDL disk file / ALSA file PCM)."""
import os, sys, time
S = "/tmp/heyma-validate-heyma-19/fix1"
os.environ.update(WAX_ROOT=S+"/root", WAX_VAULT=S+"/vault", WAX_VOICES_DIR=S+"/voices")
sys.path.insert(0, "/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/src")
from wax import voices, voices_tui as t
mode = sys.argv[1]; want = sys.argv[2] if len(sys.argv) > 2 else "cycle"
print("PATH =", os.environ["PATH"]); print("WAX_VOICES_PLAYER =", os.environ.get("WAX_VOICES_PLAYER"))
argv = t.player_command()
print("player_command() ->", argv)
p = t.make_player()
print("make_player ->", type(p).__name__, getattr(p, "argv", None), getattr(p, "reason", ""))
if not getattr(p, "available", False):
    sys.exit(0)
conn = voices.connect()
clips = t.plan_clips(conn, "voice-a00001")
if want == "one":                      # one clip, stopped the moment its player exits (so a file sink keeps it whole)
    p.play("voice-a00001", clips[:1])
    t0 = time.time()
    while p._proc is None and time.time() - t0 < 5: time.sleep(0.005)
    proc = p._proc; t1 = time.time()
    while proc.poll() is None and time.time() - t0 < 30: time.sleep(0.003)
    p.stop(); print(f"one clip: player pid={proc.pid} rc={proc.returncode} wall={time.time()-t1:.2f}s state={p.state()}")
else:
    p.play("voice-a00001", clips)
    seen = []
    t0 = time.time()
    while time.time() - t0 < float(want):
        st = p.state()
        if st and (not seen or seen[-1][0] != st["index"]): seen.append((st["index"], round(time.time()-t0, 2), st["what"], st["error"]))
        time.sleep(0.05)
    print("loop state transitions (index, t, what, error):"); [print("  ", s) for s in seen]
    print("final state:", p.state()); p.stop()
print("after stop: state =", p.state())
