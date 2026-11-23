import unittest

from heyma_shell import model

from fakes import NOW, wax_snapshot

ALL = {"wax": True, "vinyl": True, "telephony": True, "phonograph": True}


def row(product, health="ok", installed=True, summary=""):
    return {"id": product, "installed": installed, "running": True, "health": health,
            "summary": summary or f"{product} {health}", "actions": [], "detail": {}}


def inputs(wax=None, vinyl=None, sections=None, slow=(), installed=ALL):
    return model.Inputs(
        wax_snap=wax if wax is not None else wax_snapshot(),
        vinyl_snap=vinyl if vinyl is not None else {"state": "idle"},
        sections=sections or {}, slow_degraded=set(slow), installed=installed, now=NOW)


class CombinedColourTest(unittest.TestCase):
    def test_green_when_everything_is_fine(self):
        self.assertEqual(model.verdict(inputs()).colour, "green")

    def test_red_while_recording(self):
        view = model.verdict(inputs(wax=wax_snapshot(stream="recording")))
        self.assertEqual((view.colour, view.recording, view.dictating), ("red", True, False))
        self.assertIn("recording", view.tooltip)

    def test_red_while_dictating(self):
        for state in ("listening", "stopping"):
            with self.subTest(state=state):
                view = model.verdict(inputs(vinyl={"state": state}))
                self.assertEqual((view.colour, view.dictating), ("red", True))

    def test_red_while_both(self):
        view = model.verdict(inputs(wax=wax_snapshot(stream="recording"), vinyl={"state": "listening"}))
        self.assertEqual(view.colour, "red")
        self.assertIn("recording + dictating", view.tooltip)

    def test_a_hot_mic_beats_every_degraded_section(self):
        view = model.verdict(inputs(vinyl={"state": "listening"},
                                    wax=wax_snapshot(queue={"failed": 3}),
                                    sections={p: row(p) for p in model.PRODUCTS},
                                    slow=["phonograph"]))
        self.assertEqual(view.colour, "red")
        self.assertIn("wax", view.degraded)

    def test_yellow_for_each_kind_of_degraded_section(self):
        cases = {
            "wax mirror": inputs(wax=wax_snapshot(queue={"failed": 1})),
            "wax stale": inputs(wax=wax_snapshot(age=999)),
            "wax missing": model.Inputs(wax_snap=None, vinyl_snap={"state": "idle"},
                                        installed=ALL, now=NOW),
            "vinyl error": inputs(vinyl={"state": "idle", "last_error": "x"}),
            "vinyl daemon down": inputs(slow=["vinyl"]),
            "telephony": inputs(slow=["telephony"]),
            "phonograph": inputs(slow=["phonograph"]),
        }
        for name, case in cases.items():
            with self.subTest(name):
                self.assertEqual(model.verdict(case).colour, "yellow")

    def test_a_section_that_is_not_installed_cannot_degrade_the_icon(self):
        only_vinyl = {"wax": False, "vinyl": True, "telephony": False, "phonograph": False}
        view = model.verdict(inputs(wax=wax_snapshot(age=999), slow=["telephony", "phonograph"],
                                    installed=only_vinyl))
        self.assertEqual(view.colour, "green")

    def test_vinyl_only_host_has_no_wax_dependence(self):
        only_vinyl = {"wax": False, "vinyl": True, "telephony": False, "phonograph": False}
        view = model.verdict(model.Inputs(wax_snap=None, vinyl_snap={"state": "listening"},
                                          installed=only_vinyl, now=NOW))
        self.assertEqual((view.colour, view.dictating, view.recording), ("red", True, False))

    def test_silent_recording_flashes_yellow_but_a_steady_icon_does_not(self):
        silent = wax_snapshot(stream="recording")
        silent["stream"]["silent"] = True
        self.assertEqual(model.verdict(inputs(wax=silent)).flash, "yellow")
        self.assertIsNone(model.verdict(inputs()).flash)

    def test_off_telephony_does_not_degrade(self):
        sections = {p: row(p) for p in model.PRODUCTS}
        sections["telephony"] = row("telephony", "off")
        deb = model.Debounce()
        self.assertEqual(deb.update(sections), set())

    def test_sections_decide_visibility_once_the_registry_has_answered(self):
        sections = {"wax": row("wax"), "vinyl": row("vinyl", installed=False)}
        self.assertEqual(model.installed_ids(inputs(sections=sections)), ["wax"])
        self.assertEqual(model.installed_ids(inputs()), ["wax", "vinyl", "telephony", "phonograph"])


class DebounceTest(unittest.TestCase):
    def test_turns_on_only_after_two_bad_reads_and_clears_at_once(self):
        deb = model.Debounce()
        bad = {"phonograph": row("phonograph", "fail")}
        good = {"phonograph": row("phonograph", "ok")}
        self.assertEqual(deb.update(bad), set())
        self.assertEqual(deb.update(bad), {"phonograph"})
        self.assertEqual(deb.update(bad), {"phonograph"})
        self.assertEqual(deb.update(good), set())
        self.assertEqual(deb.update(bad), set())

    def test_uninstalled_rows_never_count(self):
        deb = model.Debounce(after=1)
        self.assertEqual(deb.update({"vinyl": row("vinyl", "fail", installed=False)}), set())


