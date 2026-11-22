from pathlib import Path
import unittest

from diagnostic_helper_iso import (
    EXPECTED_FILES,
    EXPECTED_STAGING_SHA256,
    EXPECTED_STAGING_SIZE,
    EXPECTED_VOLUME_ID,
    verify_helper_staging_iso,
)


STAGING_ISO = Path(
    r"C:\LCA-PhaseB-Media\5d9310328de54e18925a9d53cb1d0fa2"
    r"\helper.iso.8702fbd5e1b04e86a67db65329c8b95a.staging"
)
SOURCE_ROOT = Path(__file__).resolve().parents[1]


class DiagnosticHelperIsoTests(unittest.TestCase):
    @unittest.skipUnless(STAGING_ISO.is_file(), "operator staging ISO is not present")
    def test_pycdlib_122_pvd_volume_identifier_and_staging_contents(self):
        report = verify_helper_staging_iso(STAGING_ISO, SOURCE_ROOT)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["volume_id"], EXPECTED_VOLUME_ID)
        self.assertEqual(report["size_bytes"], EXPECTED_STAGING_SIZE)
        self.assertEqual(report["sha256"], EXPECTED_STAGING_SHA256)
        self.assertEqual(report["directories"], [])
        self.assertEqual(set(report["inventory"]), set(EXPECTED_FILES))
        for name, (expected_hash, expected_size) in EXPECTED_FILES.items():
            self.assertEqual(report["inventory"][name], {
                "size_bytes": expected_size,
                "sha256": expected_hash,
            })


if __name__ == "__main__":
    unittest.main()
