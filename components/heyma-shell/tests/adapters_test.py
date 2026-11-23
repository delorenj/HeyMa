import json
import unittest

from heyma_shell import adapters
from heyma_shell.adapters import Run

from fakes import NOW, FakeWorld, ok, wax_snapshot

WAX = "/repo/bin/wax"
TELEPHONY = "/repo/bin/telephony"
VINYL_STATE = "/home/u/.local/state/vinyl/state.json"
MIRROR = "/home/u/HeyMa/var/state.json"


class SectionShapeTest(unittest.TestCase):
    def test_every_adapter_returns_the_contract_keys_and_a_valid_health(self):
        world = FakeWorld()
        for product, adapter in adapters.ADAPTERS.items():
            row = adapter(world.context())
            with self.subTest(product=product):
                self.assertEqual(row["id"], product)
                for key in ("installed", "running", "health", "summary", "actions"):
                    self.assertIn(key, row)
                self.assertIn(row["health"], adapters.HEALTH)


class WaxAdapterTest(unittest.TestCase):
    def world(self, snap, mirror=None, **kw):
        files = {MIRROR: wax_snapshot() if mirror is None else mirror}
        return FakeWorld(present=[WAX], cli={(WAX, "--json", "status"): ok(snap)}, files=files, **kw)

    def test_not_installed_when_the_repo_cli_is_missing(self):
        row = adapters.wax_adapter(FakeWorld().context())
        self.assertFalse(row["installed"])
        self.assertEqual(row["health"], "off")

    def test_calls_the_absolute_repo_cli_with_a_timeout(self):
        world = self.world(wax_snapshot())
        adapters.wax_adapter(world.context())
        argv, timeout = world.calls[0]
        self.assertEqual(argv, (WAX, "--json", "status"))
        self.assertGreater(timeout, 0)

    def test_healthy_idle_daemon(self):
        row = adapters.wax_adapter(self.world(wax_snapshot()).context())
        self.assertEqual((row["installed"], row["running"], row["health"]), (True, True, "ok"))

    def test_recording_is_ok_and_flagged(self):
        row = adapters.wax_adapter(self.world(wax_snapshot(stream="recording")).context())
        self.assertEqual(row["health"], "ok")
        self.assertTrue(row["detail"]["recording"])
        self.assertIn("recording", row["summary"])

    def test_failed_items_degrade_to_warn(self):
        snap = wax_snapshot(queue={"failed": 2})
        row = adapters.wax_adapter(self.world(snap).context())
        self.assertEqual(row["health"], "warn")
        self.assertIn("failed items: 2", row["summary"])

    def test_status_exit_2_is_a_verdict_not_a_failure_to_answer(self):
        """`wax status` exits 2 for anything non-nominal, JSON intact (live 2026-11-18)."""
        snap = wax_snapshot(queue={"failed": 4}, inbox="error")
        world = FakeWorld(present=[WAX], files={MIRROR: wax_snapshot()},
                          cli={(WAX, "--json", "status"): Run(2, json.dumps(snap), "")})
        row = adapters.wax_adapter(world.context())
        self.assertEqual((row["running"], row["health"]), (True, "warn"))
        self.assertEqual(row["detail"]["inbox"], "error")
        self.assertNotIn("cli_error", row["detail"])

    def test_dead_daemon_is_fail_even_though_the_cli_answers_fresh(self):
        """The CLI stamps updated_at with its own clock, so it looks alive with no
        waxd at all; only the mirror waxd rewrites every second can say (review 2026-11-18)."""
        for mirror, why in ((None, "no readable"), (wax_snapshot(age=600), "600s old")):
            with self.subTest(why=why):
                world = FakeWorld(present=[WAX], files={MIRROR: mirror} if mirror else {},
                                  cli={(WAX, "--json", "status"): ok(wax_snapshot(age=0))})
                row = adapters.wax_adapter(world.context())
                self.assertEqual((row["running"], row["health"]), (False, "fail"))
                self.assertIn(why, row["summary"])
                self.assertIn(why, row["detail"]["error"])

    def test_dead_daemon_does_not_spend_a_cli_call(self):
        world = FakeWorld(present=[WAX], cli={(WAX, "--json", "status"): ok(wax_snapshot())})
        adapters.wax_adapter(world.context())
        self.assertEqual(world.calls, [])

    def test_cli_failure_falls_back_to_the_live_mirror(self):
        world = FakeWorld(present=[WAX], cli={(WAX, "--json", "status"): Run(1, "", "boom\nmore")},
                          files={MIRROR: wax_snapshot()})
        row = adapters.wax_adapter(world.context())
        self.assertTrue(row["running"])
        self.assertEqual(row["detail"]["cli_error"], "boom")

    def test_error_line_prefers_stderr_over_a_json_body(self):
        self.assertEqual(adapters._first_error(Run(1, "{\n  \"a\": 1\n}", "real reason\n")), "real reason")
        self.assertEqual(adapters._first_error(Run(3, "", "")), "exit 3")

    def test_wax_root_env_moves_the_mirror(self):
        world = FakeWorld(present=[WAX], environ={"WAX_ROOT": "/data/wax"},
                          cli={(WAX, "--json", "status"): Run(1, "", "x")},
                          files={"/data/wax/var/state.json": wax_snapshot()})
        self.assertTrue(adapters.wax_adapter(world.context())["running"])


