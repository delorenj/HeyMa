#!/usr/bin/python3
"""Session-owned audio routing; Zellij and idle SSH masters own no audio leases."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import uuid


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        temporary = Path(f.name)
        try:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def read_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def runtime_dir():
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "ssh-audio"


def state_file():
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "ssh-audio/state.json"


class Pulse:
    def __init__(self):
        self.local = f"unix:/run/user/{os.getuid()}/pulse/native"

    def call(self, *args, server=None, json_output=False):
        command = ["/usr/bin/pactl", "--server", server or self.local]
        if json_output:
            command += ["--format=json"]
        result = subprocess.run(command + list(map(str, args)), capture_output=True,
                                text=True, timeout=3)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "pactl failed")
        return json.loads(result.stdout) if json_output else result.stdout.strip()

    def info(self, server=None):
        return self.call("info", server=server, json_output=True)

    def listing(self, kind, server=None):
        return self.call("list", kind, server=server, json_output=True)


def identity(stream):
    props = stream.get("properties", {})
    return str(props.get("object.serial", json.dumps([
        stream["index"], stream.get("client"), props.get("application.process.id"),
        props.get("application.name")], sort_keys=True)))


class Router:
    def __init__(self, pulse, journal):
        self.pulse = pulse
        self.journal = journal
        self.state = read_json(journal)

    def save(self):
        write_json(self.journal, self.state)

    def capture_streams(self):
        """Save each original route before moving it, including newly created streams."""
        moves = []
        changed = False
        for kind, stream_kind in (("sink", "sink-inputs"), ("source", "source-outputs")):
            names = {d["index"]: d["name"] for d in self.pulse.listing(kind + "s")}
            original = self.state["defaults"][kind]
            remote = self.state["nodes"][kind]
            for stream in self.pulse.listing(stream_kind):
                current = names.get(stream[kind])
                if current not in (original, remote):
                    continue  # An explicitly selected different device stays selected.
                key = kind + ":" + identity(stream)
                if key not in self.state["routes"]:
                    self.state["routes"][key] = original
                    changed = True
                if current == original:
                    moves.append((kind, stream["index"], remote))
        if changed:
            self.save()
        for kind, index, destination in moves:
            # A short sound may disappear between listing and moving.
            try:
                self.pulse.call("move-" + kind + ("-input" if kind == "sink" else "-output"),
                                index, destination)
            except RuntimeError:
                if any(s["index"] == index for s in self.pulse.listing(
                        "sink-inputs" if kind == "sink" else "source-outputs")):
                    raise

    def activate(self, port, devices=None):
        server = f"tcp:127.0.0.1:{port}"
        info = self.pulse.info(server)
        for kind, name in (devices or {}).items():
            info["default_" + kind + "_name"] = name
        overrides = devices or {}
        for kind in ("sink", "source"):
            devices = self.pulse.listing(kind + "s", server)
            name = info["default_" + kind + "_name"]
            selected = next((d for d in devices if d["name"] == name), None)
            if kind == "source" and (selected is None or name.endswith(".monitor")
                                     or selected.get("monitor_of_sink") not in (None, 4294967295, "4294967295")):
                microphones = [d for d in devices if not d["name"].endswith(".monitor")
                               and d.get("monitor_of_sink") in (None, 4294967295, "4294967295")]
                if "source" in overrides:
                    raise RuntimeError("Mac selected input is not a microphone")
                if len(microphones) != 1:
                    raise RuntimeError("Mac microphone is ambiguous or unavailable")
                name = microphones[0]["name"]
                info["default_source_name"] = name
                selected = microphones[0]
            if selected is None:
                raise RuntimeError(f"Mac has no default {kind}")
        local = self.pulse.info()
        token = uuid.uuid4().hex[:12]
        self.state = {
            "port": port,
            "defaults": {kind: local["default_" + kind + "_name"] for kind in ("sink", "source")},
            "nodes": {kind: f"ssh_audio_{token}_{kind}" for kind in ("sink", "source")},
            "routes": {},
        }
        self.save()  # Recovery information exists before the first audio mutation.
        try:
            for kind in ("sink", "source"):
                self.pulse.call("load-module", "module-tunnel-" + kind, "server=" + server,
                                kind + "=" + info["default_" + kind + "_name"],
                                kind + "_name=" + self.state["nodes"][kind], "latency_msec=100")
            deadline = time.monotonic() + 5
            while not all(any(d["name"] == self.state["nodes"][k]
                             for d in self.pulse.listing(k + "s")) for k in ("sink", "source")):
                if time.monotonic() > deadline:
                    raise RuntimeError("Mac audio devices did not become available")
                time.sleep(0.1)
            self.capture_streams()
            for kind in ("sink", "source"):
                self.pulse.call("set-default-" + kind, self.state["nodes"][kind])
        except Exception:
            self.restore()
            raise

    def restore(self):
        if self.state is None:
            return
        destinations = {}
        for kind in ("sink", "source"):
            available = [d["name"] for d in self.pulse.listing(kind + "s")
                         if d["name"] not in self.state["nodes"].values()
                         and (kind != "source" or not d["name"].endswith(".monitor"))]
            original = self.state["defaults"][kind]
            if original in available:
                destinations[kind] = original
            elif available:
                destinations[kind] = available[0]
            else:
                raise RuntimeError(f"No local {kind} available to restore")
            self.pulse.call("set-default-" + kind, destinations[kind])
        for kind, stream_kind in (("sink", "sink-inputs"), ("source", "source-outputs")):
            names = {d["index"]: d["name"] for d in self.pulse.listing(kind + "s")}
            for stream in self.pulse.listing(stream_kind):
                if names.get(stream[kind]) != self.state["nodes"][kind]:
                    continue  # Respect a route changed manually during the connection.
                destination = self.state["routes"].get(kind + ":" + identity(stream), destinations[kind])
                if destination not in names.values():
                    destination = destinations[kind]
                try:
                    self.pulse.call("move-" + kind + ("-input" if kind == "sink" else "-output"),
                                    stream["index"], destination)
                except RuntimeError:
                    if any(s["index"] == stream["index"] for s in self.pulse.listing(stream_kind)):
                        raise
        # Match owned names, not module IDs: IDs may be reused after a server restart.
        for module in self.pulse.listing("modules"):
            if module["name"] not in ("module-tunnel-sink", "module-tunnel-source"):
                continue
            args = shlex.split(module.get("argument", ""))
            if any(f"{k}_name={n}" in args for k, n in self.state["nodes"].items()):
                self.pulse.call("unload-module", module["index"])
        self.journal.unlink(missing_ok=True)
        self.state = None

    def step(self, leases):
        # Prefer the latest healthy session; another session survives its disconnect.
        selected = None
        for lease in sorted(leases, key=lambda x: x["created"], reverse=True):
            try:
                self.pulse.info(f"tcp:127.0.0.1:{lease['port']}")
                selected = lease
                break
            except (RuntimeError, subprocess.TimeoutExpired):
                continue
        if not selected:
            self.restore()
            return {"phase": "local"}
        if self.state and self.state["port"] != selected["port"]:
            self.restore()
        if self.state is None:
            self.activate(selected["port"], selected.get("devices"))
        else:
            self.capture_streams()
        return {"phase": "remote", "port": selected["port"], "devices": self.state["nodes"]}


def process_stamp(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]  # Field 22 (start time), after pid and comm.
    except (FileNotFoundError, ProcessLookupError):
        return None


def leases_at(directory):
    leases = []
    for path in directory.glob("lease-*.json"):
        lease = read_json(path)
        if process_stamp(lease["pid"]) == lease["stamp"]:
            leases.append(lease)
        else:
            path.unlink(missing_ok=True)
    return leases


def watch():
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "watch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        router = Router(Pulse(), state_file())
        router.restore()  # Recover a previous killed watcher before accepting leases.
        running = True

        def stop(_signal, _frame):
            nonlocal running
            running = False

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, stop)
        previous = None
        try:
            while running:
                try:
                    status = router.step(leases_at(directory))
                except Exception as error:
                    try:
                        router.restore()
                    except Exception as recovery_error:
                        error = RuntimeError(f"{error}; restoration pending: {recovery_error}")
                    status = {"phase": "error", "reason": str(error)}
                write_json(directory / "status.json", status)
                if status != previous:
                    print(json.dumps(status), flush=True)
                    previous = status
                time.sleep(0.5)
        finally:
            router.restore()
            write_json(directory / "status.json", {"phase": "local"})


def session(port, command, devices=None):
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lease = directory / f"lease-{uuid.uuid4().hex}.json"
    write_json(lease, {"pid": os.getpid(), "stamp": process_stamp(os.getpid()),
                       "port": port, "created": time.monotonic_ns(), "devices": devices or {}})
    child = None

    def stop(signum, _frame):
        if child and child.poll() is None:
            child.send_signal(signum)
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, stop)
    # Ctrl+C belongs to the interactive shell and its foreground job.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = read_json(directory / "status.json") or {}
            if status.get("phase") == "remote" and status.get("port") == port:
                break
            if status.get("phase") == "error":
                print("Mac audio unavailable: " + status["reason"], file=sys.stderr)
                break
            time.sleep(0.1)
        else:
            print("Mac audio did not become ready; using local audio.", file=sys.stderr)
        child = subprocess.Popen(command or [os.environ.get("SHELL", "/bin/bash"), "-l"])
        return child.wait()
    finally:
        lease.unlink(missing_ok=True)


def install():
    launcher = Path(__file__).resolve().parents[2] / "bin/ssh-audio"
    target = Path.home() / ".local/bin/ssh-audio"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and target.resolve() != launcher:
        raise RuntimeError(f"Refusing to replace existing {target}")
    if not target.exists():
        target.symlink_to(launcher)
    unit = Path.home() / ".config/systemd/user/ssh-audio.service"
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(
        "[Unit]\nDescription=Restore and route audio for live SSH sessions\n"
        "After=pipewire-pulse.service\nWants=pipewire-pulse.service\n\n[Service]\n"
        f'ExecStart=/usr/bin/python3 "{Path(__file__).resolve()}" watch\n'
        f'ExecStopPost=/usr/bin/python3 "{Path(__file__).resolve()}" restore\n'
        "Restart=on-failure\nRestartSec=1\nTimeoutStopSec=20\nNoNewPrivileges=yes\n"
        "UMask=0077\n\n[Install]\nWantedBy=default.target\n")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "ssh-audio.service"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("watch", "restore", "status", "install"):
        commands.add_parser(name)
    shell = commands.add_parser("session")
    shell.add_argument("--port", type=int, required=True)
    shell.add_argument("--source")
    shell.add_argument("--sink")
    shell.add_argument("--command", nargs=argparse.REMAINDER, default=[])
    args = parser.parse_args()
    if args.action == "watch":
        watch()
    elif args.action == "restore":
        Router(Pulse(), state_file()).restore()
    elif args.action == "status":
        print(json.dumps({"routing": read_json(runtime_dir() / "status.json") or {"phase": "local"},
                          "audio": Pulse().info()}, indent=2))
    elif args.action == "install":
        install()
    else:
        if not 1 <= args.port <= 65535:
            parser.error("port must be between 1 and 65535")
        devices = {k: getattr(args, k) for k in ("sink", "source") if getattr(args, k)}
        return session(args.port, args.command, devices)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
