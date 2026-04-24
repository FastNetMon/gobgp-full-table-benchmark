# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 FastNetMon Ltd.

cmake_minimum_required(VERSION 3.16)
project(gobgp_client_benchmark CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE Release)
endif()

add_compile_options(-Wall -Wextra)

# --- dependencies ------------------------------------------------------------
# Expect system-installed protobuf + gRPC (e.g. Debian/Ubuntu:
#   apt install -y libgrpc++-dev libprotobuf-dev protobuf-compiler protobuf-compiler-grpc)
find_package(Protobuf REQUIRED)
find_package(Threads REQUIRED)

find_program(PROTOC_EXECUTABLE protoc REQUIRED)
find_program(GRPC_CPP_PLUGIN grpc_cpp_plugin REQUIRED)

# Link against gRPC via pkg-config when the CMake config isn't installed.
find_package(PkgConfig REQUIRED)
pkg_check_modules(GRPC REQUIRED grpc++ grpc protobuf)

# --- code-gen from ./proto/api/*.proto ---------------------------------------
# Proto files are copied in-tree so this directory is self-sufficient and can
# be built without any sibling files from the gobgp repo.
set(PROTO_SRC_DIR "${CMAKE_SOURCE_DIR}/proto")
set(PROTO_OUT_DIR "${CMAKE_BINARY_DIR}/gen")
file(MAKE_DIRECTORY "${PROTO_OUT_DIR}")

set(PROTO_FILES
    api/attribute.proto
    api/capability.proto
    api/common.proto
    api/extcom.proto
    api/gobgp.proto
    api/nlri.proto
)

set(GENERATED_SOURCES)
foreach(proto ${PROTO_FILES})
    get_filename_component(proto_name "${proto}" NAME_WE)
    get_filename_component(proto_dir  "${proto}" DIRECTORY)
    set(pb_cc      "${PROTO_OUT_DIR}/${proto_dir}/${proto_name}.pb.cc")
    set(pb_h       "${PROTO_OUT_DIR}/${proto_dir}/${proto_name}.pb.h")
    set(grpc_pb_cc "${PROTO_OUT_DIR}/${proto_dir}/${proto_name}.grpc.pb.cc")
    set(grpc_pb_h  "${PROTO_OUT_DIR}/${proto_dir}/${proto_name}.grpc.pb.h")

    add_custom_command(
        OUTPUT  "${pb_cc}" "${pb_h}" "${grpc_pb_cc}" "${grpc_pb_h}"
        COMMAND "${PROTOC_EXECUTABLE}"
                --cpp_out=${PROTO_OUT_DIR}
                --grpc_out=${PROTO_OUT_DIR}
                --plugin=protoc-gen-grpc=${GRPC_CPP_PLUGIN}
                -I ${PROTO_SRC_DIR}
                ${PROTO_SRC_DIR}/${proto}
        DEPENDS "${PROTO_SRC_DIR}/${proto}"
        COMMENT "protoc ${proto}"
        VERBATIM
    )
    list(APPEND GENERATED_SOURCES "${pb_cc}" "${grpc_pb_cc}")
endforeach()

# --- binary ------------------------------------------------------------------
add_executable(gobgp_client_benchmark
    main.cpp
    ${GENERATED_SOURCES}
)

target_include_directories(gobgp_client_benchmark PRIVATE
    "${PROTO_OUT_DIR}"
    ${GRPC_INCLUDE_DIRS}
    ${Protobuf_INCLUDE_DIRS}
)

target_link_directories(gobgp_client_benchmark PRIVATE
    ${GRPC_LIBRARY_DIRS}
)

target_link_libraries(gobgp_client_benchmark PRIVATE
    ${GRPC_LIBRARIES}
    ${Protobuf_LIBRARIES}
    Threads::Threads
)
