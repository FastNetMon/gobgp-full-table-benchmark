# SPDX-License-Identifier: Apache-2.0
"""Guard against publishing incomplete runs or confusing memory scopes."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from compare import Comparison, load_config, parse_request, sha256
from report import load_run


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.reference = {"destinations": 2, "paths": 2, "nlri_bytes": 8,
                          "attribute_bytes": 40, "sha256": "a" * 64}
        names = ["before", "after"]
        self.meta = {"status": "complete", "config": {
            "variants": [{"name": n, "revision": "abc"} for n in names],
            "expected": {"destinations": 2, "paths": 2},
            "warmup_rounds": 1, "measured_rounds": 1, "memory": True},
            "correctness": {k: {n: copy.deepcopy(self.reference) for n in names} for k in ("before", "after")}}
        self.rows, self.memory = [], []
        for name in names:
            for mode in ("structured", "mixed", "binary"):
                for round_id, total in [(0, 999), (1, 20 if name == "before" else 10)]:
                    self.rows.append({"daemon": name, "mode": mode, "round": round_id,
                        "warmup": round_id == 0, "dests": 2, "paths": 2,
                        "nlri_bytes": 8 if mode == "binary" else 0,
                        "attr_bytes": 40 if mode != "structured" else 0,
                        "total_ms": total, "ttfb_ms": 2, "in_recv_ms": 5,
                        "in_proc_ms": 1, "server_cpu_seconds": .1})
                self.memory.append({"daemon": name, "mode": mode, "validation": copy.deepcopy(self.reference),
                                    "request": {"dests": 2, "paths": 2}, "baseline": {"rss_kib": 10}, "peak_rss_kib": 20})

    def write(self):
        for name, data in [("results", self.rows), ("memory", self.memory), ("run-metadata", self.meta)]:
            (self.root / (name + ".json")).write_text(json.dumps(data))

    def test_warmups_are_excluded_from_medians(self):
        self.write()
        summary = load_run(self.root)[3]
        self.assertEqual([r["total_ms"] for r in summary], [20, 20, 20, 10, 10, 10])

    def test_rejects_equal_counts_with_different_route_content(self):
        self.meta["correctness"]["before"]["after"]["sha256"] = "b" * 64
        self.meta["correctness"]["after"]["after"]["sha256"] = "b" * 64
        self.write()
        with self.assertRaisesRegex(ValueError, "different route data"):
            load_run(self.root)

    def test_rejects_changed_table_after_timing(self):
        self.meta["correctness"]["after"]["before"]["attribute_bytes"] += 1
        self.write()
        with self.assertRaisesRegex(ValueError, "before/after"):
            load_run(self.root)

    def test_rejects_missing_or_duplicate_round(self):
        for change in (lambda: self.rows.pop(), lambda: self.rows.append(copy.deepcopy(self.rows[0]))):
            original = copy.deepcopy(self.rows)
            change()
            self.write()
            with self.assertRaisesRegex(ValueError, "round"):
                load_run(self.root)
            self.rows = original

    def test_rejects_incorrect_warmup_label(self):
        self.rows[0]["warmup"] = False
        self.write()
        with self.assertRaisesRegex(ValueError, "warm-up"):
            load_run(self.root)

    def test_cannot_substitute_lifetime_peaks_for_missing_isolated_cases(self):
        self.memory.pop()
        self.write()
        with self.assertRaisesRegex(ValueError, "fresh-process"):
            load_run(self.root)

    def test_rejects_corrupt_cold_validation(self):
        self.memory[0]["validation"]["sha256"] = "b" * 64
        self.write()
        with self.assertRaisesRegex(ValueError, "fresh-process"):
            load_run(self.root)

    def test_rejects_failed_run_and_wrong_byte_counts(self):
        self.meta["status"] = "failed"
        self.write()
        with self.assertRaisesRegex(ValueError, "incomplete or failed"):
            load_run(self.root)
        self.meta["status"] = "complete"
        self.rows[-1]["attr_bytes"] = 0
        self.write()
        with self.assertRaisesRegex(ValueError, "Attribute byte"):
            load_run(self.root)

    def test_request_parser_checks_counts_and_binary_bytes(self):
        line = "run=1 dests=2 paths=2 decoded=2 nlri_bytes=8 attr_bytes=40 ttfb_ms=1.0 in_recv_ms=2.0 in_proc_ms=0.1 total_ms=3.0\n(sink=123)\n"
        self.assertEqual(parse_request(line, "binary", self.reference, self.reference)["sink"], 123)
        for invalid in (line.replace("dests=2", "dests=1"), line.replace("attr_bytes=40", "attr_bytes=39")):
            with self.assertRaises(ValueError):
                parse_request(invalid, "binary", self.reference, self.reference)

    def test_config_resolves_relative_paths_and_checks_snapshot_pin(self):
        for name in ("rib", "daemon", "cli"):
            (self.root / name).write_bytes(b"fixture")
        cfg = {"snapshot": {"path": "rib", "sha256": sha256(self.root / "rib")},
               "source": {"daemon": "daemon", "cli": "cli"}, "expected": {"destinations": 2, "paths": 2},
               "variants": [{"name": "before", "binary": "daemon", "kind": "gobgp", "revision": "abc"}]}
        path = self.root / "config.json"
        path.write_text(json.dumps(cfg))
        self.assertEqual(load_config(path)["variants"][0]["binary"], str(self.root / "daemon"))
        (self.root / "rib").write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            load_config(path)

    def test_compressed_snapshot_suffix_is_preserved_for_gobgp(self):
        for suffix in (".bz2", ".gz", ""):
            cfg = {"snapshot": {"path": "input.mrt" + suffix}}
            run = Comparison(cfg, self.root)
            self.assertEqual(run.snapshot_target, "/bench/input.mrt" + suffix)

    def test_remote_pid_cannot_measure_an_unrelated_local_process(self):
        run = Comparison({"snapshot": {"path": "input.mrt"}}, self.root)
        with patch.object(run, "docker", return_value="123"), \
             patch("compare.Path.read_bytes", return_value=b"/unrelated-process\0"), \
             patch("compare.process_stats") as stats:
            with self.assertRaisesRegex(ValueError, "local host processes"):
                run.pid("before")
            stats.assert_not_called()


if __name__ == "__main__":
    unittest.main()
