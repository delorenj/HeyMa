"""Raw split-escape sequences through a real pty into `wax voices tui`. Each case: fresh isolated fixture, send bytes
with a gap between them, then type `Zed` + Enter and read what name voice-a00001 got (frontmatter on disk).
Outcome meaning:  "Zed" = the escape was swallowed or consumed as a key, nothing leaked into the name field;
"<junk>Zed" = a tail of the sequence was typed into the name."""
import os, pty, sys, time, subprocess, select, fcntl, termios, struct, shutil, re, signal
D = "/tmp/heyma-validate-heyma-19"
WAX = "/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/bin/wax"
FIX = "/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/docs/evidence/heyma-19/make_fixture.py"
def fixture(path):
    shutil.rmtree(path, ignore_errors=True)
    subprocess.run(["env","-i","HOME=/home/delorenj","PATH=/usr/bin:/bin","/usr/bin/python3","-I",FIX,path],check=True,capture_output=True)
def name_of(path):
    t = open(f"{path}/voices/voice-a00001.md").read()
    m = re.search(r"^name: ?(.*)$", t, re.M); return m.group(1) if m else None
def run(case, seq, gap, tag):
    S = f"{D}/pty/{tag}"; fixture(S)
    pid, fd = pty.fork()
    if pid == 0:
        os.execve("/usr/bin/env", ["env","-i","HOME=/home/delorenj","PATH=/usr/bin:/bin","TERM=xterm-256color",
            f"WAX_ROOT={S}/root",f"WAX_VAULT={S}/vault",f"TRANSCRIPTS={S}/vault",f"WAX_VOICES_DIR={S}/voices","WAX_VOICE_NOTIFY=0",
            "/usr/bin/python3",WAX,"voices","tui","--no-play","--no-sync"], {})
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 110, 0, 0))
    def drain(t):
        end = time.time() + t
        while time.time() < end:
            r,_,_ = select.select([fd],[],[],0.05)
            if r:
                try: os.read(fd, 65536)
                except OSError: return
    drain(1.5)
    for chunk in seq:
        os.write(fd, chunk); drain(gap)
    drain(0.4)
    os.write(fd, b"Zed\r"); drain(1.5)
    os.write(fd, b"\x03"); drain(1.5)
    try: _, st = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError: st = 0
    got = name_of(S)
    if "merge" in case:
        t = open(f"{S}/voices/voice-a00001.md").read()
        got = "/".join(re.findall(r"^(?:status|merged-into|name): ?(.*)$", t, re.M))
    print(f"{case:<34} gap={gap*1000:>4.0f}ms  name={got!r}")
    try: os.kill(pid, signal.SIGKILL)
    except ProcessLookupError: pass
if os.environ.get("MERGECASE"):
    cases_ = [("Alt+M split, Enter, y => merged?", [b"\x1b", b"m", b"\r", b"y"])]
else:
    cases_ = None
cases = [
 ("CSI Down split ESC | [ | B",   [b"\x1b", b"[", b"B"]),
 ("SS3 Down split ESC | O | B",   [b"\x1b", b"O", b"B"]),
 ("CSI PgDn split ESC [ 6 ~",     [b"\x1b", b"[", b"6", b"~"]),
 ("Alt+M split ESC | m, then Esc", [b"\x1b", b"m", b"\x1b"]),
 ("lone ESC then Zed",            [b"\x1b"]),
]
cases = cases_ or cases
for gap in [float(g) for g in sys.argv[1:]] or [0.02]:
    for i,(case, seq) in enumerate(cases):
        run(case, seq, gap, f"g{int(gap*1000)}-{i}")
