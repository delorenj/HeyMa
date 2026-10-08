import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest


spec = importlib.util.spec_from_file_location("ssh_audio", Path(__file__).parents[1] / "ssh_audio.py")
audio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audio)


class FakePulse:
    def __init__(self):
        self.defaults = {"sink": "desktop", "source": "yeti"}
        self.devices = {"sinks": [{"index": 1, "name": "desktop"}, {"index": 3, "name": "tv"}],
                        "sources": [{"index": 2, "name": "yeti"}]}
        self.streams = {
            "sink-inputs": [self.stream(10, "sink", 1), self.stream(12, "sink", 3)],
            "source-outputs": [self.stream(11, "source", 2)],
        }
        self.modules = []
        self.next_index = 100
        self.unreachable = set()
        self.fail_source = False
        self.before_mutation = None

    @staticmethod
    def stream(index, kind, device):
        return {"index": index, kind: device, "properties": {"object.serial": str(index)}}

    def info(self, server=None):
        if server in self.unreachable:
            raise RuntimeError("disconnected")
        return {"default_sink_name": "mac-speaker" if server else self.defaults["sink"],
                "default_source_name": "mac-mic" if server else self.defaults["source"]}

    def listing(self, kind, server=None):
        if server:
            return [{"index": 1, "name": "mac-speaker" if kind == "sinks" else "mac-mic"}]
        if kind == "modules":
            return copy.deepcopy(self.modules)
        return copy.deepcopy(self.devices.get(kind, self.streams.get(kind, [])))

    def call(self, command, *args):
        if self.before_mutation:
            self.before_mutation(command, args)
        if command == "load-module":
            kind = args[0].removeprefix("module-tunnel-")
            if kind == "source" and self.fail_source:
                raise RuntimeError("source unavailable")
            name = next(a.split("=", 1)[1] for a in args[1:] if a.startswith(kind + "_name="))
            index = self.next_index
            self.next_index += 1
            self.devices[kind + "s"].append({"index": index, "name": name})
            self.modules.append({"index": index, "name": args[0], "argument": " ".join(args[1:])})
            return str(index)
        if command.startswith("set-default-"):
            self.defaults[command.removeprefix("set-default-")] = args[0]
        elif command.startswith("move-"):
            kind = "sink" if "sink" in command else "source"
            device = next(d["index"] for d in self.devices[kind + "s"] if d["name"] == args[1])
            stream_kind = "sink-inputs" if kind == "sink" else "source-outputs"
            next(s for s in self.streams[stream_kind] if s["index"] == args[0])[kind] = device
        elif command == "unload-module":
            module = next(m for m in self.modules if m["index"] == args[0])
            kind = module["name"].removeprefix("module-tunnel-")
            self.devices[kind + "s"] = [d for d in self.devices[kind + "s"] if d["index"] != args[0]]
            self.modules.remove(module)
        else:
            raise AssertionError(command)


def lease(port=12345, created=1):
    return {"port": port, "created": created}


@pytest.fixture
def setup(tmp_path):
    pulse = FakePulse()
    router = audio.Router(pulse, tmp_path / "state.json")
    return pulse, router


def test_idle_does_not_change_audio_or_create_recovery_state(setup):
    pulse, router = setup
    before = copy.deepcopy(pulse.__dict__)
    assert router.step([]) == {"phase": "local"}
    assert pulse.__dict__ == before
    assert not router.journal.exists()


def test_connect_moves_existing_default_streams_and_disconnect_restores(setup):
    pulse, router = setup
    original_streams = copy.deepcopy(pulse.streams)
    assert router.step([lease()])["phase"] == "remote"
    assert pulse.defaults == router.state["nodes"]
    assert pulse.streams["sink-inputs"][0]["sink"] != 1
    assert pulse.streams["source-outputs"][0]["source"] != 2
    assert pulse.streams["sink-inputs"][1]["sink"] == 3  # Explicit TV route remains pinned.
    router.step([])
    assert pulse.defaults == {"sink": "desktop", "source": "yeti"}
    assert pulse.streams == original_streams
    assert not pulse.modules
    assert not router.journal.exists()


