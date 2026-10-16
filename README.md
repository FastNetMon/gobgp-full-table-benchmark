# gobgp_client_benchmark

A minimal C++ gRPC client for benchmarking [gobgpd](https://github.com/osrg/gobgp)'s
`ListPath` RPC — useful when you need to understand where time is spent serving
a full BGP table to an API consumer (e.g. FastNetMon pulling ADJ_IN from a
route collector).

The comparison scripts also support RustyBGP and multiple versions of either
daemon. This measures **API retrieval**, not BGP convergence, propagation
latency, update throughput or policy evaluation.

For each iteration it measures:

- **ttfb** — time from sending the request to receiving the first streamed response.
- **in_recv** — time the client was blocked inside `Read()` (server + network + gRPC).
- **in_proc** — time spent between `Read()` calls, i.e. the client-side cost of
  walking destinations/paths and optionally touching the NLRI + attribute bytes.
- **total** — wall-clock time for the whole stream.

By default the client requests the "fully binary" path
(`enable_only_binary=true`, `enable_nlri_binary=true`, `enable_attribute_binary=true`)
so gobgpd skips its native→proto attribute type-switch and hands back the raw
wire-format BGP NLRI and path attributes. Flip the binary flags off to measure
the cost of the proto-native encoding path for comparison.

`--decode true` scans the binary buffers; it does not fully decode BGP NLRI
or attributes. In structured mode there are no binary buffers to scan.

## Reproducible comparisons and graphs

See [METHODOLOGY.md](METHODOLOGY.md) for the measurement definitions, fixed-input
setup, correctness checks, limitations and build provenance to record.

```sh
./build.sh
# Copy examples/comparison.json, supply pinned binaries and the MRT file,
# then update paths/revisions/build_info in your config.
python3 scripts/compare.py examples/comparison.json --output runs/comparison --check
python3 scripts/compare.py examples/comparison.json --output runs/comparison
python3 -m venv .venv
.venv/bin/pip install -r requirements-report.txt
.venv/bin/python scripts/report.py runs/comparison
```

The runner requires a local Linux Docker daemon and Python 3.9+. It preloads
the MRT snapshot before connecting targets, checks route-data hashes, runs
shuffled timing rounds, and measures each mode's memory in a fresh daemon.
It records total/first-response/receive/processing time, server CPU, route and
byte counts, loaded/peak/idle RSS, time-series samples and exact binary hashes.

`runs/comparison/report/` contains a Markdown report, JSON/CSV tables, and
PNG/SVG graphs for latency, first response, CPU, client timing and memory.
Fresh-process memory and combined-workload lifetime peaks are reported
separately. A combined peak must not be described as binary-only memory use.

The reporter also reads the saved 16 October 2026 comparison/bounded-stream
run bundles. Without fresh-process measurements it labels the memory graph
as a combined-workload peak and makes no per-mode memory claim.

## Build

The supported build path is a containerised build against Ubuntu 24.04's
packaged gRPC / Protobuf so you don't need a local toolchain:

```
./build.sh
# produces ./dist/gobgp_client_benchmark and ./dist/gobgp_table_validate
```

The resulting binary dynamically links against `libgrpc++1.51t64` and
`libprotobuf32t64` — install those packages on the target host to run it.

If you'd rather build directly:

```
cmake -B build -S .
cmake --build build -j
```

## Usage

```
# default: adj-in table, "fully binary" mode (only_binary + nlri_binary + attr_binary),
# iterate 5 times, scan every binary NLRI + attribute slice.
./build/gobgp_client_benchmark --target SERVER:50051 --peer x.x.x.x --iterations 5

# Non-only-binary setup: server still returns raw NLRI/attribute bytes, but
# doesn't take the enable_only_binary shortcut — exercises the mixed code path.
./build/gobgp_client_benchmark --target SERVER:50051 --peer x.x.x.x \
    --only-binary false --nlri-binary true --attr-binary true

# Measure with vs. without client-side byte scanning. With --decode false the client
# receives the stream but never touches the per-path byte buffers, so in_proc
# drops to almost zero and total ~= in_recv. Useful for isolating server cost.
./build/gobgp_client_benchmark --target SERVER:50051 --peer x.x.x.x --decode false

# Global RIB (the router's own post-policy table). --peer is not required
# because a global lookup isn't peer-scoped.
./build/gobgp_client_benchmark --target SERVER:50051 --table global

# First path per destination: process just paths(0) instead of every path.
# This changes client processing only; it does not ask the server to filter
# its response or guarantee that paths(0) is the installed route.
./build/gobgp_client_benchmark --target SERVER:50051 --peer x.x.x.x --best-only true
```

## Flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `--target HOST:PORT` | `127.0.0.1:50051` | gRPC endpoint of gobgpd. |
| `--peer IP` | — | Peer address. Required for `adj-in`, `adj-out`, `local`. |
| `--table NAME` | `adj-in` | One of `adj-in`, `adj-out`, `global`, `local`. |
| `--iterations N` | `3` | Number of back-to-back runs. |
| `--binary BOOL` | `true` | Shorthand: set all three binary flags at once. |
| `--only-binary BOOL` | `true` | `ListPathRequest.enable_only_binary`. |
| `--nlri-binary BOOL` | `true` | `ListPathRequest.enable_nlri_binary`. |
| `--attr-binary BOOL` | `true` | `ListPathRequest.enable_attribute_binary`. |
| `--decode BOOL` | `true` | Scan `nlri_binary` + every `pattrs_binary` slice per path. |
| `--best-only BOOL` | `false` | Process only `paths(0)` per destination. |
| `--deadline SEC` | `120` | Per-iteration gRPC deadline. |
| `--max-recv-mb N` | `1024` | Max individual gRPC receive message size in MiB, not a limit on the whole stream. |

Booleans accept `true`/`false`, `1`/`0`, `yes`/`no`.

## Output

One line per iteration on stdout, plus a config header and a sink value on
stderr (the sink is only there to stop the compiler from optimising out the
byte-touch loop).

```
target=SERVER:50051 table=adj-in peer=x.x.x.x iterations=5 only_binary=1 nlri_binary=1 attr_binary=1 decode=1 best_only=0
run=1 dests=987654 paths=987654 decoded=987654 nlri_bytes=... attr_bytes=... ttfb_ms=12.3 in_recv_ms=4321.0 in_proc_ms=890.1 total_ms=5223.4
...
```

## Checks

```sh
python3 -m unittest discover -s tests -v
bash -n build.sh
```

The tests check that reports reject missing rounds, changed route data and
missing isolated memory cases, and exclude warm-ups from timing summaries.

## License

Apache-2.0. See [`LICENSE`](./LICENSE).

The `proto/api/*.proto` files are vendored from [osrg/gobgp](https://github.com/osrg/gobgp)
and retain their upstream MIT-style copyright (Nippon Telegraph and Telephone
Corporation).
