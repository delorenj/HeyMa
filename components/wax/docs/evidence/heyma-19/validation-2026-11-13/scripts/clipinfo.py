import os, sys, subprocess
S = "/tmp/heyma-validate-heyma-19/fix1"
os.environ.update(WAX_ROOT=S+"/root", WAX_VAULT=S+"/vault", WAX_VOICES_DIR=S+"/voices")
sys.path.insert(0, "/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/src")
from wax import voices, voices_tui as t
print("VAR (clip dir parent):", t.paths.VAR, " VOICES_DIR:", voices.VOICES_DIR)
conn = voices.connect()
specs = t.plan_clips(conn, "voice-a00001")
print("turn-derived specs:", [(s.what, s.start, s.dur) for s in specs])
clip = t.cut_clip(specs[0]); print("clip:", clip)
print(subprocess.run(["ffprobe","-v","error","-show_entries","stream=codec_name,sample_rate,channels:format=format_name,duration","-of","default=nw=1",str(clip)],capture_output=True,text=True).stdout)
