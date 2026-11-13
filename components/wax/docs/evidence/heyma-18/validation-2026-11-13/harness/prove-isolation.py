"""Run under `iso`: print every resolved path/endpoint and fail unless each is isolated."""
import os, shutil, sys, json
from pathlib import Path
sys.path.insert(0, os.path.join(os.environ["WAX_COMPONENT_SRC"]))
from wax import paths, natsclient, voices, archive, finalize, transcribe_adapter, tray_process
from wax.transcriptions import config as txc
V = os.environ["HEYMA_VALIDATE"]
rows = {
  "WAX_ROOT (paths.HEYMA)": paths.HEYMA, "ledger (paths.DB)": paths.DB, "state.json": paths.STATE_JSON,
  "status socket (paths.SOCK)": paths.SOCK, "singleton lock": paths.VAR / "waxd.singleton.lock",
  "CLI capture lock (paths.LOCK)": paths.LOCK, "inbox": paths.INBOX, "stream": paths.STREAM,
  "archive": paths.ARCHIVE, "skipped": paths.SKIPPED, "recovered": paths.RECOVERED,
  "vault (paths.VAULT)": paths.VAULT, "voices dir": voices.VOICES_DIR, "HOME": Path.home(),
  "WAX_TRANSCRIBE": transcribe_adapter.transcribe_command(), "WAX_PASSES_DIR": os.environ["WAX_PASSES_DIR"],
  "mc config dir": os.environ["MC_CONFIG_DIR"],
}
bad = []
for k, v in rows.items():
    ok = str(v).startswith(V + "/") or str(v) == V
    print(f"{'OK ' if ok else 'BAD'} {k:32} {v}")
    if not ok: bad.append(k)
print(f"{'OK ' if natsclient.HOST == '127.0.0.1' and natsclient.PORT == 1 else 'BAD'} NATS                             {natsclient.HOST}:{natsclient.PORT}  (port 1 = nothing listens)")
print(f"{'OK ' if txc.dsn() is None else 'BAD'} transcripts DSN                    {txc.dsn()!r}  (unset = capture-only, Postgres never contacted; unit file {txc.unit_path()})")
print(f"{'OK ' if os.environ['WAX_S3_ENDPOINT'].startswith('http://127.0.0.1:19123') else 'BAD'} S3 endpoint (WAX_S3_ENDPOINT)      {os.environ['WAX_S3_ENDPOINT']}  (throwaway MinIO container, tmpfs)")
print(f"{'OK ' if os.environ.get('DISPLAY') is None and os.environ.get('WAYLAND_DISPLAY') is None and os.environ.get('XDG_RUNTIME_DIR') is None else 'BAD'} display/runtime dir               DISPLAY={os.environ.get('DISPLAY')} WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY')} XDG_RUNTIME_DIR={os.environ.get('XDG_RUNTIME_DIR')}")
for tool in ("canberra-gtk-play", "notify-send", "pactl", "systemd-run", "ffmpeg", "systemctl", "op", "xdg-open", "paplay", "mc"):
    found = shutil.which(tool)
    stub = found is not None and "/harness/" in os.path.realpath(found)
    ok = found is None or stub
    print(f"{'OK ' if ok else 'BAD'} PATH lookup {tool:18} -> {found} {'(logging STUB)' if stub else '(absent)'}")
    if not ok: bad.append(tool)
print(f"{'OK ' if archive.MC == '/usr/local/bin/mc' else 'BAD'} archive.MC                       {archive.MC} (real mc binary; alias 'delo' resolved from MC_CONFIG_DIR above, not from the real ~/.mc)")
print("env var names present:", sorted(os.environ))
sys.exit(1 if bad else 0)
