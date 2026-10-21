import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from discover import ai_envs_from, build_options, cloud_of, model_settings, parse_gpu_shapes, to_markdown  # noqa: E402

GIB = 1024**3
GTE = "Alibaba-NLP/gte-large-en-v1.5"
GTE_CATALOG = "system.ai.gte_large_en_v1_5"
AWS_SHAPES = {
    "GPU_SMALL": {"gpu": "T4", "count": 1, "memory_gb": 16, "notes": ""},
    "GPU_MEDIUM": {"gpu": "A10G", "count": 1, "memory_gb": 24, "notes": ""},
    "MULTIGPU_MEDIUM": {"gpu": "A10G", "count": 4, "memory_gb": 24, "notes": ""},
    "GPU_LARGE": {"gpu": "L40", "count": 1, "memory_gb": 48, "notes": "Beta; some regions only"},
}


def hf(name=GTE, weights=int(1.6 * GIB), config=None, chat_template=False):
    return {
        "hf_name": name,
        "metadata": {"siblings": [{"rfilename": "model.safetensors", "size": weights}]},
        "config": config if config is not None else {"auto_map": {"AutoModel": "x"}},
        "chat_template": chat_template,
    }


def catalog_model(name=GTE_CATALOG, url=GTE, versions=None):
    return {
        "name": name,
        "comment": f"Source: https://huggingface.co/{url}" if url else "GTE large",
        "versions": versions
        if versions is not None
        else [
            {
                "version": "1",
                "status": "READY",
                "optimization": {
                    "optimizable": True,
                    "task": "llm/v1/embeddings",
                    "throughput_chunk_size": 19000,
                },
            },
            {
                "version": "2",
                "status": "READY",
                "optimization": {
                    "optimizable": True,
                    "task": "llm/v1/embeddings",
                    "throughput_chunk_size": 19000,
                },
            },
        ],
    }


def ppt_endpoint(name="databricks-gte-large-en", entity=GTE_CATALOG, task="llm/v1/embeddings"):
    return {"name": name, "task": task, "entity_name": entity, "foundation": True, "ready": "READY"}


def by_method(options, method):
    return [o for o in options if o["method"] == method]


def test_gte_embedding_finds_pt_ppt_and_single_gpu_shapes():
    options = build_options(
        "embedding", hf(), {"gpu_shapes": AWS_SHAPES, "catalog": [catalog_model()], "endpoints": [ppt_endpoint()]}
    )

    (pt,) = by_method(options, "pt")
    assert pt["viable"]
    assert pt["endpoint"] == {
        "name": "sb-gte-large-en-v1-5-pt",
        "method": "pt",
        "entity_name": GTE_CATALOG,
        "entity_version": "2",
        "min_provisioned_throughput": 19000,
        "max_provisioned_throughput": 19000,
    }

    (ppt,) = by_method(options, "ppt")
    assert ppt["viable"]
    assert ppt["endpoint"] == {"name": "databricks-gte-large-en", "method": "ppt"}

    custom = {o["target"]: o for o in by_method(options, "custom")}
    assert all(custom[w]["viable"] for w in ("GPU_SMALL", "GPU_MEDIUM", "GPU_LARGE"))
    assert custom["GPU_SMALL"]["endpoint"] == {
        "name": "sb-gte-large-en-v1-5-gpu-small",
        "method": "custom",
        "label": "Custom GPU (T4)",
        "workload_type": "GPU_SMALL",
        "scale_to_zero_enabled": False,
    }
    assert "Beta" in custom["GPU_LARGE"]["reason"]
    assert not custom["MULTIGPU_MEDIUM"]["viable"]
    assert "one GPU" in custom["MULTIGPU_MEDIUM"]["reason"]
    assert "trust_remote_code" in custom["GPU_SMALL"]["reason"]


def test_model_without_catalog_match_is_custom_only():
    options = build_options(
        "embedding",
        hf("sentence-transformers/all-MiniLM-L6-v2", weights=90 * 1024**2, config={}),
        {"gpu_shapes": AWS_SHAPES, "catalog": [catalog_model()], "endpoints": [ppt_endpoint()]},
    )
    assert [o["viable"] for o in by_method(options, "pt")] == [False]
    assert [o["viable"] for o in by_method(options, "ppt")] == [False]
    viable = {o["target"] for o in by_method(options, "custom") if o["viable"]}
    assert viable == {"GPU_SMALL", "GPU_MEDIUM", "GPU_LARGE"}


def test_similar_name_without_source_url_is_not_a_match():
    options = build_options(
        "embedding", hf(), {"gpu_shapes": AWS_SHAPES, "catalog": [catalog_model(url=None)], "endpoints": [ppt_endpoint()]}
    )
    assert not any(o["viable"] for o in by_method(options, "pt") + by_method(options, "ppt"))


def test_pt_task_mismatch_and_unoptimizable_are_not_viable():
    versions = [
        {"version": "1", "status": "READY", "optimization": {"optimizable": False}},
    ]
    options = build_options(
        "chat",
        hf(chat_template=True, config={}),
        {"gpu_shapes": AWS_SHAPES, "catalog": [catalog_model(versions=versions)], "endpoints": [ppt_endpoint()]},
    )
    (pt,) = by_method(options, "pt")
    assert not pt["viable"]
    (ppt,) = by_method(options, "ppt")
    assert not ppt["viable"] and "task" in ppt["reason"]


