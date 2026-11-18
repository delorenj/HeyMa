import unittest


class TrayUiImportTest(unittest.TestCase):
    def test_module_imports_without_gtk(self):
        """The registry and `heyma status` run under any python; only `tray` needs gi."""
        import heyma_shell.tray_ui
        self.assertTrue(callable(heyma_shell.tray_ui.main))

    def test_every_clickable_row_has_an_action_in_main(self):
        """A menu row with no action would raise KeyError on click."""
        import inspect
        from heyma_shell import model, tray_ui
        source = inspect.getsource(tray_ui.main)
        for entry in model.walk():
            if entry["kind"] == "item":
                with self.subTest(row=entry["id"]):
                    self.assertIn(f'"{entry["id"]}"', source)


if __name__ == "__main__":
    unittest.main()
