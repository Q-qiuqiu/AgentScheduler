from typing import Any

from get_cluster_resources_tool import get_cluster_resources
from run_task_on_node_tool import run_task_on_node
from tool_common import default_static_info_path, load_json_config


SCHEDULER_WEIGHTS = {
    "base_latency_ms": 0.0003,
    "cpu_after": 0.01,
    "mem_after": 2.0,
    "npu_after": 0.8,
}


def normalize_platform(platform: str | None) -> str | None:
    if platform in {"RK3588", "ATLAS_L", "ATLAS_H"}:
        return platform
    if platform == "Atlas 200DK":
        return "ATLAS_L"
    if platform == "Orange Pi aipro":
        return "ATLAS_H"
    return platform


def node_global_id(node: dict[str, Any]) -> str | None:
    return node.get("global_id") or node.get("target_global_id") or node.get("node_id")


def is_excluded(node: dict[str, Any], constraints: dict[str, Any]) -> bool:
    excluded = set((constraints or {}).get("exclude_nodes", []))
    return node_global_id(node) in excluded or normalize_platform(node.get("type")) in excluded


def is_required(node: dict[str, Any], constraints: dict[str, Any]) -> bool:
    required = set((constraints or {}).get("require_nodes", []))
    require_node = (constraints or {}).get("require_node")
    if require_node:
        required.add(require_node)
    if not required:
        return True
    return node_global_id(node) in required or normalize_platform(node.get("type")) in required


def node_resource_ratio(node: dict[str, Any], key: str, default: float = 1.0) -> float:
    resource = node.get("resource", {}) if isinstance(node.get("resource"), dict) else {}
    value = resource.get(key)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def static_base_latency_ms(static_info: dict[str, Any], task_type: str, device_type: str | None) -> float:
    if not device_type:
        return 0.0
    task_entry = static_info.get(task_type, {}) if isinstance(static_info, dict) else {}
    device_entry = task_entry.get(device_type, {}) if isinstance(task_entry, dict) else {}
    overhead = device_entry.get("taskOverhead", {}) if isinstance(device_entry, dict) else {}
    value = overhead.get("base_latency_ms")
    return float(value) if isinstance(value, (int, float)) else 0.0


def candidate_nodes(task_type: str, constraints: dict[str, Any]) -> list[dict[str, Any]]:
    static_info = load_json_config(default_static_info_path(), "static_info.json")
    supported = set(static_info.get(task_type, {}).keys())
    cluster_resources = get_cluster_resources({})
    result = cluster_resources.get("result", [])
    if not isinstance(result, list):
        raise RuntimeError(f"get_cluster_resources returned invalid payload: {cluster_resources}")

    candidates = []
    for node in result:
        if not isinstance(node, dict):
            continue
        if node.get("online") is False:
            continue
        target_global_id = node_global_id(node)
        device_type = normalize_platform(node.get("type"))
        if not target_global_id or not device_type:
            continue
        if supported and device_type not in supported:
            continue
        if is_excluded(node, constraints) or not is_required(node, constraints):
            continue
        cpu_after = node_resource_ratio(node, "cpu_used")
        mem_after = node_resource_ratio(node, "mem_used")
        npu_after = node_resource_ratio(node, "npu_used")
        base_latency_ms = static_base_latency_ms(static_info, task_type, device_type)
        candidates.append({
            "node": node,
            "detail": {
                "target_global_id": target_global_id,
                "device_type": device_type,
                "base_latency_ms": round(base_latency_ms, 3),
                "cpu_after": round(cpu_after, 6),
                "mem_after": round(mem_after, 6),
                "npu_after": round(npu_after, 6),
                "container_running": bool(node.get("container_running", False)),
                "cold_start": not bool(node.get("container_running", False)),
            },
        })
    return candidates


def select_candidate(candidates: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    if not candidates:
        raise RuntimeError("no candidate nodes available for schedule_on_node")
    policy = (policy or "scheduler").strip()
    ranked = []
    for item in candidates:
        detail = item["detail"]
        node_id = detail["target_global_id"]
        avg_load = round((detail["cpu_after"] + detail["mem_after"] + detail["npu_after"]) / 3, 6)
        if policy == "scheduler":
            score = round(
                SCHEDULER_WEIGHTS["base_latency_ms"] * detail["base_latency_ms"]
                + SCHEDULER_WEIGHTS["cpu_after"] * detail["cpu_after"]
                + SCHEDULER_WEIGHTS["npu_after"] * detail["npu_after"]
                + SCHEDULER_WEIGHTS["mem_after"] * detail["mem_after"],
                6,
            )
            sort_key = (score, node_id)
        elif policy == "resource_average":
            score = avg_load
            sort_key = (score, node_id)
        elif policy == "cpu_min":
            score = detail["cpu_after"]
            sort_key = (score, node_id)
        elif policy == "npu_min":
            score = detail["npu_after"]
            sort_key = (score, node_id)
        elif policy == "mem_min":
            score = detail["mem_after"]
            sort_key = (score, node_id)
        elif policy == "container_first":
            score = avg_load
            sort_key = (1 if detail["cold_start"] else 0, score, node_id)
        else:
            raise RuntimeError(f"unsupported schedule_on_node policy: {policy}")
        ranked.append((sort_key, score, item))
    ranked.sort(key=lambda item: item[0])
    _, score, selected = ranked[0]
    return {
        "policy": policy,
        "target_global_id": selected["detail"]["target_global_id"],
        "device_type": selected["detail"]["device_type"],
        "score": score,
        "eligible_nodes": [item[2]["detail"]["target_global_id"] for item in ranked],
        "detail": selected["detail"],
    }


def schedule_on_node(arguments: dict[str, Any]) -> dict[str, Any]:
    task_type = arguments.get("task_type")
    if not task_type:
        raise RuntimeError("task_type is required")
    policy = arguments.get("policy", "scheduler")
    constraints = {
        "exclude_nodes": arguments.get("exclude_nodes", []),
        "require_node": arguments.get("require_node"),
        "require_nodes": arguments.get("require_nodes", []),
    }
    selection = select_candidate(candidate_nodes(task_type, constraints), policy)
    run_arguments = dict(arguments)
    run_arguments["task_type"] = task_type
    run_arguments["target_global_id"] = selection["target_global_id"]
    run_arguments.pop("policy", None)
    run_arguments.pop("exclude_nodes", None)
    run_arguments.pop("require_node", None)
    run_arguments.pop("require_nodes", None)
    result = run_task_on_node(run_arguments)
    if not isinstance(result, dict):
        return result
    return {
        **result,
        "schedule_policy": selection["policy"],
        "scheduled_target_global_id": selection["target_global_id"],
        "scheduled_device_type": selection["device_type"],
        "schedule_score": selection["score"],
        "eligible_nodes": selection["eligible_nodes"],
        "schedule_detail": selection["detail"],
    }
