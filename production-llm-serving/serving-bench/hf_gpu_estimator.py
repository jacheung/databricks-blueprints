#!/usr/bin/env python3
"""Estimate inference VRAM for a Hugging Face model and match GPU shapes."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


GIB = 1024**3
GPU_SHAPES = {
    "1x T4 16GB": (1, 16),
    "1x A10G 24GB": (1, 24),
    "4x A10G 24GB": (4, 24),
    "1x H100 80GB": (1, 80),
    "2x H100 80GB": (2, 80),
    "4x H100 80GB": (4, 80),
    "8x H100 80GB": (8, 80),
}


def fetch_json(url: str, token: str | None) -> dict:
    headers = {"User-Agent": "hf-gpu-estimator/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Hugging Face returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Could not reach Hugging Face: {error.reason}") from error


def model_metadata(model_id: str, revision: str, token: str | None) -> tuple[dict, dict]:
    quoted_model = urllib.parse.quote(model_id, safe="/")
    quoted_revision = urllib.parse.quote(revision, safe="")
    api_url = (
        f"https://huggingface.co/api/models/{quoted_model}/revision/"
        f"{quoted_revision}?blobs=true"
    )
    config_url = (
        f"https://huggingface.co/{quoted_model}/resolve/{quoted_revision}/config.json"
    )
    return fetch_json(api_url, token), fetch_json(config_url, token)


def weight_bytes(metadata: dict) -> tuple[int, list[str]]:
    siblings = metadata.get("siblings", [])
    preferred = [
        sibling for sibling in siblings
        if sibling.get("rfilename", "").endswith(".safetensors")
        and sibling.get("rfilename") != "model.safetensors.index.json"
    ]
    if not preferred:
        preferred = [
            sibling for sibling in siblings
            if sibling.get("rfilename", "").endswith((".bin", ".pt"))
            and "optimizer" not in sibling.get("rfilename", "").lower()
        ]

    files = [sibling["rfilename"] for sibling in preferred if sibling.get("size")]
    total = sum(int(sibling["size"]) for sibling in preferred if sibling.get("size"))
    if not total:
        raise RuntimeError(
            "No sized safetensors/bin weight files found. Specify a revision containing "
            "standard Hugging Face weights."
        )
    return total, files


def kv_cache_bytes(config: dict, context: int, concurrency: int, kv_bytes: int) -> int:
    layers = config.get("num_hidden_layers") or config.get("n_layer")
    attention_heads = config.get("num_attention_heads") or config.get("n_head")
    kv_heads = config.get("num_key_value_heads") or attention_heads
    hidden_size = config.get("hidden_size") or config.get("n_embd")
    head_dim = config.get("head_dim")
    if head_dim is None and hidden_size and attention_heads:
        head_dim = hidden_size // attention_heads
    missing = [
        name for name, value in (
            ("layers", layers), ("KV heads", kv_heads), ("head dimension", head_dim)
        ) if value is None
    ]
    if missing:
        raise RuntimeError(f"config.json is missing values needed for KV cache: {', '.join(missing)}")
    return 2 * layers * kv_heads * head_dim * context * concurrency * kv_bytes


def classify(required: int, gpu_count: int, gpu_gib: int, usable_fraction: float) -> dict:
    capacity = gpu_count * gpu_gib * GIB * usable_fraction
    utilization = required / capacity
    if utilization <= 0.75:
        status = "recommended"
    elif utilization <= 0.85:
        status = "likely"
    elif utilization <= 1.0:
        status = "marginal"
    else:
        status = "unsupported"
    return {
        "status": status,
        "usable_vram_gib": round(capacity / GIB, 1),
        "utilization_percent": round(utilization * 100, 1),
    }


def estimate(args: argparse.Namespace) -> dict:
    token = args.token or os.getenv("HF_TOKEN")
    metadata, config = model_metadata(args.model, args.revision, token)
    weights, files = weight_bytes(metadata)
    kv_cache = kv_cache_bytes(config, args.context, args.concurrency, args.kv_bytes)
    runtime = int(weights * args.runtime_overhead)
    subtotal = weights + kv_cache + runtime
    required = int(subtotal * (1 + args.safety_margin))

    shapes = {
        name: classify(required, count, memory, args.usable_fraction)
        for name, (count, memory) in GPU_SHAPES.items()
    }
    return {
        "model": args.model,
        "revision": args.revision,
        "assumptions": {
            "context_tokens": args.context,
            "concurrent_sequences": args.concurrency,
            "kv_cache_bytes_per_element": args.kv_bytes,
            "runtime_overhead_percent": args.runtime_overhead * 100,
            "safety_margin_percent": args.safety_margin * 100,
            "usable_gpu_memory_percent": args.usable_fraction * 100,
        },
        "memory_gib": {
            "weights": round(weights / GIB, 2),
            "kv_cache": round(kv_cache / GIB, 2),
            "runtime_overhead": round(runtime / GIB, 2),
            "required_total": round(required / GIB, 2),
        },
        "weight_files": files,
        "gpu_shapes": shapes,
        "note": "Memory fit only; confirm architecture, quantization, and tensor-parallel support.",
    }


def print_text(result: dict) -> None:
    memory = result["memory_gib"]
    print(f"Model: {result['model']} @ {result['revision']}")
    print(
        f"Estimated VRAM: {memory['required_total']:.2f} GiB "
        f"(weights {memory['weights']:.2f}, KV {memory['kv_cache']:.2f}, "
        f"runtime {memory['runtime_overhead']:.2f})"
    )
    print()
    for name, fit in result["gpu_shapes"].items():
        print(
            f"{name:<18} {fit['status']:<11} "
            f"({fit['utilization_percent']:.1f}% of usable VRAM)"
        )
    print(f"\nNote: {result['note']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", help="Hugging Face model ID, e.g. meta-llama/Llama-3.1-70B-Instruct")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--context", type=int, default=8192, help="Max tokens per sequence")
    parser.add_argument("--concurrency", type=int, default=4, help="Simultaneous sequences")
    parser.add_argument("--kv-bytes", type=int, choices=(1, 2, 4), default=2, help="KV dtype bytes")
    parser.add_argument("--runtime-overhead", type=float, default=0.20)
    parser.add_argument("--safety-margin", type=float, default=0.15)
    parser.add_argument("--usable-fraction", type=float, default=0.90)
    parser.add_argument("--token", help="HF token; prefer the HF_TOKEN environment variable")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args()
    for name in ("context", "concurrency", "kv_bytes"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    for name in ("runtime_overhead", "safety_margin", "usable_fraction"):
        value = getattr(args, name)
        if not 0 < value <= 1:
            parser.error(f"--{name.replace('_', '-')} must be greater than 0 and at most 1")
    return args


def main() -> int:
    args = parse_args()
    try:
        result = estimate(args)
    except RuntimeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print_text(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
