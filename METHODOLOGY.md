# Full-table API retrieval methodology

This benchmark measures the cost of retrieving a complete IPv4 table through
the GoBGP-compatible `ListPath` API. GoBGP and RustyBGP can use the same client.
It is useful for monitoring, route inspection, route collectors and controllers
that need an initial routing snapshot. Large dumps also expose excessive
buffering, memory spikes and problems with slow consumers.

These results do **not** measure BGP convergence, route propagation latency,
update throughput or route-map/policy evaluation performance. Those need
separate workloads and measurements. Do not describe lower API retrieval
time as better overall routing performance.

## Prepare a reproducible comparison

1. Build every daemon at an explicit commit. Record compiler versions, flags,
   dependency locks and any local changes. For before/after comparisons, use
   the same compiler, dependency lock, build profile and runtime image. Avoid
   profiling or allocator changes in performance runs.
2. Supply a GoBGP source daemon and its compatible `gobgp` CLI. Targets may be
   different GoBGP versions or RustyBGP. Binaries must run in the client image
   (Ubuntu 24.04 by default); RustyBGP binaries built on a newer host may need
   rebuilding against this runtime's glibc. The runner hashes all binaries and
   captures target `--version` output. Declared source revisions are not proof
   that the binaries were built from clean checkouts.
