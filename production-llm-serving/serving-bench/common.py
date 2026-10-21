"""Helpers shared by the notebooks: config, payloads, readiness, model checks, readout."""

from __future__ import annotations

import statistics
import time

import yaml

def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if cfg["model"]["task"] not in ("embedding", "chat"):
        raise ValueError("model.task must be embedding or chat")
    for ep in cfg["endpoints"]:
        if ep["method"] not in ("pt", "ppt", "custom"):
            raise ValueError(f"{ep['name']}: method must be pt, ppt or custom")
    return cfg


def endpoint_label(ep: dict) -> str:
    if ep.get("label"):
        return ep["label"]
    if ep["method"] == "custom":
        return f"Custom GPU ({ep['workload_type']})"  # discover.py sets a label with the GPU name
    return {"pt": "PT", "ppt": "Pay-per-token"}[ep["method"]]


def request_payload(method: str, task: str, text: str, max_tokens: int = 128) -> dict:
    if task == "chat":
        return {
            "messages": [{"role": "user", "content": text}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "stream": False,
        }
    if method == "custom":
        return {"dataframe_records": [{"text": text}]}
    return {"input": [text]}


def parse_response(task: str, body: dict) -> dict:
    """Validate a 200 response body; return the fields worth recording."""
    try:
        if task == "chat":
            if not isinstance(body["choices"][0]["message"]["content"], str):
                raise TypeError
            usage = body.get("usage") or {}
            return {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            }
        vector = body["data"][0]["embedding"] if "data" in body else body["predictions"][0]
        if not vector:
            raise ValueError
        return {"dim": len(vector)}
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError(f"Unexpected {task} response: {str(body)[:200]}") from error


def wait_for_endpoint(w, name: str, timeout_minutes: int = 60, poll_interval: int = 30) -> bool:
    """Ready means READY *and* NOT_UPDATING; READY alone can still be mid config update."""
    from databricks.sdk.service.serving import EndpointStateConfigUpdate, EndpointStateReady

    start = time.time()
    while time.time() - start < timeout_minutes * 60:
        state = w.serving_endpoints.get(name=name).state
        if (
            state.ready == EndpointStateReady.READY
            and state.config_update == EndpointStateConfigUpdate.NOT_UPDATING
        ):
            print(f"  {name}: READY")
            return True
        if state.config_update == EndpointStateConfigUpdate.UPDATE_FAILED:
            print(f"  {name}: FAILED -- {state}")
            return False
        update = state.config_update.value if state.config_update else "-"
        print(f"  {name}: {state.ready.value} / {update} ({int(time.time() - start)}s)")
        time.sleep(poll_interval)
    print(f"  {name}: TIMEOUT after {timeout_minutes}m")
    return False


def readout(summary: list[dict], idle: list[dict]) -> list[dict]:
    """One row per endpoint: throughput plateau from the sweep, latency after the longest idle.

    Saturation = lowest concurrency reaching 90% of peak RPS; beyond it extra clients mostly
    add queueing. Levels with more than MAX_ERROR_RATE failed requests are excluded: their RPS
    counts only the requests that got through (e.g. under a rate limit), so it isn't capacity.
    """
    rows = []
    for endpoint in dict.fromkeys(r["endpoint"] for r in summary):
        all_levels = sorted((r for r in summary if r["endpoint"] == endpoint), key=lambda r: r["concurrency"])
        levels = [r for r in all_levels if error_rate(r) <= MAX_ERROR_RATE]
        excluded = [r for r in all_levels if error_rate(r) > MAX_ERROR_RATE]
        peak = max((r["rps"] for r in levels), default=None)
        sat = next((r for r in levels if r["rps"] >= 0.9 * peak), {}) if levels else {}
        probes = [r for r in idle if r["endpoint"] == endpoint]
        warm = [r["latency_ms"] for r in probes if r["idle_seconds"] == 0 and r["latency_ms"] is not None]
        longest = max((r["idle_seconds"] for r in probes), default=None)
        after = [r for r in probes if longest and r["idle_seconds"] == longest]
        after_ok = [r["latency_ms"] for r in after if r["latency_ms"] is not None]
        rows.append(
            {
                "endpoint": endpoint,
                "peak_rps": peak,
                "saturation_concurrency": sat.get("concurrency"),
                "p50_at_saturation_ms": sat.get("p50"),
                "p95_at_saturation_ms": sat.get("p95"),
                "errors": sum(r["errors"] for r in all_levels),
                "warm_ms": statistics.mean(warm) if warm else None,
                "longest_idle_s": longest,
                "after_idle_ms": statistics.mean(after_ok) if after_ok else None,
                "after_idle_std_ms": statistics.stdev(after_ok) if len(after_ok) > 1 else None,
                "idle_failures": len(after) - len(after_ok),
                "note": "; ".join(excluded_note(r) for r in excluded) or None,
            }
        )
    return rows


MAX_ERROR_RATE = 0.01


def error_rate(level: dict) -> float:
    return level["errors"] / level["requests"] if level.get("requests") else 0.0


def excluded_note(level: dict) -> str:
    note = f"C{level['concurrency']} excluded: {error_rate(level):.0%} errors"
    if level.get("rate_limited") and level["rate_limited"] >= level["errors"] / 2:
        note += " (rate-limited, HTTP 429)"
    return note


def fmt_rps(value) -> str:
    return "—" if value is None else f"{value:.1f}"


def readout_markdown(rows: list[dict]) -> str:
    def fmt(value, suffix=""):
        return "—" if value is None else f"{value:,.0f}{suffix}"

    lines = [
        "| Endpoint | Peak RPS | Saturation concurrency | P50 @ sat | P95 @ sat | Errors "
        "| Warm latency | Latency after longest idle | Std after longest idle | Notes |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for r in rows:
        idle = fmt(r["after_idle_ms"], " ms")
        if r["longest_idle_s"]:
            idle += f" ({r['longest_idle_s']}s, {r['idle_failures']} failed)"
        lines.append(
            f"| {r['endpoint']} | {fmt_rps(r['peak_rps'])} | {r['saturation_concurrency'] or '—'} "
            f"| {fmt(r['p50_at_saturation_ms'], ' ms')} | {fmt(r['p95_at_saturation_ms'], ' ms')} "
            f"| {r['errors']} | {fmt(r['warm_ms'], ' ms')} | {idle} | {fmt(r['after_idle_std_ms'], ' ms')} | {r.get('note') or ''} |"
        )
    return "\n".join(lines)


def requests_for_level(load_test: dict, concurrency: int) -> int:
    """Enough requests that high-concurrency windows aren't a fraction of a second."""
    return max(load_test["requests_per_level"], load_test.get("requests_per_client", 0) * concurrency)


def endpoint_settings(ep: dict) -> str:
    """What was deployed, so a readout stands alone without its config."""
    if ep["method"] == "pt":
        if "provisioned_model_units" in ep:
            capacity = f"{ep['provisioned_model_units']} model units"
        else:
            capacity = f"{ep['min_provisioned_throughput']}–{ep['max_provisioned_throughput']} tokens/s"
        return f"pt, {ep['entity_name']} v{ep['entity_version']}, {capacity}"
    if ep["method"] == "custom":
        scale = "on" if ep.get("scale_to_zero_enabled") else "off"
        return f"custom, {ep['workload_type']}, scale-to-zero {scale}"
    return "ppt, shared"


def readout_document(meta: dict, rows: list[dict]) -> str:
    """Self-contained markdown readout for readouts/<model>.md."""
    lt, idle = meta["load_test"], meta.get("idle_test") or {}
    endpoints = "\n".join(f"- {ep['label']}: `{ep['name']}` ({endpoint_settings(ep)})" for ep in meta["endpoints"])
    per_client = lt.get("requests_per_client")
    requests = f"max({lt['requests_per_level']}, {per_client} × concurrency)" if per_client else str(
        lt["requests_per_level"])
    idle_line = (
        f"{', '.join(f'{s}s' for s in idle['idle_seconds'])} × {idle['reps']} reps"
        if idle.get("idle_seconds") else "not run"
    )
    return f"""# {meta['model']} ({meta['task']})

Run `{meta['run_id']}` on {meta['date']} in {meta['workspace']}.

{readout_markdown(rows)}

## Settings

- Input: {meta['input_tokens']} tokens, same text for every request.
- Concurrency levels: {', '.join(map(str, lt['concurrency_levels']))}; requests per level: {requests}.
- Idle test: {idle_line}; one probe per rep after each idle duration.
- Saturation concurrency: the lowest level reaching 90% of peak RPS. Levels with more than 1% failed
  requests are excluded from peak and saturation (see Notes); their RPS counts only requests that got through.
- Custom models logged on AI env v{meta.get('ai_env', '?')}.

## Endpoints

{endpoints}

Raw data: `serving_bench_requests` and `serving_bench_summary`, filtered on `run_id = '{meta['run_id']}'`.
"""


class ModelCheckError(RuntimeError):
    """The model failed to load or run in this environment — usually a library-version mismatch."""


def library_versions() -> dict:
    import torch
    import transformers

    return {
        "transformers": transformers.__version__,
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }


def load_and_check(model: dict, ai_env: str):
    """Load the HF model on this GPU and run one real inference before anything is deployed.

    Returns (loaded, info): a SentenceTransformer for embeddings, (tokenizer, model) for chat.
    Raises ModelCheckError with the environment and library versions on any failure.
    """
    import os

    if not os.environ.get("DATABRICKS_ACCELERATOR"):
        raise RuntimeError("Custom models must be loaded on Serverless GPU compute.")
    versions = {"ai_env": ai_env} | library_versions()
    try:
        if model["task"] == "embedding":
            from sentence_transformers import SentenceTransformer

            loaded = SentenceTransformer(
                model["hf_name"], device="cuda", trust_remote_code=model["trust_remote_code"]
            )
            vector = loaded.encode(["serving-bench compatibility check"])[0]
            return loaded, versions | {"embedding_dim": len(vector)}
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(model["hf_name"])
        lm = AutoModelForCausalLM.from_pretrained(model["hf_name"], torch_dtype=torch.float16)
        lm = lm.to("cuda").eval()
        encoded = tokenizer.apply_chat_template(
            [{"role": "user", "content": "Say hi"}], add_generation_prompt=True,
            return_tensors="pt", return_dict=True,
        ).to("cuda")
        with torch.inference_mode():
            output = lm.generate(**encoded, max_new_tokens=8, do_sample=False)
        reply = tokenizer.decode(output[0][encoded["input_ids"].shape[-1]:], skip_special_tokens=True)
        return (tokenizer, lm), versions | {"reply": reply}
    except Exception as error:
        raise ModelCheckError(incompatibility_message(model, versions, error)) from error


def incompatibility_message(model: dict, versions: dict, error: Exception) -> str:
    return (
        f"{model['hf_name']} failed to load or run on AI env v{versions['ai_env']} "
        f"(transformers {versions['transformers']}, torch {versions['torch']}, "
        f"CUDA {versions['cuda']}): {type(error).__name__}: {str(error)[:300]}\n"
        "This is usually a library-version incompatibility (e.g. remote model code written for "
        "transformers 4.x on an env with 5.x). No model was registered and no endpoints were "
        "created. Run 00_compat_check on the other AI env candidates "
        f"({', '.join(model.get('ai_env_candidates', []))}) to find one that works."
    )
