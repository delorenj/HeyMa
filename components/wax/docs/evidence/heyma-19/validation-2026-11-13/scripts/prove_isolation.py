import sys
sys.path.insert(0, "/home/delorenj/HeyMa/.worktrees/hm19-tui-validate/components/wax/src")
from wax import paths, voices, natsclient
from wax.transcriptions import config as tc
print("WAX_ROOT/HEYMA :", paths.HEYMA)
print("ledger DB      :", paths.DB)
print("waxd socket    :", paths.SOCK, "| singleton lock:", paths.LOCK, "| state.json:", paths.STATE_JSON)
print("vault          :", paths.VAULT)
print("voices dir     :", voices.VOICES_DIR)
print("transcripts store enabled:", tc.enabled(), "| DSN:", tc.dsn(), "| unit file read:", tc.unit_path(), "(exists:", tc.unit_path().exists(), ")")
print("NATS target    :", natsclient.HOST, natsclient.PORT, "(port 9 = discard, nothing listens)")
import os
print("notify         : WAX_VOICE_NOTIFY =", os.environ.get("WAX_VOICE_NOTIFY"))
print("S3             : no S3 code runs in voice-id / relabel / TUI (no endpoint configured)")
