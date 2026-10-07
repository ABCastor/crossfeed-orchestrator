"""Only configured model/level workers can become effort-labelled lanes."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from chatgpt_transport import catalog_models, Rejected


class CatalogWakeModeTests(unittest.TestCase):
    def row(self, label, **extra):
        return dict(id="chatgpt:" + label, object="model", saved=True,
                    row="Latest", level=2, **extra)

    def catalog(self, *rows):
        return catalog_models(dict(object="list", data=list(rows)))

    def test_native_defaults_are_not_admitted_as_high_lanes(self):
        rows = self.catalog(self.row("native", wake_mode="native"),
                            self.row("configured", wake_mode="configured"))
        self.assertEqual(list(rows), ["chatgpt:configured"])
        self.assertEqual(rows["chatgpt:configured"]["worker_level"], "high")

    def test_legacy_workers_keep_their_configured_effort(self):
        rows = self.catalog(self.row("legacy"))
        self.assertEqual(rows["chatgpt:legacy"]["worker_level"], "high")

    def test_native_only_catalog_has_no_fabricated_model_lane(self):
        self.assertEqual(self.catalog(self.row("native", wake_mode="native")), {})

    def test_unknown_wake_modes_are_invalid_catalog_data(self):
        for mode in ("automatic", "", None, 1):
            with self.subTest(mode=mode), self.assertRaises(Rejected):
                self.catalog(self.row("bad", wake_mode=mode))


if __name__ == "__main__":
    unittest.main()
