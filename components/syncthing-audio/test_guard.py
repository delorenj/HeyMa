"""Real two-device sync regression: temporary profiles and synthetic bytes only."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET


spec = importlib.util.spec_from_file_location("guard", Path(__file__).with_name("guard.py"))
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait(check, message, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError(message)


def stop(process):
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


class GuardTest(unittest.TestCase):
    def test_custom_rules_survive_repeated_reconciliation(self):
        rules = guard.patterns({"gone [1].mp3"}, ["/private.mp3"])
        self.assertEqual(rules, guard.patterns({"gone [1].mp3"}, rules))
        self.assertIn("/gone \\[1\\].mp3", rules)
        self.assertLess(rules.index("/private.mp3"), rules.index("(?i)!/*.mp3"))

    @unittest.skipUnless(shutil.which("syncthing"), "Syncthing is required")
    def test_receive_delete_edit_revert_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="syncthing-audio-test-") as temporary:
            root = Path(temporary)
            profiles, apis, ids, processes, logs = [], [], [], [], []
            try:
                for name in ("sender", "hub"):
                    profile = root/name
                    subprocess.run(["syncthing", "generate", "--no-default-folder", "--home="+str(profile)],
                                   check=True, capture_output=True)
                    config = ET.parse(profile/"config.xml")
                    config.find("gui/address").text = "127.0.0.1:"+str(port())
                    config.find("options/listenAddress").text = "tcp://127.0.0.1:"+str(port())
                    for setting in ("globalAnnounceEnabled", "localAnnounceEnabled", "relaysEnabled", "natEnabled", "startBrowser"):
                        config.find("options/"+setting).text = "false"
                    config.find("options/autoUpgradeIntervalH").text = "0"
                    config.write(profile/"config.xml")
                    profiles.append(profile)
                    apis.append(guard.API(profile/"config.xml"))
                    log = (root/(name+".log")).open("w")
                    logs.append(log)
                    processes.append(subprocess.Popen(["syncthing", "serve", "--home="+str(profile), "--no-browser", "--no-restart"], stdout=log, stderr=log, start_new_session=True))
                for api in apis:
                    wait(lambda: api.call("system/status"), "temporary Syncthing did not start")
                    ids.append(api.call("system/status")["myID"])
                for i, api in enumerate(apis):
                    other = 1-i
                    address = ET.parse(profiles[other]/"config.xml").findtext("options/listenAddress")
                    api.call("config/devices", {"deviceID": ids[other], "name": "peer", "addresses": [address]})
                    folder = api.call("config/defaults/folder")
                    data = profiles[i]/"audio"
                    data.mkdir()
                    (data/".stfolder").mkdir()
                    folder.update(id="audio", label="audio", path=str(data), type="sendonly" if i==0 else "receiveonly",
                                  devices=[{"deviceID": ids[i]}, {"deviceID": ids[other]}], ignoreDelete=True,
                                  rescanIntervalS=1, fsWatcherDelayS=1)
                    if i==1:
                        (data/".stignore").write_text("\n".join(guard.patterns(set(), []))+"\n")
                    api.call("config/folders", folder)
                sender = profiles[0]/"audio"
                hub = profiles[1]/"audio"
                state = root/"received.json"
                # Spaces and metacharacters exercise exact exclusions, not globs.
                name = "capture [1].MP3"
                (sender/name).write_bytes(b"synthetic original recording")
                (sender/"2026").mkdir()
                (sender/"2026/nested.mp3").write_bytes(b"must not reach hub")
                apis[0].call("db/scan?folder=audio", {})
                wait(lambda: (hub/name).exists(), "root-level audio was not received")
                guard.reconcile(apis[1], hub, state, "audio")
                self.assertEqual((hub/name).read_bytes(), b"synthetic original recording")
                self.assertFalse((hub/"2026").exists(), "nested directory was received")
                (hub/name).unlink()  # synthetic test data only
                (sender/name).write_bytes(b"remote update must not resurrect hub deletion")
                apis[0].call("db/scan?folder=audio", {})
                apis[1].call("db/scan?folder=audio", {})
                apis[1].call("db/revert?folder=audio", {})
                # A new recording still arrives while the old name is blocked.
                (sender/"new.ogg").write_bytes(b"new synthetic recording")
                apis[0].call("db/scan?folder=audio", {})
                wait(lambda: (hub/"new.ogg").exists(), "new recording stopped syncing")
                guard.reconcile(apis[1], hub, state, "audio")
                self.assertFalse((hub/name).exists(), "remote edit/revert resurrected a deletion")
                # Incoming deletes must not erase a received recording.
                (sender/"new.ogg").unlink()
                apis[0].call("db/scan?folder=audio", {})
                self.assertEqual((hub/"new.ogg").read_bytes(), b"new synthetic recording")
                # State survives a hub restart and can repair removed rules.
                stop(processes[1])
                processes[1] = subprocess.Popen(["syncthing", "serve", "--home="+str(profiles[1]), "--no-browser", "--no-restart"], stdout=logs[1], stderr=logs[1], start_new_session=True)
                wait(lambda: apis[1].call("system/status"), "hub did not restart")
                wait(lambda: apis[1].call("db/status?folder=audio")["state"]=="idle", "hub did not finish restarting")
                guard.reconcile(apis[1], hub, state, "audio")
                (sender/"after-restart.ogg").write_bytes(b"restart proof")
                apis[0].call("db/scan?folder=audio", {})
                wait(lambda: (hub/"after-restart.ogg").exists(), "sync did not resume after restart")
                self.assertFalse((hub/name).exists(), "restart resurrected a deletion")
                self.assertIn(name, json.loads(state.read_text()))
            finally:
                for process in processes:
                    stop(process)
                for log in logs:
                    log.close()


if __name__ == "__main__":
    unittest.main()