class VinylAdapterTest(unittest.TestCase):
    def test_resolution_order_env_then_usr_bin_then_local_bin(self):
        both = FakeWorld(present=["/usr/bin/vinyl", "/home/u/.local/bin/vinyl"])
        self.assertEqual(adapters.vinyl_command(both.context()), ["/usr/bin/vinyl"])
        local = FakeWorld(present=["/home/u/.local/bin/vinyl"])
        self.assertEqual(adapters.vinyl_command(local.context()), ["/home/u/.local/bin/vinyl"])
        env = FakeWorld(present=["/usr/bin/vinyl"], environ={"VINYL_BIN": "/opt/py /opt/vinyl"})
        self.assertEqual(adapters.vinyl_command(env.context()), ["/opt/py", "/opt/vinyl"])
        self.assertIsNone(adapters.vinyl_command(FakeWorld().context()))

    def test_not_installed(self):
        row = adapters.vinyl_adapter(FakeWorld().context())
        self.assertEqual((row["installed"], row["health"]), (False, "off"))

    def test_idle_daemon_with_endpoint_from_the_mirror(self):
        world = FakeWorld(present=["/usr/bin/vinyl"],
                          cli={("/usr/bin/vinyl", "status"): ok(
                              {"state": "idle", "segments_decoded": 7, "trace_ref": "vinyl-trace:1"})},
                          files={VINYL_STATE: {"mode": "client", "server": "big:7733"}})
        row = adapters.vinyl_adapter(world.context())
        self.assertEqual((row["running"], row["health"]), (True, "ok"))
        self.assertEqual(row["detail"]["endpoint"], "big:7733")
        self.assertIn("big:7733", row["summary"])
        self.assertEqual(row["detail"]["trace_ref"], "vinyl-trace:1")

    def test_listening_reads_as_dictating(self):
        world = FakeWorld(present=["/usr/bin/vinyl"],
                          cli={("/usr/bin/vinyl", "status"): ok({"state": "listening"})})
        row = adapters.vinyl_adapter(world.context())
        self.assertTrue(row["detail"]["listening"])
        self.assertIn("dictating", row["summary"])

    def test_last_error_is_warn(self):
        world = FakeWorld(present=["/usr/bin/vinyl"],
                          cli={("/usr/bin/vinyl", "status"): ok({"state": "idle", "last_error": "mic gone"})})
        row = adapters.vinyl_adapter(world.context())
        self.assertEqual(row["health"], "warn")
        self.assertIn("mic gone", row["summary"])

    def test_daemon_not_answering_is_fail(self):
        world = FakeWorld(present=["/usr/bin/vinyl"],
                          cli={("/usr/bin/vinyl", "status"): Run(1, "", "no socket")})
        row = adapters.vinyl_adapter(world.context())
        self.assertEqual((row["installed"], row["running"], row["health"]), (True, False, "fail"))

    def test_settings_command_prefers_the_deb_then_the_checkout(self):
        deb = FakeWorld(present=["/usr/lib/vinyl/bin/vinyl-settings",
                                 "/repo/components/vinyl/bin/vinyl-settings"])
        self.assertEqual(adapters.vinyl_settings_command(deb.context()),
                         ["/usr/bin/python3", "/usr/lib/vinyl/bin/vinyl-settings"])
        checkout = FakeWorld(present=["/repo/components/vinyl/bin/vinyl-settings"])
        self.assertEqual(adapters.vinyl_settings_command(checkout.context()),
                         ["/usr/bin/python3", "/repo/components/vinyl/bin/vinyl-settings"])
        self.assertIsNone(adapters.vinyl_settings_command(FakeWorld().context()))

    def test_never_imports_vinyl_code(self):
        import sys
        self.assertFalse([m for m in sys.modules if m == "vinyl" or m.startswith("vinyl.")])