def test_new_streams_return_to_original_defaults_even_without_an_active_poll(setup):
    pulse, router = setup
    router.step([lease()])
    sink = next(d["index"] for d in pulse.devices["sinks"] if d["name"] == pulse.defaults["sink"])
    source = next(d["index"] for d in pulse.devices["sources"] if d["name"] == pulse.defaults["source"])
    pulse.streams["sink-inputs"].append(pulse.stream(25, "sink", sink))
    pulse.streams["source-outputs"].append(pulse.stream(26, "source", source))
    router.step([])
    assert pulse.streams["sink-inputs"][-1]["sink"] == 1
    assert pulse.streams["source-outputs"][-1]["source"] == 2


def test_routes_are_saved_before_they_are_changed(setup):
    pulse, router = setup

    def checkpoint(command, args):
        saved = json.loads(router.journal.read_text())
        assert saved["defaults"] == {"sink": "desktop", "source": "yeti"}
        if command.startswith("move-"):
            assert saved["routes"]

    pulse.before_mutation = checkpoint
    router.step([lease()])
    router.step([])


def test_failed_partial_setup_unloads_its_module_and_restores_audio(setup):
    pulse, router = setup
    pulse.fail_source = True
    with pytest.raises(RuntimeError, match="source unavailable"):
        router.step([lease()])
    assert pulse.defaults == {"sink": "desktop", "source": "yeti"}
    assert not pulse.modules
    assert not router.journal.exists()


def test_recovery_after_watcher_sigkill_uses_saved_routes(setup):
    pulse, router = setup
    original = copy.deepcopy(pulse.streams)
    router.step([lease()])
    recovered = audio.Router(pulse, router.journal)
    recovered.restore()
    assert pulse.streams == original
    assert pulse.defaults == {"sink": "desktop", "source": "yeti"}
    assert not pulse.modules


def test_disconnected_tunnel_restores_without_waiting_for_lease_cleanup(setup):
    pulse, router = setup
    router.step([lease()])
    pulse.unreachable.add("tcp:127.0.0.1:12345")
    assert router.step([lease()]) == {"phase": "local"}
    assert pulse.defaults == {"sink": "desktop", "source": "yeti"}


def test_two_logins_keep_audio_remote_until_last_disconnect(setup):
    pulse, router = setup
    router.step([lease()])
    router.step([lease(), lease(23456, 2)])
    assert router.state["port"] == 23456
    router.step([lease()])
    assert router.state["port"] == 12345
    assert router.state["defaults"] == {"sink": "desktop", "source": "yeti"}
    router.step([])
    assert pulse.defaults == {"sink": "desktop", "source": "yeti"}


def test_unhealthy_newest_login_does_not_displace_healthy_login(setup):
    pulse, router = setup
    pulse.unreachable.add("tcp:127.0.0.1:23456")
    router.step([lease(), lease(23456, 2)])
    assert router.state["port"] == 12345


def test_user_route_change_is_preserved_on_disconnect(setup):
    pulse, router = setup
    router.step([lease()])
    pulse.streams["sink-inputs"][0]["sink"] = 3
    router.step([])
    assert pulse.streams["sink-inputs"][0]["sink"] == 3


def test_recovery_does_not_unload_an_unrelated_module_with_reused_id(setup):
    pulse, router = setup
    router.step([lease()])
    pulse.modules[0]["argument"] = "sink_name=someone_elses_sink"
    unrelated = copy.deepcopy(pulse.modules[0])
    router.restore()
    assert pulse.modules == [unrelated]


def test_missing_original_device_uses_remaining_local_device(setup):
    pulse, router = setup
    router.step([lease()])
    pulse.devices["sinks"] = [d for d in pulse.devices["sinks"] if d["name"] != "desktop"]
    router.restore()
    assert pulse.defaults["sink"] == "tv"


def test_dead_or_reused_session_pid_cannot_keep_audio_diverted(tmp_path, monkeypatch):
    path = tmp_path / "lease-example.json"
    audio.write_json(path, {"pid": 42, "stamp": "old", "port": 12345, "created": 1})
    monkeypatch.setattr(audio, "process_stamp", lambda pid: "new")
    assert audio.leases_at(tmp_path) == []
    assert not path.exists()


def test_restore_failure_keeps_journal_for_retry(setup, monkeypatch):
    pulse, router = setup
    router.step([lease()])
    original = pulse.call

    def fail(command, *args):
        if command == "set-default-sink":
            raise RuntimeError("temporarily unavailable")
        return original(command, *args)

    monkeypatch.setattr(pulse, "call", fail)
    with pytest.raises(RuntimeError):
        router.restore()
    assert router.journal.exists()
    monkeypatch.setattr(pulse, "call", original)
    router.restore()
    assert not router.journal.exists()
