// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 FastNetMon Ltd.
//
// gobgp_client_benchmark
//
// Minimal C++ gRPC client for benchmarking gobgpd's ListPath RPC against a
// specific peer's ADJ_IN table. Measures ttfb, time blocked in Recv, time
// processing between Recvs, and total wall time. Optionally decodes NLRI
// and iterates attribute slices to include client-side decode cost.
//
// Defaults mirror the "fully binary" path (enable_only_binary=true) so the
// server skips its native→proto attribute type-switch. Flip --binary=false to
// measure the non-binary path for comparison.
//
// Build:
//   cd client_benchmark && cmake -B build -S . && cmake --build build -j
// Run:
//   ./build/gobgp_client_benchmark --target 127.0.0.1:50051 --peer 10.0.0.1 --iterations 5

#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <getopt.h>
#include <iostream>
#include <memory>
#include <string>

#include <grpcpp/grpcpp.h>

#include "api/gobgp.pb.h"
#include "api/gobgp.grpc.pb.h"

using clock_t_ = std::chrono::steady_clock;

namespace {

struct Options {
    std::string target       = "127.0.0.1:50051";
    std::string peer;                 // required for ADJ_IN / ADJ_OUT / LOCAL
    std::string table        = "adj-in";  // adj-in | adj-out | global | local
    int         iterations   = 3;
    bool        only_binary  = true;
    bool        nlri_binary  = true;
    bool        attr_binary  = true;
    bool        decode       = true;  // iterate attrs + touch NLRI bytes per path
    bool        best_only    = false; // only process paths(0)
    int         deadline_s   = 120;
    int32_t     max_recv_mb  = 1024;  // gRPC default is small; set generously
};

void usage(const char* argv0) {
    std::fprintf(stderr,
        "usage: %s [options]\n"
        "  --target HOST:PORT     gRPC address of gobgpd (default 127.0.0.1:50051)\n"
        "  --peer IP              peer address (required for adj-in / adj-out / local)\n"
        "  --table NAME           adj-in | adj-out | global | local (default adj-in)\n"
        "  --iterations N         number of back-to-back runs (default 3)\n"
        "  --binary BOOL          set enable_only_binary + _nlri_ + _attribute_ (default true)\n"
        "  --only-binary BOOL     only enable_only_binary (default true)\n"
        "  --nlri-binary BOOL     only enable_nlri_binary (default true)\n"
        "  --attr-binary BOOL     only enable_attribute_binary (default true)\n"
        "  --decode BOOL          scan nlri_binary + each pattrs_binary (default true)\n"
        "  --best-only BOOL       process only paths(0) per destination (default false)\n"
        "  --deadline SEC         per-iteration deadline (default 120)\n"
        "  --max-recv-mb N        max gRPC recv message size in MiB (default 1024)\n",
        argv0);
}

bool parse_bool(const std::string& s, bool& out) {
    if (s == "true" || s == "1" || s == "yes")  { out = true;  return true; }
    if (s == "false" || s == "0" || s == "no")  { out = false; return true; }
    return false;
}

bool parse_args(int argc, char** argv, Options& o) {
    static const option long_opts[] = {
        {"target",       required_argument, nullptr, 't'},
        {"peer",         required_argument, nullptr, 'p'},
        {"table",        required_argument, nullptr, 'T'},
        {"iterations",   required_argument, nullptr, 'n'},
        {"binary",       required_argument, nullptr, 'b'},
        {"only-binary",  required_argument, nullptr, 'O'},
        {"nlri-binary",  required_argument, nullptr, 'N'},
        {"attr-binary",  required_argument, nullptr, 'A'},
        {"decode",       required_argument, nullptr, 'd'},
        {"best-only",    required_argument, nullptr, 'B'},
        {"deadline",     required_argument, nullptr, 'D'},
        {"max-recv-mb",  required_argument, nullptr, 'M'},
        {"help",         no_argument,       nullptr, 'h'},
        {nullptr, 0, nullptr, 0},
    };
    int c;
    while ((c = getopt_long(argc, argv, "", long_opts, nullptr)) != -1) {
        bool b;
        switch (c) {
            case 't': o.target = optarg; break;
            case 'p': o.peer   = optarg; break;
            case 'T': o.table  = optarg; break;
            case 'n': o.iterations  = std::atoi(optarg); break;
            case 'b':
                if (!parse_bool(optarg, b)) { usage(argv[0]); return false; }
                o.only_binary = o.nlri_binary = o.attr_binary = b;
                break;
            case 'O': if (!parse_bool(optarg, o.only_binary)) { usage(argv[0]); return false; } break;
            case 'N': if (!parse_bool(optarg, o.nlri_binary)) { usage(argv[0]); return false; } break;
            case 'A': if (!parse_bool(optarg, o.attr_binary)) { usage(argv[0]); return false; } break;
            case 'd': if (!parse_bool(optarg, o.decode))      { usage(argv[0]); return false; } break;
            case 'B': if (!parse_bool(optarg, o.best_only))   { usage(argv[0]); return false; } break;
            case 'D': o.deadline_s  = std::atoi(optarg); break;
            case 'M': o.max_recv_mb = std::atoi(optarg); break;
            case 'h': usage(argv[0]); std::exit(0);
            default:  usage(argv[0]); return false;
        }
    }
    if (o.table != "global" && o.peer.empty()) {
        std::fprintf(stderr, "--peer is required when --table is %s\n", o.table.c_str());
        return false;
    }
    return true;
}

api::TableType table_enum(const std::string& s) {
    if (s == "global")  return api::TableType::TABLE_TYPE_GLOBAL;
    if (s == "local")   return api::TableType::TABLE_TYPE_LOCAL;
    if (s == "adj-in")  return api::TableType::TABLE_TYPE_ADJ_IN;
    if (s == "adj-out") return api::TableType::TABLE_TYPE_ADJ_OUT;
    std::fprintf(stderr, "unknown --table %s\n", s.c_str());
    std::exit(2);
}

// Minimal "touch" to force the kernel / protobuf layer to hand us the bytes —
// stops the compiler from optimizing them away, and approximates what a real
// decoder pays per path.
volatile uint64_t sink = 0;
inline void touch(const std::string& b) {
    const auto* p = reinterpret_cast<const uint8_t*>(b.data());
    uint64_t s = 0;
    for (size_t i = 0; i < b.size(); ++i) s += p[i];
    sink += s;
}

struct Counters {
    uint64_t dests       = 0;
    uint64_t paths       = 0;
    uint64_t decoded     = 0;
    uint64_t nlri_bytes  = 0;
    uint64_t attr_bytes  = 0;
};

struct Timing {
    std::chrono::nanoseconds ttfb {0};
    std::chrono::nanoseconds in_recv {0};
    std::chrono::nanoseconds in_proc {0};
    std::chrono::nanoseconds total {0};
};

bool run_once(api::GoBgpService::Stub& stub,
              const Options& o,
              Counters& c,
              Timing& t) {
    grpc::ClientContext ctx;
    ctx.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(o.deadline_s));