class TelephonyAdapterTest(unittest.TestCase):
    def test_disabled_is_off_not_degraded(self):
        world = FakeWorld(present=[TELEPHONY], cli={
            (TELEPHONY, "--json", "status"): ok({"mode": "disabled", "ring_enabled": True})})
        row = adapters.telephony_adapter(world.context())
        self.assertEqual((row["installed"], row["running"], row["health"]), (True, True, "off"))

    def test_shadow_mode_is_ok_and_reports_ring(self):
        world = FakeWorld(present=[TELEPHONY], cli={
            (TELEPHONY, "--json", "status"): ok({"mode": "shadow", "ring_enabled": True,
                                                 "verified_ingress": False})})
        row = adapters.telephony_adapter(world.context())
        self.assertEqual(row["health"], "ok")
        self.assertIn("ring on", row["summary"])

    def test_status_failure_is_fail(self):
        world = FakeWorld(present=[TELEPHONY],
                          cli={(TELEPHONY, "--json", "status"): Run(2, "", "bad")})
        self.assertEqual(adapters.telephony_adapter(world.context())["health"], "fail")

    def test_not_installed(self):
        self.assertFalse(adapters.telephony_adapter(FakeWorld().context())["installed"])


class PhonographAdapterTest(unittest.TestCase):
    URL = "https://phonograph.intellifornia.com"

    def test_default_url_and_ok(self):
        world = FakeWorld(http={self.URL + "/healthz": (200, '{"ok": true, "version": "0.4.0"}')})
        row = adapters.phonograph_adapter(world.context())
        self.assertEqual((row["installed"], row["running"], row["health"]), (True, True, "ok"))
        self.assertIn("0.4.0", row["summary"])
        self.assertEqual(world.calls[0][0], self.URL + "/healthz")
        self.assertLessEqual(world.calls[0][1], 5)

    def test_env_overrides_the_url_and_tolerates_a_healthz_suffix(self):
        for configured in ("https://p.example/", "https://p.example/healthz"):
            world = FakeWorld(environ={"HEYMA_PHONOGRAPH_URL": configured},
                              http={"https://p.example/healthz": (200, "{}")})
            with self.subTest(configured=configured):
                row = adapters.phonograph_adapter(world.context())
                self.assertEqual(row["health"], "ok")
                self.assertEqual(row["detail"]["url"], "https://p.example")

    def test_unreachable_and_http_errors_are_fail(self):
        for answer, word in (((0, ""), "unreachable"), ((503, ""), "HTTP 503")):
            world = FakeWorld(http={self.URL + "/healthz": answer})
            with self.subTest(answer=answer):
                row = adapters.phonograph_adapter(world.context())
                self.assertEqual((row["running"], row["health"]), (False, "fail"))
                self.assertIn(word, row["summary"])

    def test_ok_false_in_the_body_is_fail(self):
        world = FakeWorld(http={self.URL + "/healthz": (200, '{"ok": false}')})
        self.assertEqual(adapters.phonograph_adapter(world.context())["health"], "fail")

    def test_can_be_switched_off_for_a_host(self):
        row = adapters.phonograph_adapter(FakeWorld(environ={"HEYMA_PHONOGRAPH": "off"}).context())
        self.assertFalse(row["installed"])


