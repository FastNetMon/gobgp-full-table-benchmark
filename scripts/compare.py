#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compare pinned GoBGP/RustyBGP binaries on a frozen IPv4 Adj-RIB-In table.

Requires a local Linux Docker daemon: RSS/CPU are read from its host PIDs.
Only the containers and network created by this run are removed.
"""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import random
import re
import subprocess
import threading
import time
import uuid

MODES = {
    "structured": ["--binary", "false"],
    "mixed": ["--only-binary", "false", "--nlri-binary", "false", "--attr-binary", "true"],
    "binary": ["--binary", "true"],
}


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command(args, timeout=600):
    result = subprocess.run(list(map(str, args)), text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Command failed: {args}\n{result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def load_config(path):
    cfg = json.loads(path.read_text())
    cfg.setdefault("runtime_image", "gobgp-client-benchmark:ubuntu-24.04")
    for key, default in {"warmup_rounds": 1, "measured_rounds": 5, "seed": 20261016,
                         "deadline_seconds": 300, "ready_timeout_seconds": 300,
                         "settle_seconds": 12, "idle_seconds": 15,
                         "sample_interval_seconds": .025, "memory": True,
                         "subnet": "172.30.88.0/24"}.items():
        cfg.setdefault(key, default)
    for key in ("warmup_rounds", "measured_rounds", "seed", "deadline_seconds", "ready_timeout_seconds"):
        if type(cfg[key]) is not int or cfg[key] < (0 if key in ("warmup_rounds", "seed") else 1):
            raise ValueError(f"Invalid {key}")
    for key in ("settle_seconds", "idle_seconds", "sample_interval_seconds"):
        if not isinstance(cfg[key], (int, float)) or not 0 < cfg[key] < float("inf"):
            raise ValueError(f"Invalid {key}")
    if type(cfg["memory"]) is not bool:
        raise ValueError("memory must be a boolean")
    expected = cfg["expected"]
    for key in ("destinations", "paths"):
        if type(expected[key]) is not int or expected[key] <= 0:
            raise ValueError(f"expected.{key} must be positive")
    if not re.fullmatch(r"[0-9a-f]{64}", cfg["snapshot"]["sha256"]):
        raise ValueError("snapshot.sha256 must pin the input file")
    variants = cfg["variants"]
    names = [v["name"] for v in variants]
    if not names or len(set(names)) != len(names):
        raise ValueError("Provide unique variant names")
    for variant in variants:
        if variant["name"] in ("source", "client") or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", variant["name"]):
            raise ValueError("Variant names must use lowercase letters, digits and hyphens; source/client are reserved")
        if variant["kind"] not in ("gobgp", "rustybgp") or not variant.get("revision"):
            raise ValueError("Each variant needs kind and revision")
    # Binaries are supplied by the caller; the runner never fetches a moving 'latest'.
    entries = [(cfg["snapshot"], "path"), (cfg["source"], "daemon"), (cfg["source"], "cli")]
    entries += [(v, "binary") for v in variants]
    for obj, key in entries:
        value = Path(obj[key]).expanduser()
        value = (path.parent / value).resolve() if not value.is_absolute() else value.resolve()
        if not value.is_file() or ":" in str(value) or "," in str(value):
            raise ValueError(f"Not a mountable file: {value}")
        obj[key] = str(value)
    if sha256(Path(cfg["snapshot"]["path"])) != cfg["snapshot"]["sha256"]:
        raise ValueError("MRT snapshot SHA-256 mismatch")
    for obj in [cfg["source"], *variants]:
        if not isinstance(obj.get("extra_args", []), list) or not all(isinstance(x, str) for x in obj.get("extra_args", [])):
            raise ValueError("extra_args must be an array of arguments")
    network = ipaddress.ip_network(cfg["subnet"])
    if network.version != 4 or network.num_addresses < len(variants) + 5:
        raise ValueError("subnet must be IPv4 with room for source and targets")
    cfg["source_address"] = str(network.network_address + 2)
    for offset, variant in enumerate(variants, 3):
        variant["address"] = str(network.network_address + offset)
    return cfg


def process_stats(pid):
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    status = Path(f"/proc/{pid}/status").read_text()
    return {"time": time.time(),
            "cpu_seconds": (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"),
            "rss_kib": int(re.search(r"VmRSS:\s+(\d+)", status)[1]),
            "peak_rss_kib": int(re.search(r"VmHWM:\s+(\d+)", status)[1])}


class Monitor:
    def __init__(self, pids, interval):
        self.pids, self.interval = pids, interval
        self.rows, self.error = [], None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self.collect)

    def collect(self):
        try:
            while not self.done.is_set():
                for name, pid in self.pids.items():
                    self.rows.append({"daemon": name, **process_stats(pid)})
                self.done.wait(self.interval)
        except Exception as error:
            self.error = error

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, *_):
        self.done.set()
        self.thread.join()
        if self.error and exc_type is None:
            raise self.error


def check_counts(values, expected):
    for key in ("destinations", "paths"):
        if values[key] != expected[key]:
            raise ValueError(f"Wrong {key}: {values[key]} != {expected[key]}")


def parse_request(output, mode, expected, reference):
    lines = [s for s in output.splitlines() if s.startswith("run=")]
    if len(lines) != 1:
        raise ValueError("Expected exactly one successful client result")
    row = {k: float(v) if "." in v else int(v) for k, v in re.findall(r"(\w+)=([\d.]+)", lines[0])}
    check_counts({"destinations": row["dests"], "paths": row["paths"]}, expected)
    for key in ("ttfb_ms", "in_recv_ms", "in_proc_ms", "total_ms"):
        if key not in row or row[key] < 0:
            raise ValueError(f"Missing/invalid timing: {key}")
    if mode != "structured" and row["attr_bytes"] != reference["attribute_bytes"]:
        raise ValueError("Binary attribute byte count changed")
    if mode == "binary" and row["nlri_bytes"] != reference["nlri_bytes"]:
        raise ValueError("Binary NLRI byte count changed")
    row["sink"] = int(re.search(r"sink=(\d+)", output)[1])
    return row


class Comparison:
    def __init__(self, cfg, output):
        self.cfg, self.output = cfg, output
        self.network = "listpath-" + uuid.uuid4().hex[:10]
        self.running = set()
        self.created_network = False
        # GoBGP selects decompression from the filename suffix.
        suffix = Path(cfg["snapshot"]["path"]).suffix
        self.snapshot_target = "/bench/input.mrt" + (suffix if suffix in (".bz2", ".gz") else "")
        self.reference = None
        self.rows, self.memory = [], []
        self.metadata = {"schema_version": 1, "status": "running", "config": cfg,
                         "started_at": datetime.now(timezone.utc).isoformat(),
                         "harness_sha256": {name: sha256(Path(__file__).resolve().with_name(name))
                                            for name in ("compare.py", "report.py")},
                         "network": self.network, "source_preloaded": True,
                         "correctness": {"before": {}, "after": {}, "cold": {}},
                         "host": {"platform": platform.platform(), "cpu_count": os.cpu_count(),
                                  "meminfo": Path("/proc/meminfo").read_text(),
                                  "cpu_model": next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                                                     if line.startswith("model name")), platform.machine()),
                                  "python": platform.python_version(),
                                  "load_before": list(os.getloadavg())}}

    def docker(self, *args, **kwargs):
        return command(["docker", *args], **kwargs)

    def cli(self, name, *args, timeout=120):
        return self.docker("exec", self.network + "-" + name, "/bench/cli", *args, timeout=timeout)

    def start(self, variant=None):
        cfg = self.cfg
        name = variant["name"] if variant else "source"
        address = variant["address"] if variant else cfg["source_address"]
        neighbors = [(cfg["source_address"], 65001)] if variant else [(v["address"], 65002) for v in cfg["variants"]]
        config = {"global": {"config": {"as": 65002 if variant else 65001, "router-id": address}},
                  "neighbors": [{"config": {"neighbor-address": ip, "peer-as": asn},
                                 "afi-safis": [{"config": {"afi-safi-name": "ipv4-unicast"}}]}
                                for ip, asn in neighbors]}
        path = self.output / "configs" / (name + ".yaml")
        save(path, config)  # JSON is also valid YAML for both daemons.
        binary = variant["binary"] if variant else cfg["source"]["daemon"]
        container = self.network + "-" + name
        # Track before starting so cleanup also handles a partially failed start.
        self.running.add(name)
        self.docker("run", "-d", "--name", container, "--label", "listpath-run=" + self.network,
                    "--network", self.network, "--ip", address,
                    "-v", f"{binary}:/bench/daemon:ro", "-v", f"{cfg['source']['cli']}:/bench/cli:ro",
                    "-v", f"{cfg['snapshot']['path']}:{self.snapshot_target}:ro", "-v", f"{path}:/bench/config.yaml:ro",
                    "--entrypoint", "/bench/daemon", cfg["runtime_image"], "-f", "/bench/config.yaml",
                    *(variant or cfg["source"]).get("extra_args", []))

    def stop(self, name, label):
        container = self.network + "-" + name
        try:
            (self.output / "raw" / (label + "-daemon.log")).write_text(self.docker("logs", container))
        finally:
            self.docker("rm", "-f", container)
            self.running.discard(name)

    def ready(self, name):
        end = time.monotonic() + self.cfg["ready_timeout_seconds"]
        next_progress = 0
        while time.monotonic() < end:
            try:
                out = self.cli(name, "neighbor", self.cfg["source_address"])
                counts = [re.search(rf"{field}:\s+(\d+)", out) for field in ("Received", "Accepted")]
                if all(m and int(m[1]) == self.cfg["expected"]["paths"] for m in counts):
                    print(f"{name}: table ready ({self.cfg['expected']['paths']:,} paths)", flush=True)
                    return
                if time.monotonic() >= next_progress:
                    print(f"{name}: loading, received/accepted=" + "/".join(m[1] if m else "unknown" for m in counts), flush=True)
                    next_progress = time.monotonic() + 15
            except RuntimeError:
                pass
            time.sleep(1)
        raise RuntimeError(f"{name}: received/accepted counts did not reach the expected table")

    def client(self, variant, entrypoint, *args):
        return self.docker("run", "--rm", "--name", self.network + "-client",
                           "--label", "listpath-run=" + self.network, "--network", self.network,
                           "--entrypoint", "/src/build/" + entrypoint, self.cfg["runtime_image"],
                           *args, timeout=self.cfg["deadline_seconds"] + 60)

    def validate(self, variant, label):
        out = self.client(variant, "gobgp_table_validate", variant["address"] + ":50051",
                          self.cfg["source_address"], "adj-in", str(self.cfg["deadline_seconds"]))
        result = json.loads(out)
        check_counts(result, self.cfg["expected"])
        if not re.fullmatch(r"[0-9a-f]{64}", result["sha256"]):
            raise ValueError("Invalid route-data hash")
        for key in ("nlri_bytes", "attribute_bytes"):
            if key in self.cfg["expected"] and result[key] != self.cfg["expected"][key]:
                raise ValueError(f"Unexpected {key}")
        save(self.output / "raw" / (label + "-validation.json"), result)
        if self.reference is None:
            self.reference = result
        elif result != self.reference:
            raise ValueError(f"Route data differs: {label}: {result} != {self.reference}")
        return result

    def request(self, variant, mode, label):
        out = self.client(variant, "gobgp_client_benchmark", "--target", variant["address"] + ":50051",
                          "--peer", self.cfg["source_address"], "--table", "adj-in", "--iterations", "1",
                          "--decode", "true", "--deadline", str(self.cfg["deadline_seconds"]), *MODES[mode])
        (self.output / "raw" / (label + ".txt")).write_text(out)
        return parse_request(out, mode, self.cfg["expected"], self.reference)

    def pid(self, name):
        pid = int(self.docker("inspect", "-f", "{{.State.Pid}}", self.network + "-" + name).strip())
        # A remote Docker PID could accidentally name an unrelated local process.
        if Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0] != b"/bench/daemon":
            raise ValueError("Docker PIDs do not match local host processes")
        process_stats(pid)
        return pid

    def run(self):
        cfg, meta = self.cfg, self.metadata
        if self.docker("info", "--format", "{{.OSType}}").strip() != "linux":
            raise ValueError("Requires a local Linux Docker daemon")
        meta["runtime_image_id"] = self.docker("image", "inspect", "-f", "{{.Id}}", cfg["runtime_image"]).strip()
        meta["client_hashes"] = self.docker("run", "--rm", "--network", "none", "--entrypoint", "sha256sum",
                                            cfg["runtime_image"], "/src/build/gobgp_client_benchmark",
                                            "/src/build/gobgp_table_validate").strip()
        meta["binary_sha256"] = {v["name"]: sha256(Path(v["binary"])) for v in cfg["variants"]}
        meta["source_sha256"] = {k: sha256(Path(cfg["source"][k])) for k in ("daemon", "cli")}
        meta["version_output"] = {v["name"]: self.docker("run", "--rm", "--network", "none",
            "-v", f"{v['binary']}:/bench/daemon:ro", "--entrypoint", "/bench/daemon", cfg["runtime_image"], "--version").strip()
            for v in cfg["variants"]}
        self.docker("network", "create", "--subnet", cfg["subnet"], "--label", "listpath-run=" + self.network, self.network)
        self.created_network = True
        self.start()
        for _ in range(90):
            try:
                self.cli("source", "global")
                break
            except RuntimeError:
                time.sleep(1)
        else:
            raise RuntimeError("Source API failed to start")
        print("Preloading MRT before connecting any target", flush=True)
        (self.output / "raw/inject.txt").write_text(self.cli("source", "mrt", "inject", "global", self.snapshot_target,
             "--only-best", "--no-ipv6", "--nexthop", cfg["source_address"], timeout=900))
        for variant in cfg["variants"]:
            self.start(variant)
        for variant in cfg["variants"]:
            self.ready(variant["name"])
        time.sleep(cfg["settle_seconds"])
        pids = {v["name"]: self.pid(v["name"]) for v in cfg["variants"]}
        meta["steady_memory_before"] = {n: process_stats(p) for n, p in pids.items()}
        for variant in cfg["variants"]:
            name = variant["name"]
            meta["correctness"]["before"][name] = self.validate(variant, "steady-" + name + "-before")
            (self.output / "raw" / (name + "-sample-route.json")).write_text(
                self.cli(name, "-j", "neighbor", cfg["source_address"], "adj-in", "1.0.0.0/24"))
        jobs = [(v, mode) for v in cfg["variants"] for mode in MODES]
        rng = random.Random(cfg["seed"])
        with Monitor(pids, cfg["sample_interval_seconds"]) as monitor:
            for round_id in range(cfg["warmup_rounds"] + cfg["measured_rounds"]):
                order = list(jobs)
                rng.shuffle(order)
                for variant, mode in order:
                    name = variant["name"]
                    before = process_stats(pids[name])
                    start = time.time()
                    row = self.request(variant, mode, f"steady-{name}-{mode}-{round_id}")
                    end = time.time()
                    after = process_stats(pids[name])
                    row.update({"daemon": name, "mode": mode, "round": round_id,
                                "warmup": round_id < cfg["warmup_rounds"], "start": start, "end": end,
                                "server_cpu_seconds": after["cpu_seconds"] - before["cpu_seconds"],
                                "server_rss_kib_after": after["rss_kib"], "server_hwm_kib": after["peak_rss_kib"]})
                    self.rows.append(row)
                    save(self.output / "results.json", self.rows)
                    print(f"round={round_id} {name} {mode}: {row['total_ms']} ms", flush=True)
            meta["steady_memory_after"] = {n: process_stats(p) for n, p in pids.items()}
        save(self.output / "steady-rss.json", monitor.rows)
        for variant in cfg["variants"]:
            name = variant["name"]
            meta["correctness"]["after"][name] = self.validate(variant, "steady-" + name + "-after")
            self.stop(name, "steady-" + name)
        if cfg["memory"]:
            cold_jobs = list(jobs)
            rng.shuffle(cold_jobs)
            for variant, mode in cold_jobs:
                name = variant["name"]
                label = f"cold-{name}-{mode}"
                self.start(variant)
                self.ready(name)
                time.sleep(cfg["settle_seconds"])
                pid = self.pid(name)
                baseline = process_stats(pid)
                # No ListPath validation/warm-up before this memory window.
                with Monitor({name: pid}, cfg["sample_interval_seconds"]) as monitor:
                    start = time.time()
                    request = self.request(variant, mode, label)
                    end = time.time()
                    immediate = process_stats(pid)
                    time.sleep(cfg["idle_seconds"])
                    idle = process_stats(pid)
                save(self.output / "raw" / (label + "-rss.json"), monitor.rows)
                item = {"daemon": name, "mode": mode, "baseline": baseline, "request": request,
                        "start": start, "end": end, "immediate": immediate, "idle": idle,
                        "peak_rss_kib": idle["peak_rss_kib"],
                        "sampled_peak_rss_kib": max(r["rss_kib"] for r in monitor.rows)}
                item["validation"] = self.validate(variant, label)
                meta["correctness"]["cold"][label] = item["validation"]
                self.memory.append(item)
                save(self.output / "memory.json", self.memory)
                print(f"{label}: peak RSS={item['peak_rss_kib']/1048576:.3f} GiB", flush=True)
                self.stop(name, label)
        meta["host"]["load_after"] = list(os.getloadavg())
        meta["all_route_data_identical"] = True
        with (self.output / "results.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(self.rows[0]))
            writer.writeheader()
            writer.writerows(self.rows)

    def cleanup(self):
        errors = []
        # Also remove a client left running after a timeout/interruption.
        try:
            ids = self.docker("ps", "-aq", "--filter", "label=listpath-run=" + self.network).split()
            for container in ids:
                self.docker("rm", "-f", container)
        except Exception as error:
            errors.append(str(error))
        if self.created_network:
            try:
                self.docker("network", "rm", self.network)
            except Exception as error:
                errors.append(str(error))
        return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="New directory; existing data is never overwritten")
    parser.add_argument("--check", action="store_true", help="Validate config and input hash without running Docker")
    args = parser.parse_args()
    cfg = load_config(args.config.resolve())
    if args.check:
        print(json.dumps(cfg, indent=2))
        return
    output = args.output.resolve()
    if ":" in str(output) or "," in str(output):
        parser.error("Output path cannot contain ':' or ','")
    output.mkdir(parents=True, exist_ok=False)
    for name in ("raw", "configs"):
        (output / name).mkdir()
    comparison = Comparison(cfg, output)
    save(output / "run-metadata.json", comparison.metadata)
    try:
        comparison.run()
        comparison.metadata["status"] = "complete"
    except BaseException as error:
        comparison.metadata.update(status="failed", error=str(error))
        raise
    finally:
        # Preserve diagnostics even when the run fails; no successful report then.
        for name in list(comparison.running):
            try:
                comparison.stop(name, "final-" + name)
            except Exception:
                pass
        comparison.metadata["cleanup_errors"] = comparison.cleanup()
        comparison.metadata["finished_at"] = datetime.now(timezone.utc).isoformat()
        save(output / "run-metadata.json", comparison.metadata)
    print(f"Complete. Generate report: python3 scripts/report.py {output}")


if __name__ == "__main__":
    main()
