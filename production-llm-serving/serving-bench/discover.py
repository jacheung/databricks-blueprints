#!/usr/bin/env python3
"""List which Databricks serving methods can serve a Hugging Face model.

Read-only: reads HF metadata (no weights), system.ai catalog metadata, PT optimization
info, serving endpoint metadata, the workspace's serverless GPU AI environments, and the
custom-serving GPU list from the docs for the workspace's cloud. Creates nothing and sends no inference traffic.

    python discover.py --model Alibaba-NLP/gte-large-en-v1.5 --task embedding --profile PROFILE
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from html.parser import HTMLParser
import urllib.error
import urllib.parse
import urllib.request

from hf_gpu_estimator import GIB, classify, fetch_json, kv_cache_bytes, weight_bytes

TASKS = {"embedding": "llm/v1/embeddings", "chat": "llm/v1/chat"}
DEFAULT_CONTEXT = {"embedding": 512, "chat": 4096}


def source_models(text: str) -> set[str]:
    """Only explicit HF repository URLs establish identity; similar names never do."""
    return {
        match[1].rstrip(".")
        for match in re.finditer(r"https://huggingface\.co/([\w.-]+/[\w.-]+)", text or "")
        if match[1].split("/")[0] not in {"collections", "spaces", "datasets", "docs"}
    }


def endpoint_prefix(hf_name: str) -> str:
    return "sb-" + re.sub(r"[^a-z0-9]+", "-", hf_name.split("/")[-1].lower()).strip("-")[:40]


def pt_options(task: str, hf: dict, workspace: dict) -> list[dict]:
    options = []
    for model in workspace["catalog"]:
        if hf["hf_name"] not in source_models(model.get("comment", "")):
            continue
        ready = [v for v in model["versions"] if v.get("status") == "READY"]
        if not ready:
            options.append(_option("pt", model["name"], False, "No READY catalog version"))
            continue
        version = max(ready, key=lambda v: int(v["version"]))
        target = f"{model['name']} v{version['version']}"
        info = version.get("optimization") or {}
        if not info.get("optimizable"):
            options.append(_option("pt", target, False, "Optimization API: not optimizable"))
            continue
        if info.get("task") != TASKS[task]:
            reason = f"PT task is {info.get('task')}, not {TASKS[task]}"
            options.append(_option("pt", target, False, reason))
            continue
        endpoint = {
            "name": endpoint_prefix(hf["hf_name"]) + "-pt",
            "method": "pt",
            "entity_name": model["name"],
            "entity_version": str(version["version"]),
        }
        if info.get("throughput_chunk_size"):
            chunk = info["throughput_chunk_size"]
            endpoint |= {"min_provisioned_throughput": chunk, "max_provisioned_throughput": chunk}
            reason = f"Smallest PT capacity: {chunk} tokens/s (one increment)"
        elif info.get("model_unit_chunk_size"):
            chunk = info["model_unit_chunk_size"]
            endpoint["provisioned_model_units"] = chunk
            reason = f"Smallest PT capacity: {chunk} model units (one increment)"
        else:
            options.append(_option("pt", target, False, "PT capacity increment unknown"))
            continue
        options.append(_option("pt", target, True, reason, endpoint))
    return options or [_option("pt", "-", False, "No system.ai model links to this HF repo")]


def ppt_options(task: str, hf: dict, workspace: dict) -> list[dict]:
    matched = {
        m["name"] for m in workspace["catalog"] if hf["hf_name"] in source_models(m.get("comment"))
    }
    options = []
    for ep in workspace["endpoints"]:
        if not ep.get("foundation") or ep.get("entity_name") not in matched:
            continue
        if ep.get("task") != TASKS[task]:
            reason = f"Endpoint task is {ep.get('task')}, not {TASKS[task]}"
            options.append(_option("ppt", ep["name"], False, reason))
        elif ep.get("ready") != "READY":
            options.append(_option("ppt", ep["name"], False, f"Endpoint is {ep.get('ready')}"))
        else:
            reason = f"Shared pay-per-token endpoint serving {ep['entity_name']}"
            endpoint = {"name": ep["name"], "method": "ppt"}
            options.append(_option("ppt", ep["name"], True, reason, endpoint))
    return options or [_option("ppt", "-", False, "No pay-per-token endpoint serves this model")]


def custom_options(task: str, hf: dict, context: int, shapes: dict) -> list[dict]:
    remote_code = remote_code_of(hf)
    blocker = None
    if task == "chat" and remote_code:
        blocker = "Custom chat adapter does not run remote model code"
    elif task == "chat" and not hf["chat_template"]:
        blocker = "Tokenizer has no chat template"
    try:
        weights, _ = weight_bytes(hf["metadata"])
        # Without config.json (gated repo), the KV cache is unknown: estimate weights only.
        kv = kv_cache_bytes(hf["config"], context, 1, 2) if task == "chat" and hf["config"] else 0
    except RuntimeError as error:
        blocker = blocker or f"Memory estimate unavailable: {error}"
        weights = kv = 0
    required = int((weights * 1.2 + kv) * 1.15)
    estimate = "weights only" if hf["config"] is None else ""
    options = []
    for workload, shape in shapes.items():
        gpu, memory = shape["gpu"], shape["memory_gb"]
        if blocker:
            options.append(_option("custom", workload, False, blocker))
            continue
        if shape["count"] > 1:
            reason = f"{shape['count']}x {gpu}: multi-GPU; the serving adapters run the model on one GPU"
            options.append(_option("custom", workload, False, reason))
            continue
        fit = classify(required, 1, memory, 0.9)
        viable = fit["status"] != "unsupported"
        needs = f"~{required / GIB:.1f} GiB" + (f" ({estimate})" if estimate else "")
        reason = f"1x {gpu} {memory}GB: needs {needs}, {fit['utilization_percent']}% of usable VRAM ({fit['status']})"
        if viable and hf["config"] is None:
            viable = False
            reason += "; needs Hugging Face access to this gated repo to deploy"
        if remote_code:
            reason += "; needs trust_remote_code"
        if shape["notes"]:
            reason += f"; {shape['notes']}"
        endpoint = {
            "name": f"{endpoint_prefix(hf['hf_name'])}-{workload.lower().replace('_', '-')}",
            "method": "custom",
            "label": f"Custom GPU ({gpu})",
            "workload_type": workload,
            "scale_to_zero_enabled": False,
        }
        options.append(_option("custom", workload, viable, reason, endpoint if viable else None))
    fitting = [o for o in options if o["viable"]]
    if fitting:
        min(fitting, key=lambda o: shapes[o["target"]]["memory_gb"])["recommended"] = True
    return options


def _option(method, target, viable, reason, endpoint=None) -> dict:
    return {
        "method": method,
        "target": target,
        "viable": viable,
        "recommended": False,  # custom only: the smallest GPU that fits
        "reason": reason,
        "endpoint": endpoint,
    }


def build_options(task: str, hf: dict, workspace: dict, context: int | None = None) -> list[dict]:
    if task not in TASKS:
        raise ValueError(f"task must be one of {sorted(TASKS)}")
    context = context or DEFAULT_CONTEXT[task]
    return (
        pt_options(task, hf, workspace)
        + ppt_options(task, hf, workspace)
        + custom_options(task, hf, context, workspace["gpu_shapes"])
    )


def model_settings(hf: dict, ai_envs: list[int]) -> dict:
    """Settings 01_deploy.py needs for custom endpoints.

    Deploy starts on the latest AI env. If the model breaks there (e.g. GTE v1.5's remote code
    on transformers 5), 00_compat_check tries the other candidates.
    """
    return {
        "hf_name": hf["hf_name"],
        "trust_remote_code": remote_code_of(hf),
        "registered_name": re.sub(r"[^a-z0-9]+", "_", hf["hf_name"].split("/")[-1].lower()).strip("_"),
        "ai_env": str(ai_envs[0]),
        "ai_env_candidates": [str(v) for v in ai_envs[:3]],
    }


def remote_code_of(hf: dict) -> bool:
    return any("auto_map" in (c or {}) for c in (hf["config"], hf["metadata"].get("config")))


def ai_envs_from(base_envs: list[dict]) -> list[int]:
    """Numbered serverless GPU AI envs in the workspace, newest first.

    Skips the moving "AI Latest" alias so configs and readouts record a fixed version.
    """
    versions = {
        int(m[1])
        for env in base_envs
        if env.get("base_environment_type") == "GPU"
        and (m := re.fullmatch(r"workspace-base-environments/databricks_ai_v(\d+)", env.get("name", "")))
    }
    return sorted(versions, reverse=True)


def to_markdown(options: list[dict]) -> str:
    rows = ["| Method | Target | Viable | Reason |", "| --- | --- | --- | --- |"]
    for o in options:
        rows.append(
            f"| {o['method']} | {o['target']} | {'yes (recommended)' if o['recommended'] else 'yes' if o['viable'] else 'no'} | {o['reason']} |"
        )
    return "\n".join(rows)


class _Table(HTMLParser):
    """Table rows; tolerates the omitted </td>/</tr> end tags on docs.databricks.com."""

    def __init__(self, html: str):
        super().__init__()
        self.rows: list[list[str]] = []
        self.row: list[str] = []
        self.cell: list[str] | None = None
        self.feed(html)
        self.flush_row()

    def flush_cell(self) -> None:
        if self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None

    def flush_row(self) -> None:
        self.flush_cell()
        if self.row:
            self.rows.append(self.row)
            self.row = []

    def handle_starttag(self, tag, attrs):
        if tag in ("tr", "table"):
            self.flush_row()
        if tag in ("td", "th"):
            self.flush_cell()
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self.flush_cell()
        if tag in ("tr", "table"):
            self.flush_row()

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)


def parse_gpu_shapes(html: str) -> dict:
    """Custom model serving GPU workload types from the docs table: {workload: gpu, count, memory}."""
    shapes = {}
    for row in _Table(html).rows:
        workload = re.match(r"((?:MULTI)?GPU_[A-Z0-9_]+)", row[0]) if row else None
        gpu = re.fullmatch(r"(\d+)\s*[x×]\s*(.+)", row[1]) if workload and len(row) > 2 else None
        memory = re.search(r"(\d+)\s*GB", row[2]) if gpu else None
        if memory:
            notes = (["Beta"] if "Beta" in row[0] else []) + [n for n in row[3:] if n]
            shapes[workload[1]] = {
                "gpu": gpu[2],
                "count": int(gpu[1]),
                "memory_gb": int(memory[1]) // int(gpu[1]),
                "notes": "; ".join(notes),
            }
    return shapes


def cloud_of(host: str) -> str:
    if "azuredatabricks.net" in host:
        return "azure"
    return "gcp" if "gcp.databricks.com" in host else "aws"


GPU_DOCS = {
    "aws": "https://docs.databricks.com/aws/en/machine-learning/model-serving/custom-models",
    "gcp": "https://docs.databricks.com/gcp/en/machine-learning/model-serving/custom-models",
    "azure": "https://learn.microsoft.com/en-us/azure/databricks/machine-learning/model-serving/custom-models",
}


# ── Live metadata reads ───────────────────────────────────────────────


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "serving-bench"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_gpu_shapes(host: str) -> dict:
    url = GPU_DOCS[cloud_of(host)]
    shapes = parse_gpu_shapes(fetch_text(url))
    if not shapes:
        raise RuntimeError(f"No GPU workload types found at {url}")
    return shapes



def fetch_hf(hf_name: str, revision: str = "main", token: str | None = None) -> dict:
    """HF metadata is public even for gated repos; their files (config.json) need approved access."""
    quoted, quoted_revision = urllib.parse.quote(hf_name, safe="/"), urllib.parse.quote(revision, safe="")
    metadata = fetch_json(f"https://huggingface.co/api/models/{quoted}/revision/{quoted_revision}?blobs=true", token)
    try:
        config, config_error = fetch_json(f"https://huggingface.co/{quoted}/resolve/{quoted_revision}/config.json", token), None
    except RuntimeError as error:
        config, config_error = None, str(error)
    files = {s["rfilename"] for s in metadata.get("siblings", [])}
    chat_template = "chat_template.jinja" in files or bool(
        (metadata.get("config") or {}).get("tokenizer_config", {}).get("chat_template")
    )
    if not chat_template and config is not None and "tokenizer_config.json" in files:
        url = f"https://huggingface.co/{quoted}/resolve/{quoted_revision}/tokenizer_config.json"
        chat_template = bool(fetch_json(url, token).get("chat_template"))
    return {
        "hf_name": hf_name,
        "metadata": metadata,
        "config": config,
        "config_error": config_error,
        "chat_template": chat_template,
        "license": (metadata.get("cardData") or {}).get("license"),
        "gated": metadata.get("gated", False),
    }


def hf_summary(hf: dict) -> dict:
    """Open-weights check: what the customer needs to know before choosing options."""
    try:
        weights = round(weight_bytes(hf["metadata"])[0] / GIB, 1)
    except RuntimeError:
        weights = None
    return {
        "license": hf["license"],
        "gated": hf["gated"],
        "files_accessible": hf["config"] is not None,
        "weights_gib": weights,
        "parameters": sum((hf["metadata"].get("safetensors") or {}).get("parameters", {}).values()) or None,
    }


def fetch_workspace(client, hf_name: str) -> dict:
    catalog = []
    for model in client.registered_models.list(
        catalog_name="system", schema_name="ai", include_browse=True
    ):
        entry = {"name": model.full_name, "comment": model.comment or "", "versions": []}
        if hf_name in source_models(entry["comment"]):
            for v in client.model_versions.list(full_name=model.full_name, include_browse=True):
                version = {"version": str(v.version), "status": v.status and v.status.value}
                if version["status"] == "READY":
                    path = urllib.parse.quote(model.full_name, safe="")
                    version["optimization"] = client.api_client.do(
                        "GET",
                        f"/api/2.0/serving-endpoints/get-model-optimization-info/{path}/{v.version}",
                    )
                entry["versions"].append(version)
        catalog.append(entry)

    # Raw REST list: the SDK's list objects drop the provisioned-capacity fields.
    endpoints, query = [], {}
    while True:
        page = client.api_client.do("GET", "/api/2.0/serving-endpoints", query=query)
        for ep in page.get("endpoints", []):
            entities = ep.get("config", {}).get("served_entities", [])
            if len(entities) != 1:
                continue
            entity = entities[0]
            dedicated = entity.get("min_provisioned_throughput") or entity.get(
                "provisioned_model_units"
            )
            endpoints.append(
                {
                    "name": ep["name"],
                    "task": ep.get("task"),
                    "entity_name": entity.get("entity_name"),
                    "foundation": bool(entity.get("foundation_model")) and not dedicated,
                    "ready": ep.get("state", {}).get("ready"),
                }
            )
        if not page.get("next_page_token"):
            return {"catalog": catalog, "endpoints": endpoints}
        query = {"page_token": page["next_page_token"]}


def fetch_ai_envs(client) -> list[int]:
    base_envs, query = [], {}
    while True:
        page = client.api_client.do("GET", "/api/environments/v1/workspace-base-environments", query=query)
        base_envs += page.get("workspace_base_environments", [])
        if not page.get("next_page_token"):
            break
        query = {"page_token": page["next_page_token"]}
    envs = ai_envs_from(base_envs)
    if not envs:
        raise RuntimeError("No serverless GPU AI environments (databricks_ai_vN) in this workspace")
    return envs


def discover(hf_name: str, task: str, client, revision: str = "main") -> dict:
    hf = fetch_hf(hf_name, revision, os.getenv("HF_TOKEN"))
    return {
        "hf": hf_summary(hf),
        "model": model_settings(hf, fetch_ai_envs(client)) | {"task": task},
        "options": build_options(
            task, hf, fetch_workspace(client, hf_name) | {"gpu_shapes": fetch_gpu_shapes(client.config.host)}
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", required=True, help="HF repo id, e.g. owner/name")
    parser.add_argument("--task", required=True, choices=sorted(TASKS))
    parser.add_argument("--profile", required=True, help="Databricks CLI profile")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of markdown")
    args = parser.parse_args()

    from databricks.sdk import WorkspaceClient

    try:
        result = discover(args.model, args.task, WorkspaceClient(profile=args.profile), args.revision)
    except (RuntimeError, urllib.error.URLError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result["hf"]))
        print(json.dumps(result["model"]))
        print(to_markdown(result["options"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
