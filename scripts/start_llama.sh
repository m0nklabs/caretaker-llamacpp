#!/bin/bash
# Caretaker-owned llama-server launcher (systemd ExecStart for llama-server.service).
#
# Replaces the legacy guardian-v1 launcher (/home/flip/llama_cpp_guardian/scripts/
# start_llama.sh) so the backend launch path no longer depends on the frozen
# legacy directory (F7 rule). The caretaker writes the deployment files into its
# OWN config dir (caretaker/paths.py: CURRENT_MODEL_ARGS_FILE / ENV_FILE /
# SIG_FILE); this script reads exactly those files, so the model the caretaker
# (re)launches is always the model it was asked to serve.
#
# Mirrors caretaker/paths.py env handling:
#   ROOT_DIR    <- CARETAKER_ROOT            (default: this script's repo)
#   CONFIG_DIR  <- CARETAKER_CONFIG_DIR      (default: <ROOT_DIR>/config)
#   SLOTS_DIR   <- CARETAKER_LLAMA_SLOTS_DIR -> GUARDIAN_LLMPROVIDER_GATEWAY_SLOTS_DIR
#                  -> ~/llama_slots
#   BINARY      <- LLAMA_SERVER_BINARY       (set by the unit drop-ins)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="${CARETAKER_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
CONFIG_DIR="${CARETAKER_CONFIG_DIR:-$ROOT_DIR/config}"
OFFICIAL_ROOT="${LLAMA_CPP_OFFICIAL_ROOT:-$ROOT_DIR/../llama_cpp_official}"
MODELS_DIR="${MODELS_DIR:-$ROOT_DIR/../models}"
SLOTS_DIR="${CARETAKER_LLAMA_SLOTS_DIR:-${GUARDIAN_LLMPROVIDER_GATEWAY_SLOTS_DIR:-$HOME/llama_slots}}"

CONFIG_FILE="$CONFIG_DIR/current_model.args"
ENV_FILE="$CONFIG_DIR/current_model.env"

# Source only CUDA_VISIBLE_DEVICES from the optional per-model env file.
# SECURITY: never `source` the file wholesale — it could set/export arbitrary
# env (PATH hijack, LD_PRELOAD, etc.). Extract just the one known-safe key.
if [ -f "$ENV_FILE" ]; then
    echo "Reading CUDA_VISIBLE_DEVICES from: $ENV_FILE"
    CUDA_VISIBLE_DEVICES=$(grep -E '^export CUDA_VISIBLE_DEVICES=' "$ENV_FILE" | cut -d= -f2- | tr -d '"')
    if [ -n "$CUDA_VISIBLE_DEVICES" ]; then
        export CUDA_VISIBLE_DEVICES
        echo "CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"
    fi
fi

# Keep llama CUDA ordinals stable across reboots; v2 telemetry maps nvidia-smi
# readings into this same PCI-bus order.
export CUDA_DEVICE_ORDER="${CUDA_DEVICE_ORDER:-PCI_BUS_ID}"
echo "CUDA_DEVICE_ORDER=$CUDA_DEVICE_ORDER"

# Default binary: official llama.cpp build next to the models/ checkout.
DEFAULT_BINARY="$OFFICIAL_ROOT/build/bin/llama-server"
BINARY="${LLAMA_SERVER_BINARY:-$DEFAULT_BINARY}"

# Default fallback if config missing. SECURITY: must point at a model that EXISTS
# in $MODELS_DIR so a missing current_model.args degrades gracefully instead of
# crashing llama-server on a nonexistent file. Maps to alias qwen3.6-35b-uncensored.
DEFAULT_MODEL="$MODELS_DIR/Qwen3.6-35B-A3B-Uncensored-Aggressive.i1-Q4_K_M.gguf"
ARGS="-m $DEFAULT_MODEL -c 262144 -ngl 99 -ctk q4_0 -ctv q4_0 --host 127.0.0.1 --port 11440 --slot-save-path $SLOTS_DIR --no-mmap --tensor-split 0.57,0.43 -nkvo --parallel 4"

if [ -f "$CONFIG_FILE" ]; then
    # Read args from file (expecting single line)
    IFS= read -r ARGS < "$CONFIG_FILE"
    echo "Starting Llama Server with dynamic args: $ARGS"
else
    echo "Config file not found, using default: $ARGS"
fi

echo "Using official llama.cpp binary: $BINARY"

# Verify binary exists
if [ ! -x "$BINARY" ]; then
    echo "ERROR: Binary not found or not executable: $BINARY"
    echo "Falling back to default: $DEFAULT_BINARY"
    BINARY="$DEFAULT_BINARY"
fi

# Need to run llama-server explicitly
$BINARY $ARGS