VOXXY = "/home/u/.local/bin/voxxy"
VOXXY_HEALTH = (VOXXY, "health", "--json")


class VoxxyAdapterTest(unittest.TestCase):
    def health(self, rc, payload=None, stderr=""):
        world = FakeWorld(present=[VOXXY], cli={VOXXY_HEALTH: Run(rc, json.dumps(payload or {}), stderr)})
        return world, adapters.voxxy_adapter(world.context())

    def test_ok_names_the_ready_engines(self):
        world, row = self.health(0, {"status": "ok", "engines": [
            {"name": "voxcpm", "ready": True}, {"name": "elevenlabs", "ready": True}]})
        self.assertEqual((row["installed"], row["running"], row["health"]), (True, True, "ok"))
        self.assertEqual(row["summary"], "up · voxcpm, elevenlabs ready")
        self.assertEqual(world.calls, [(VOXXY_HEALTH, adapters.CLI_TIMEOUT_S)])
        self.assertEqual([a["id"] for a in row["actions"]], ["voxxy.test", "voxxy.open"])

    def test_exit_2_is_warn_and_says_what_is_down(self):
        _, row = self.health(2, {"status": "degraded", "engines": [
            {"name": "voxcpm", "ready": False}, {"name": "elevenlabs", "ready": True}]})
        self.assertEqual(row["health"], "warn")
        self.assertEqual(row["summary"], "degraded · voxcpm down · elevenlabs ready")

    def test_exit_3_is_fail_unreachable_with_the_url(self):
        _, row = self.health(3)
        self.assertEqual((row["running"], row["health"]), (False, "fail"))
        self.assertEqual(row["summary"], "unreachable · https://vox.delo.sh")

    def test_any_other_exit_is_fail_with_the_first_error_line(self):
        _, row = self.health(1, stderr="Traceback\nboom")
        self.assertEqual(row["health"], "fail")
        self.assertIn("Traceback", row["summary"])

    def test_resolution_order_never_uses_path(self):
        self.assertIsNone(adapters.voxxy_command(FakeWorld().context()))
        self.assertEqual(adapters.voxxy_command(FakeWorld(present=["/usr/bin/voxxy", VOXXY]).context()),
                         ["/usr/bin/voxxy"])
        self.assertEqual(adapters.voxxy_command(FakeWorld(environ={"VOXXY_BIN": "uvx voxxy"}).context()),
                         ["uvx", "voxxy"])

    def test_not_installed_and_switched_off(self):
        self.assertFalse(adapters.voxxy_adapter(FakeWorld().context())["installed"])
        world = FakeWorld(present=[VOXXY], environ={"HEYMA_VOXXY": "off"})
        self.assertFalse(adapters.voxxy_adapter(world.context())["installed"])
        self.assertFalse(adapters.quick_installed(world.context())["voxxy"])
        self.assertEqual(world.calls, [])

    def test_vox_url_names_the_server(self):
        world = FakeWorld(present=[VOXXY], environ={"VOX_URL": "https://v.example/"},
                          cli={VOXXY_HEALTH: Run(3)})
        self.assertEqual(adapters.voxxy_adapter(world.context())["detail"]["url"], "https://v.example")


class QuickProbeTest(unittest.TestCase):
    def test_no_subprocess_and_no_network(self):
        world = FakeWorld(present=[WAX, "/usr/bin/vinyl"])
        found = adapters.quick_installed(world.context())
        self.assertEqual(found, {"wax": True, "vinyl": True, "telephony": False, "phonograph": True,
                                 "voxxy": False})
        self.assertEqual(world.calls, [])


class RealRunnersTest(unittest.TestCase):
    def test_missing_binary_is_rc_127_not_an_exception(self):
        self.assertEqual(adapters.run_cli(["/nonexistent/heyma-fake"]).rc, 127)

    def test_timeout_is_rc_124(self):
        self.assertEqual(adapters.run_cli(["/bin/sleep", "5"], timeout=0.2).rc, 124)


if __name__ == "__main__":
    unittest.main()
