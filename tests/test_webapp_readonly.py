import json
import unittest

import polymarket_arb.webapp as webapp


class TestReadOnlyPayload(unittest.TestCase):
    def test_demo_payload_shape(self):
        p = webapp.read_only_payload(False)
        self.assertEqual(p["meta"]["source"], "demo")
        for key in ("polymarket", "cross_venue", "ev"):
            self.assertIn(key, p)
            self.assertIsInstance(p[key], list)
        # The bundled demo always has at least the seeded edges.
        self.assertTrue(p["polymarket"])
        # Must be JSON-serializable (it's returned by the serverless function).
        json.loads(json.dumps(p))

    def test_falls_back_to_demo_on_live_error(self):
        original = webapp.build_readonly_service

        def boom(live):
            raise RuntimeError("egress blocked")

        webapp.build_readonly_service = boom
        try:
            p = webapp.read_only_payload(True)
        finally:
            webapp.build_readonly_service = original

        self.assertEqual(p["meta"]["source"], "demo")
        self.assertIn("egress blocked", p["meta"]["live_error"])
        self.assertTrue(p["polymarket"])  # still renders real demo data

    def test_demo_service_is_used_when_not_live(self):
        svc = webapp.build_readonly_service(False)
        # Demo service exposes cross + ev scan fns and a dry-run config.
        self.assertEqual(svc.exec_config.mode, "dry-run")
        self.assertIsNotNone(svc.cross_scan_fn)


if __name__ == "__main__":
    unittest.main()