3. Pin the compressed MRT snapshot by SHA-256. The example uses RouteViews
   `rib.20260808.0000.bz2`, SHA-256
   `03bf1ea39809789576786c61c7ff77d8e1977d04706af1457aaafaf7cb0e95fb`.
   Download it from
   [the RouteViews archive](https://archive.routeviews.org/bgpdata/2026.08/RIBS/rib.20260808.0000.bz2).
4. Copy `examples/comparison.json` and set file paths, labels and revisions.
   Relative file paths are resolved against the config file's directory.
   `build_info` holds compiler versions, flags and lockfile hashes. Configure
   an unused IPv4 Docker subnet; it affects the rewritten next hop and thus
   the route-data checksum. Keep it fixed across comparable runs.
5. Build the client image with `./build.sh`. Run on a quiet Linux host with a
   **local Docker daemon** and access to its PIDs in `/proc`. Docker Desktop,
   a remote Docker daemon and incompatible API/protobuf versions are outside
   the supported setup. Do not enable FIB installation or other kernel work
   in only one variant. Record any relevant daemon defaults or extra flags.

The supplied three-version config pins the versions used in the 16 October
2026 comparison; the labels do not track a moving upstream "latest". To test
the bounded-stream fix, add a RustyBGP variant at `abab8315b581dafe94f07ab0f07e8cb105004dcb`
alongside the baseline at `603d350281a5aec03a068ddecfd5fc58f8fe66c5`.
Build both with identical settings and Cargo.lock. For example, build GoBGP's
`./cmd/gobgpd` and `./cmd/gobgp` with `CGO_ENABLED=0 go build -trimpath`, and
RustyBGP with `cargo build --locked --release -p rustybgpd` in a compatible
builder. Keep the build outputs and provenance with the run.

## Fixed input and route parity

The runner creates a private Docker bridge, without published host ports or
privileged containers. The source uses AS 65001; each target uses AS 65002.
It loads the MRT file into the source **before starting any target**, using
`mrt inject global --only-best --no-ipv6 --nexthop SOURCE_ADDRESS`.
This avoids differences caused by replaying a changing table into live peers.

Wait for each target's received and accepted path counts to match the expected
table, then allow a fixed settling period (12 seconds by default). The fixture
above produces 1,115,966 destinations and paths, 4,448,247 binary NLRI bytes
and 56,476,851 binary attribute bytes in our setup. Other inputs require their
own expected counts. The readiness wait is setup, not a convergence benchmark.

A separate validator requests fully binary output and computes a canonical
SHA-256 over all paths. Each path contains length-prefixed NLRI and sorted
top-level binary attributes; sorted path digests remove stream-order effects.
Duplicate paths and attributes remain significant. Equal counts alone are
insufficient: hashes and byte totals must match across versions and before
and after the timing workload. Every fresh-process memory case is validated
after its measurement window. Any mismatch aborts the run.

This validates route data in binary output. It excludes timestamps, source
metadata, destination grouping and attribute-internal ordering differences.
It does not prove that structured responses are semantically equivalent.
Raw per-version sample responses are retained to help inspect metadata.
The timed client's additive byte-touch `sink` is not a correctness hash.

## API modes and timing

| Mode | only-binary | nlri-binary | attr-binary |
| --- | --- | --- | --- |
| Structured | false | false | false |
| Structured + binary attributes | false | false | true |
| Fully binary | true | true | true |

Each round covers every version/mode pair. Requests run sequentially in a
seeded shuffled order, with one client process per request. The default is
one excluded warm-up round and five measured rounds; report the median and
min/max, retaining every raw measurement. The deadline is 300 seconds.
Do not silently drop failed, incomplete or slow requests.

The client always requests IPv4 unicast Adj-RIB-In for the same source peer,
with all paths and `decode=true`. This option scans binary buffers to prevent
the compiler removing that work. It **does not fully decode BGP NLRI or
attributes**. Structured output has no binary buffers to scan, although gRPC
still parses its protobuf messages.

Retain total stream time, time to first response, time inside `Read()`, time
processing between reads, destination/path counts and binary byte totals.
`Read()` includes server work, transport and client protobuf parsing; it is
not pure server CPU time. Total also includes stream creation/completion,
so it need not equal the sum of receive and processing time.

Server CPU is the target process's user+system CPU delta around the client
command, read from `/proc/PID/stat`. It includes background activity and
command startup/teardown time. It is not a profile of the API handler.
Host kernel, CPU count, RAM and load averages are recorded with the run.

## Memory: keep the two scopes separate

### Fresh-process, one-mode measurements

For each version/mode pair, start a new target, load the table, wait for the
settling period, then issue exactly one request. Do not run a full-table
validation or warm-up first. Capture:

- RSS after loading, immediately after the request and after 15 seconds idle.
- Process lifetime peak RSS (`VmHWM`) before later correctness validation.
- RSS samples every 25 ms, plus the largest sampled value.

These are single observations, not medians. `VmHWM` includes route loading
as well as the request, and sampled RSS can miss short peaks. RSS is a physical
process footprint, not live Rust/Go allocations; allocator retention matters.
The CSV retains both VmHWM and the sampled maximum rather than substituting
one for the other. The timeline starts at client **command** launch and marks
command completion, including Docker/client startup overhead outside RPC time.

### Combined-workload lifetime peak

Also retain loaded RSS, RSS after the steady workload, and lifetime peak RSS
for each daemon. This scope includes loading, correctness checks, warm-ups
and all measured API modes. **Never present it as a binary-only peak** or pair
it with binary-only latency without explaining the different scopes. The
original 11.41 GiB RustyBGP peak belonged to this combined workload.

## Run and report

```sh
python3 scripts/compare.py examples/comparison.json --output runs/comparison --check
python3 scripts/compare.py examples/comparison.json --output runs/comparison
python3 -m venv .venv
.venv/bin/pip install -r requirements-report.txt
.venv/bin/python scripts/report.py runs/comparison
```

The runner uses only the Python standard library. The report requires
Matplotlib. The output directory must be new; failed/interrupted runs retain
diagnostics with `status=failed`. The report refuses incomplete rounds,
missing expected memory cases and route-data mismatches. Containers and the
bridge are cleaned up on normal completion or handled exceptions. After a
hard kill, use the `listpath-run` label/run-metadata network name to identify
only this run's remaining resources.

Outputs include raw client/daemon logs, route hashes, input/configuration,
runtime image ID, binary hashes, host details, JSON/CSV timings and memory
samples. `report/` contains:

- `REPORT.md`: timing, CPU, both memory scopes, correctness and revisions.
- `summary.json/csv`: medians and min/max; `memory.csv`: isolated footprints.
- `comparison.json/csv`: pairwise percentage changes, using each earlier
  configured variant as the baseline (negative means a reduction).
- `overview.png/svg`: latency and isolated peak memory. For older bundles
  without isolated measurements, the memory panel explicitly shows the
  combined workload peak instead.
- `timing-cpu.png/svg`, `client-timing.png/svg`: timing breakdown and CPU.
- `memory-timeline.png/svg`: fresh-process RSS traces when available.

The report also accepts our original 16 October comparison and bounded-stream
run directories, allowing their saved measurements to be plotted again. It
does not rewrite their raw data or invent missing isolated memory results.

Before publishing, inspect the plots, include the sample counts and build
provenance, and discuss memory tradeoffs alongside latency. More repetitions,
multiple peers, slow consumers and concurrent dumps are useful separate
experiments; they should not be inferred from this single-peer sequential test.
