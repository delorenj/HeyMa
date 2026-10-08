#!/usr/bin/python3
"""Ordinary interactive SSH, with microphone/speaker routing scoped to the login."""

import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time


def run(command, **kwargs):
    return subprocess.run(command, check=True, timeout=10, **kwargs)


def prepare_audio(env):
    # pactl starts the native server on demand. On macOS, --check/--start can
    # report failure while an autospawned server is already serving clients.
    info = json.loads(run(["pactl", "--format=json", "info"], env=env,
                          capture_output=True, text=True).stdout)
    if not info.get("default_source_name") or info["default_source_name"].endswith(".monitor"):
        raise RuntimeError("Mac microphone is unavailable; check microphone permission")
    system = json.loads(run(["/usr/sbin/system_profiler", "SPAudioDataType", "-json"],
                            capture_output=True, text=True, env=env).stdout)
    devices = select_devices(system, {
        kind: json.loads(run(["pactl", "--format=json", "list", kind + "s"],
                             capture_output=True, text=True, env=env).stdout)
        for kind in ("sink", "source")})
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    module = run(["pactl", "load-module", "module-native-protocol-tcp", "listen=127.0.0.1",
                  f"port={port}", "auth-ip-acl=127.0.0.1"], env=env,
                 capture_output=True, text=True).stdout.strip()
    return port, module, devices


def select_devices(system, pulse_devices):
    """Use CoreAudio defaults without changing either macOS or PulseAudio defaults."""
    descriptions = [device for group in system["SPAudioDataType"] for device in group.get("_items", [])]
    selected = {}
    for kind, direction in (("source", "input"), ("sink", "output")):
        default = next((d["_name"] for d in descriptions
                        if d.get(f"coreaudio_default_audio_{direction}_device") == "spaudio_yes"), None)
        candidates = [d for d in pulse_devices[kind] if d.get("description") == default
                      and not d["name"].endswith(".monitor")]
        if len(candidates) != 1:
            raise RuntimeError(f"Could not match macOS's selected {direction} device: {default}")
        selected[kind] = candidates[0]["name"]
    return selected


def connect(host, check=False, command=None):
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    for key in ("PULSE_SERVER", "PULSE_SOURCE", "PULSE_SINK"):
        env.pop(key, None)
    master = None
    module = None
    shell = None

    def stop(signum, _frame):
        if shell and shell.poll() is None:
            shell.send_signal(signum)
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    with tempfile.TemporaryDirectory(prefix="chungus-audio-", dir="/tmp") as directory:
        control = str(Path(directory) / "ssh")
        common = ["/usr/bin/ssh", "-S", control, "-o", "RemoteCommand=none"]
        try:
            port, module, devices = prepare_audio(env)
            master = subprocess.Popen(common + ["-MN", "-o", "ControlMaster=yes",
                "-o", "ControlPersist=no", "-o", "RequestTTY=no", "-o", "ServerAliveInterval=5",
                "-o", "ServerAliveCountMax=2", host], stdin=subprocess.DEVNULL, env=env)
            deadline = time.monotonic() + 60
            while not Path(control).exists():
                if master.poll() is not None:
                    raise RuntimeError("SSH connection failed")
                if time.monotonic() > deadline:
                    raise RuntimeError("SSH authentication timed out")
                time.sleep(0.1)
            allocated = run(common + ["-O", "forward", "-R", f"127.0.0.1:0:127.0.0.1:{port}", host],
                            capture_output=True, text=True, env=env).stdout.strip()
            remote_port = int(allocated)
            session_command = ["~/.local/bin/ssh-audio", "session", "--port", str(remote_port),
                               "--source", devices["source"], "--sink", devices["sink"]]
            if check:
                session_command += ["--command", "/home/delorenj/.local/bin/ssh-audio", "status"]
            elif command:
                session_command += ["--command"] + command
            # Preserve tilde expansion for the server launcher; quote all following arguments.
            remote_command = session_command[0] + " " + shlex.join(session_command[1:])
            shell = subprocess.Popen(common + ["-o", "ControlMaster=no", "-T" if check or command else "-tt",
                                               host, remote_command], env=env)
            return shell.wait()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            print(f"Automatic Mac audio failed: {error}", file=sys.stderr)
            return 1
        finally:
            # Closing the connection also removes the reverse listener after a failed setup.
            if master:
                if Path(control).exists():
                    subprocess.run(common + ["-O", "exit", host], env=env,
                                   capture_output=True, timeout=5)
                if master.poll() is None:
                    master.terminate()
                    try:
                        master.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        master.kill()
                        master.wait()
            if module:
                subprocess.run(["pactl", "unload-module", module], env=env,
                               capture_output=True, timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", nargs="?", default="big-chungus")
    parser.add_argument("--check", action="store_true", help="Check the audio connection and disconnect")
    parser.add_argument("--command", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args()
    return connect(args.host, args.check, args.command)


if __name__ == "__main__":
    raise SystemExit(main())
