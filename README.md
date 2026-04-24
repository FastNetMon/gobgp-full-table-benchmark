# gobgp_client_benchmark

A minimal C++ gRPC client for benchmarking [gobgpd](https://github.com/osrg/gobgp)'s
`ListPath` RPC — useful when you need to understand where time is spent serving
a full BGP table to an API consumer (e.g. FastNetMon pulling ADJ_IN from a
route collector).

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

## Build

The supported build path is a containerised build against Ubuntu 24.04's
packaged gRPC / Protobuf so you don't need a local toolchain:

```
./build.sh
# produces ./dist/gobgp_client_benchmark
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
# iterate 5 times, decode every NLRI + every attribute slice.
./build/gobgp_client_benchmark --target SERVER:50051 --peer x.x.x.x --iterations 5

# Non-only-binary setup: server still returns raw NLRI/attribute bytes, but
# doesn't take the enable_only_binary shortcut — exercises the mixed code path.
./build/gobgp_client_benchmark --target SERVER:50051 --peer x.x.x.x \
    --only-binary false --nlri-binary true --attr-binary true

# Measure with vs. without client-side decode. With --decode false the client
# receives the stream but never touches the per-path byte buffers, so in_proc
# drops to almost zero and total ~= in_recv. Useful for isolating server cost.
./build/gobgp_client_benchmark --target SERVER:50051 --peer x.x.x.x --decode false

# Global RIB (the router's own post-policy table). --peer is not required
# because a global lookup isn't peer-scoped.
./build/gobgp_client_benchmark --target SERVER:50051 --table global

# Best-path-only per destination: for each prefix, process just paths(0)
# instead of every path in the ECMP / multipath set. Approximates what a
# consumer that only cares about the installed route pays.
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
| `--max-recv-mb N` | `1024` | Max gRPC receive message size (a full table easily exceeds the default). |

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

## License

Apache-2.0. See [`LICENSE`](./LICENSE).

The `proto/api/*.proto` files are vendored from [osrg/gobgp](https://github.com/osrg/gobgp)
and retain their upstream MIT-style copyright (Nippon Telegraph and Telephone
Corporation).
