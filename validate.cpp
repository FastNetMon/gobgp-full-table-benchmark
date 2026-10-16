// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 FastNetMon Ltd.
// Separate correctness pass: do not include this work in benchmark timings.
#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <iomanip>
#include <iostream>
#include <string>
#include <vector>
#include <openssl/sha.h>
#include <grpcpp/grpcpp.h>
#include "api/gobgp.grpc.pb.h"

namespace {
void append(std::string& out, const std::string& bytes) {
    const uint32_t size = bytes.size();
    for (int shift = 24; shift >= 0; shift -= 8) out.push_back((size >> shift) & 0xff);
    out += bytes;
}
}

int main(int argc, char** argv) {
    if (argc != 5) {
        std::cerr << "usage: " << argv[0] << " HOST:PORT PEER_OR_DASH TABLE DEADLINE_SECONDS\n";
        return 2;
    }
    api::TableType table;
    const std::string name = argv[3];
    if (name == "adj-in") table = api::TABLE_TYPE_ADJ_IN;
    else if (name == "adj-out") table = api::TABLE_TYPE_ADJ_OUT;
    else if (name == "global") table = api::TABLE_TYPE_GLOBAL;
    else if (name == "local") table = api::TABLE_TYPE_LOCAL;
    else { std::cerr << "unknown table\n"; return 2; }
    int deadline;
    try { deadline = std::stoi(argv[4]); }
    catch (const std::exception&) { return 2; }
    if (deadline <= 0 || (name != "global" && std::string(argv[2]) == "-")) return 2;

    grpc::ChannelArguments options;
    options.SetMaxReceiveMessageSize(1024 * 1024 * 1024);
    auto channel = grpc::CreateCustomChannel(argv[1], grpc::InsecureChannelCredentials(), options);
    auto stub = api::GoBgpService::NewStub(channel);
    grpc::ClientContext context;
    context.set_deadline(std::chrono::system_clock::now() + std::chrono::seconds(deadline));
    api::ListPathRequest request;
    request.set_table_type(table);
    if (std::string(argv[2]) != "-") request.set_name(argv[2]);
    request.mutable_family()->set_afi(api::Family::AFI_IP);
    request.mutable_family()->set_safi(api::Family::SAFI_UNICAST);
    request.set_enable_only_binary(true);
    request.set_enable_nlri_binary(true);
    request.set_enable_attribute_binary(true);
    auto stream = stub->ListPath(&context, request);
    api::ListPathResponse response;
    std::vector<std::array<unsigned char, SHA256_DIGEST_LENGTH>> hashes;
    uint64_t destinations = 0, nlri_bytes = 0, attribute_bytes = 0;
    while (stream->Read(&response)) {
        ++destinations;
        for (const auto& path : response.destination().paths()) {
            if (path.nlri_binary().empty()) {
                std::cerr << "path missing binary NLRI\n";
                return 1;
            }
            std::vector<std::string> attrs(path.pattrs_binary().begin(), path.pattrs_binary().end());
            // Preserve duplicate paths/attributes, normalize only their order.
            std::sort(attrs.begin(), attrs.end());
            std::string canonical;
            append(canonical, path.nlri_binary());
            nlri_bytes += path.nlri_binary().size();
            for (const auto& attr : attrs) {
                append(canonical, attr);
                attribute_bytes += attr.size();
            }
            std::array<unsigned char, SHA256_DIGEST_LENGTH> digest;
            SHA256(reinterpret_cast<const unsigned char*>(canonical.data()), canonical.size(), digest.data());
            hashes.push_back(digest);
        }
    }
    const auto status = stream->Finish();
    if (!status.ok()) { std::cerr << status.error_message() << '\n'; return 1; }
    std::sort(hashes.begin(), hashes.end());
    std::array<unsigned char, SHA256_DIGEST_LENGTH> digest;
    SHA256(reinterpret_cast<const unsigned char*>(hashes.data()), hashes.size() * SHA256_DIGEST_LENGTH, digest.data());
    std::cout << "{\"destinations\":" << destinations << ",\"paths\":" << hashes.size()
              << ",\"nlri_bytes\":" << nlri_bytes << ",\"attribute_bytes\":" << attribute_bytes << ",\"sha256\":\"";
    for (auto byte : digest) std::cout << std::hex << std::setw(2) << std::setfill('0') << unsigned(byte);
    std::cout << "\"}\n";
}