    auto* fam = new api::Family;
    fam->set_afi(api::Family::AFI_IP);
    fam->set_safi(api::Family::SAFI_UNICAST);

    api::ListPathRequest req;
    req.set_table_type(table_enum(o.table));
    req.set_allocated_family(fam);
    if (!o.peer.empty()) req.set_name(o.peer);
    req.set_enable_only_binary(o.only_binary);
    req.set_enable_nlri_binary(o.nlri_binary);
    req.set_enable_attribute_binary(o.attr_binary);

    auto t0 = clock_t_::now();
    auto reader = stub.ListPath(&ctx, req);

    api::ListPathResponse resp;
    bool first = true;
    std::chrono::nanoseconds recv_acc{0}, proc_acc{0};

    while (true) {
        auto r0 = clock_t_::now();
        bool ok = reader->Read(&resp);
        auto r1 = clock_t_::now();
        recv_acc += r1 - r0;
        if (!ok) break;
        if (first) { t.ttfb = r1 - t0; first = false; }

        const auto& d = resp.destination();
        c.dests++;
        int paths = d.paths_size();
        c.paths += paths;

        int limit = o.best_only ? std::min(1, paths) : paths;
        if (o.decode) {
            for (int i = 0; i < limit; ++i) {
                const auto& p = d.paths(i);
                if (!p.nlri_binary().empty()) {
                    c.nlri_bytes += p.nlri_binary().size();
                    touch(p.nlri_binary());
                }
                int n = p.pattrs_binary_size();
                for (int k = 0; k < n; ++k) {
                    c.attr_bytes += p.pattrs_binary(k).size();
                    touch(p.pattrs_binary(k));
                }
                c.decoded++;
            }
        }
        auto r2 = clock_t_::now();
        proc_acc += r2 - r1;
    }

    auto status = reader->Finish();
    auto t1 = clock_t_::now();
    t.in_recv = recv_acc;
    t.in_proc = proc_acc;
    t.total   = t1 - t0;

    if (!status.ok()) {
        std::fprintf(stderr, "ListPath failed: code=%d msg=%s\n",
                     status.error_code(), status.error_message().c_str());
        return false;
    }
    return true;
}

double ms(std::chrono::nanoseconds ns) {
    return std::chrono::duration<double, std::milli>(ns).count();
}

} // namespace

int main(int argc, char** argv) {
    Options o;
    if (!parse_args(argc, argv, o)) return 2;

    grpc::ChannelArguments chan_args;
    chan_args.SetMaxReceiveMessageSize(o.max_recv_mb * 1024 * 1024);

    auto channel = grpc::CreateCustomChannel(
        o.target, grpc::InsecureChannelCredentials(), chan_args);
    auto stub = api::GoBgpService::NewStub(channel);

    std::fprintf(stderr,
        "target=%s table=%s peer=%s iterations=%d "
        "only_binary=%d nlri_binary=%d attr_binary=%d decode=%d best_only=%d\n",
        o.target.c_str(), o.table.c_str(),
        o.peer.empty() ? "-" : o.peer.c_str(),
        o.iterations,
        o.only_binary, o.nlri_binary, o.attr_binary, o.decode, o.best_only);

    for (int i = 1; i <= o.iterations; ++i) {
        Counters c; Timing t;
        if (!run_once(*stub, o, c, t)) return 1;
        std::printf(
            "run=%d dests=%llu paths=%llu decoded=%llu "
            "nlri_bytes=%llu attr_bytes=%llu "
            "ttfb_ms=%.1f in_recv_ms=%.1f in_proc_ms=%.1f total_ms=%.1f\n",
            i,
            (unsigned long long)c.dests, (unsigned long long)c.paths,
            (unsigned long long)c.decoded,
            (unsigned long long)c.nlri_bytes, (unsigned long long)c.attr_bytes,
            ms(t.ttfb), ms(t.in_recv), ms(t.in_proc), ms(t.total));
        std::fflush(stdout);
    }

    // sink is volatile; referenced so the touch() loop isn't optimized away.
    std::fprintf(stderr, "(sink=%llu)\n", (unsigned long long)sink);
    return 0;
}