def test_pt_model_units():
    versions = [
        {
            "version": "3",
            "status": "READY",
            "optimization": {
                "optimizable": True,
                "task": "llm/v1/chat",
                "model_unit_chunk_size": 50,
            },
        }
    ]
    options = build_options(
        "chat",
        hf(chat_template=True, config={}),
        {"gpu_shapes": AWS_SHAPES, "catalog": [catalog_model(versions=versions)], "endpoints": []},
    )
    (pt,) = by_method(options, "pt")
    assert pt["viable"]
    assert pt["endpoint"]["provisioned_model_units"] == 50
    assert "min_provisioned_throughput" not in pt["endpoint"]


def test_large_chat_model_excludes_small_gpus():
    config = {
        "num_hidden_layers": 32,
        "num_attention_heads": 32,
        "num_key_value_heads": 8,
        "hidden_size": 4096,
    }
    options = build_options(
        "chat",
        hf("meta-llama/Llama-3.1-8B-Instruct", weights=16 * GIB, config=config, chat_template=True),
        {"gpu_shapes": AWS_SHAPES, "catalog": [], "endpoints": []},
    )
    custom = {o["target"]: o for o in by_method(options, "custom")}
    assert not custom["GPU_SMALL"]["viable"]
    assert not custom["GPU_MEDIUM"]["viable"]
    assert custom["GPU_LARGE"]["viable"]


def test_custom_chat_needs_chat_template_and_no_remote_code():
    no_template = build_options(
        "chat", hf(config={}, chat_template=False), {"gpu_shapes": AWS_SHAPES, "catalog": [], "endpoints": []}
    )
    remote = build_options(
        "chat", hf(chat_template=True), {"gpu_shapes": AWS_SHAPES, "catalog": [], "endpoints": []}
    )
    for options in (no_template, remote):
        assert not any(o["viable"] for o in by_method(options, "custom"))


def test_markdown_lists_every_option():
    options = build_options(
        "embedding", hf(), {"gpu_shapes": AWS_SHAPES, "catalog": [catalog_model()], "endpoints": [ppt_endpoint()]}
    )
    table = to_markdown(options)
    assert table.count("\n") == len(options) + 1
    assert "databricks-gte-large-en" in table


def test_model_settings_use_latest_env_and_keep_three_candidates():
    assert model_settings(hf(), [6, 5, 4]) == {
        "hf_name": GTE,
        "trust_remote_code": True,
        "registered_name": "gte_large_en_v1_5",
        "ai_env": "6",
        "ai_env_candidates": ["6", "5", "4"],
    }


def test_ai_envs_from_workspace_base_environments():
    base_envs = [
        {"name": "workspace-base-environments/databricks_ai", "base_environment_type": "GPU"},
        {"name": "workspace-base-environments/databricks_ai_v4", "base_environment_type": "GPU"},
        {"name": "workspace-base-environments/databricks_ai_v6", "base_environment_type": "GPU"},
        {"name": "workspace-base-environments/databricks_ai_v5", "base_environment_type": "GPU"},
        {"name": "workspace-base-environments/databricks_ml_v6", "base_environment_type": "CPU"},
        {"name": "workspace-base-environments/someone_custom_ai_v9", "base_environment_type": "GPU"},
    ]
    assert ai_envs_from(base_envs) == [6, 5, 4]


def test_custom_options_follow_the_clouds_gpu_list():
    azure = {
        "GPU_SMALL": {"gpu": "T4", "count": 1, "memory_gb": 16, "notes": ""},
        "GPU_LARGE": {"gpu": "A100", "count": 1, "memory_gb": 80, "notes": ""},
    }
    options = build_options("embedding", hf(), {"gpu_shapes": azure, "catalog": [], "endpoints": []})
    custom = {o["target"]: o for o in by_method(options, "custom")}
    assert set(custom) == {"GPU_SMALL", "GPU_LARGE"}
    assert custom["GPU_LARGE"]["endpoint"]["label"] == "Custom GPU (A100)"


def test_parse_gpu_shapes_tolerates_missing_end_tags():
    html = (
        "<table><tr><th>Workload type<th>GPU instance<th>Memory<th>Notes"
        "<tr><td>CPU<td><td>4GB per concurrency<td>"
        "<tr><td>GPU_SMALL<td>1xT4<td>16GB per concurrency<td>"
        "<tr><td>MULTIGPU_MEDIUM<td>4xA10G<td>96GB per concurrency<td>"
        "<tr><td>GPU_LARGE (Beta)<td>1xL40<td>48GB per concurrency<td>Only available in us-west-2.</table>"
    )
    assert parse_gpu_shapes(html) == {
        "GPU_SMALL": {"gpu": "T4", "count": 1, "memory_gb": 16, "notes": ""},
        "MULTIGPU_MEDIUM": {"gpu": "A10G", "count": 4, "memory_gb": 24, "notes": ""},
        "GPU_LARGE": {"gpu": "L40", "count": 1, "memory_gb": 48, "notes": "Beta; Only available in us-west-2."},
    }


def test_cloud_of_workspace_host():
    assert cloud_of("https://e2-demo-field-eng.cloud.databricks.com") == "aws"
    assert cloud_of("https://adb-123.4.azuredatabricks.net/") == "azure"
    assert cloud_of("https://123.4.gcp.databricks.com") == "gcp"