class MenuTest(unittest.TestCase):
    def test_top_level_shape_matches_the_design(self):
        top = [(e["kind"], e.get("id")) for e in model.MENU]
        self.assertEqual(top, [
            ("item", "dictation"), ("item", "recording"), ("separator", None),
            ("submenu", "wax"), ("submenu", "vinyl"), ("submenu", "telephony"),
            ("submenu", "phonograph"), ("submenu", "voxxy"), ("separator", None),
            ("item", "settings"), ("item", "quit")])
        labels = {e["id"]: e["label"] for e in model.MENU if "label" in e}
        self.assertEqual(labels["settings"], "Settings…")
        self.assertEqual(labels["quit"], "Quit HeyMa")

    def test_submenu_contents(self):
        sub = {e["id"]: [c.get("id") for c in e["children"]] for e in model.MENU
               if e["kind"] == "submenu"}
        self.assertEqual(sub["wax"], ["wax.body"])
        self.assertEqual(sub["vinyl"], ["vinyl.status", "vinyl.trace", "vinyl.settings"])
        self.assertEqual(sub["telephony"], ["telephony.status", "telephony.silence", "telephony.page"])
        self.assertEqual(sub["phonograph"], ["phonograph.status", "phonograph.open"])
        self.assertEqual(sub["voxxy"], ["voxxy.status", "voxxy.listen", "voxxy.test", "voxxy.open"])

    def test_every_clickable_row_has_a_unique_id(self):
        ids = [e["id"] for e in model.walk() if e["kind"] == "item"]
        self.assertEqual(len(ids), len(set(ids)))

    def test_labels_flip_the_two_toggles(self):
        idle = inputs()
        out = model.labels(idle, model.verdict(idle))
        self.assertEqual(out["dictation"]["label"], "Start dictation")
        self.assertEqual(out["recording"]["label"], "Start recording")
        hot = inputs(wax=wax_snapshot(stream="recording"), vinyl={"state": "listening"})
        out = model.labels(hot, model.verdict(hot))
        self.assertEqual(out["dictation"]["label"], "Stop dictation")
        self.assertEqual(out["recording"]["label"], "Stop recording")

    def test_sections_are_visible_only_when_installed(self):
        case = inputs(installed={"wax": True, "vinyl": False, "telephony": False, "phonograph": True,
                                 "voxxy": True})
        out = model.labels(case, model.verdict(case))
        self.assertEqual({p: out[p]["visible"] for p in model.PRODUCTS},
                         {"wax": True, "vinyl": False, "telephony": False, "phonograph": True,
                          "voxxy": True})
        self.assertFalse(out["dictation"]["visible"])

    def test_recording_toggle_is_disabled_while_waxd_is_down(self):
        case = inputs(wax=wax_snapshot(age=999))
        self.assertFalse(model.labels(case, model.verdict(case))["recording"]["sensitive"])

    def test_vinyl_rows_carry_endpoint_trace_and_settings_availability(self):
        sections = {"vinyl": {**row("vinyl"), "detail": {"endpoint": "big:7733"}}}
        case = inputs(vinyl={"state": "idle", "trace_ref": "vinyl-trace:9"}, sections=sections)
        out = model.labels(case, model.verdict(case), vinyl_settings=True)
        self.assertIn("endpoint big:7733", out["vinyl.status"]["label"])
        self.assertTrue(out["vinyl.trace"]["sensitive"])
        self.assertTrue(out["vinyl.settings"]["sensitive"])
        out = model.labels(case, model.verdict(case), vinyl_settings=False)
        self.assertFalse(out["vinyl.settings"]["sensitive"])

    def test_trace_copy_is_disabled_without_a_reference_or_while_listening(self):
        for snap in ({"state": "idle"}, {"state": "listening", "trace_ref": "r"}):
            with self.subTest(snap=snap):
                case = inputs(vinyl=snap)
                self.assertFalse(model.labels(case, model.verdict(case))["vinyl.trace"]["sensitive"])

    def test_telephony_and_phonograph_status_lines_come_from_the_registry(self):
        sections = {"telephony": row("telephony", summary="shadow · ring on"),
                    "phonograph": row("phonograph", summary="up · v0.4.0")}
        case = inputs(sections=sections | {"wax": row("wax"), "vinyl": row("vinyl")})
        out = model.labels(case, model.verdict(case))
        self.assertEqual(out["telephony.status"]["label"], "shadow · ring on")
        self.assertEqual(out["phonograph.status"]["label"], "up · v0.4.0")

    def test_voxxy_row_and_tooltip_come_from_the_registry(self):
        sections = {"voxxy": row("voxxy", summary="up · voxcpm ready"), "wax": row("wax")}
        case = inputs(sections=sections)
        view = model.verdict(case)
        self.assertEqual(model.labels(case, view)["voxxy.status"]["label"], "up · voxcpm ready")
        self.assertIn("Voxxy: up · voxcpm ready", view.tooltip)

    def test_speak_on_this_desk_label_follows_the_unit(self):
        for active, label in ((False, model.SPEAK_HERE_OFF), (True, model.SPEAK_HERE_ON)):
            voxxy = {**row("voxxy"), "detail": {"speaker": {"active": active}}}
            case = inputs(sections={"voxxy": voxxy})
            with self.subTest(active=active):
                out = model.labels(case, model.verdict(case))
                self.assertEqual(out["voxxy.listen"]["label"], label)
                self.assertTrue(out["voxxy.listen"]["sensitive"])

    def test_a_degraded_voxxy_turns_the_icon_yellow_never_red(self):
        sections = {"voxxy": row("voxxy", "fail")}
        view = model.verdict(inputs(sections=sections, slow=["voxxy"]))
        self.assertEqual(view.colour, "yellow")
        self.assertIn("voxxy", view.degraded)


if __name__ == "__main__":
    unittest.main()
