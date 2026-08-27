#!/bin/bash
# Builds sony-rec from SonyRec.cpp, linking against the CrSDK runtime already
# vendored in fx30_bridge/ (libCr_Core.dylib + CrAdapter/*.dylib).
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
BRIDGE_DIR="$(cd "$SRC_DIR/.." && pwd)"
OUT="$BRIDGE_DIR/sony-rec"

CLANGXX="/Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang++"
SDKROOT="$(xcrun --show-sdk-path)"

"$CLANGXX" \
  -isysroot "$SDKROOT" \
  -std=c++17 \
  -arch arm64 \
  -O2 \
  -I "$SRC_DIR" \
  -o "$OUT.new" \
  "$SRC_DIR/SonyRec.cpp" \
  -L "$BRIDGE_DIR" -lCr_Core \
  -Wl,-rpath,@executable_path \
  -Wl,-rpath,@executable_path/CrAdapter

echo "Built: $OUT.new"
file "$OUT.new"
