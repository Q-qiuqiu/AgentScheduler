#!/usr/bin/env python3
import json
from pathlib import Path


BENCH_ROOT = Path(__file__).resolve().parents[1]
QUERY_ROOT = BENCH_ROOT / "queries"
STATE_ROOT = BENCH_ROOT / "states"
CATALOG_PATH = BENCH_ROOT / "catalog" / "catalog.json"
OUTPUT_DIR = BENCH_ROOT / "cases"
MANIFEST_PATH = OUTPUT_DIR / "manifest.json"

EXECUTION_TASKS = {
    "object_detection",
    "image_classification",
    "semantic_segmentation",
    "text_classification",
}

ORACLE = {
    "name": "least_loaded_hot_container_oracle",
    "objective": "minimize weighted load while respecting online status, task support, and query constraints",
    "weights": {
        "npu_used": 0.50,
        "cpu_used": 0.20,
        "mem_used": 0.20,
        "cold_start_penalty": 0.10
    },
    "notes": [
        "network_latency is fixed to 1 in all states and is intentionally ignored",
        "lower score is better",
        "nodes are excluded when either node_id or platform matches an exclude_nodes constraint",
        "nodes are allowed only when node_id or platform matches require_node or require_nodes constraints"
    ]
}


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_queries():
    queries = []
    for path in sorted(QUERY_ROOT.glob("*/*.json")):
        query = load_json(path)
        query["_path"] = str(path.relative_to(BENCH_ROOT))
        queries.append(query)
    return queries


def load_states():
    states = []
    for path in sorted(STATE_ROOT.glob("state_*.json")):
        state = load_json(path)
        state["_path"] = str(path.relative_to(BENCH_ROOT))
        states.append(state)
    return states


def load_catalog_tasks():
    catalog = load_json(CATALOG_PATH)
    return {
        task["task_type"]: task
        for task in catalog.get("tasks", [])
        if isinstance(task, dict) and task.get("task_type")
    }


def is_excluded(node, constraints):
    excluded = set((constraints or {}).get("exclude_nodes", []))
    return node.get("node_id") in excluded or node.get("platform") in excluded


def is_required(node, constraints):
    required = set((constraints or {}).get("require_nodes", []))
    require_node = (constraints or {}).get("require_node")
    if require_node:
        required.add(require_node)
    if not required:
        return True
    return node.get("node_id") in required or node.get("platform") in required


def score_node(node, task_type):
    cold_start_penalty = 0 if task_type in node.get("ready_containers", []) else 1
    weights = ORACLE["weights"]
    return (
        weights["npu_used"] * node.get("npu_used", 1)
        + weights["cpu_used"] * node.get("cpu_used", 1)
        + weights["mem_used"] * node.get("mem_used", 1)
        + weights["cold_start_penalty"] * cold_start_penalty
    )


def build_execution_ground_truth(query, state, catalog_tasks):
    task_type = query["expected_task"]
    constraints = query.get("constraints", {})
    supported_nodes = set(catalog_tasks.get(task_type, {}).get("supported_nodes", []))
    candidates = []

    for node in state.get("nodes", []):
        node_id = node.get("node_id")
        if not node.get("online", False):
            continue
        if supported_nodes and node_id not in supported_nodes:
            continue
        if task_type not in node.get("support_tasks", []):
            continue
        if is_excluded(node, constraints):
            continue
        if not is_required(node, constraints):
            continue

        score = round(score_node(node, task_type), 4)
        candidates.append({
            "node_id": node_id,
            "platform": node.get("platform"),
            "score": score,
            "ready_container": task_type in node.get("ready_containers", []),
            "cpu_used": node.get("cpu_used"),
            "mem_used": node.get("mem_used"),
            "npu_used": node.get("npu_used")
        })

    ranking = sorted(candidates, key=lambda item: (item["score"], item["node_id"]))
    selected = ranking[0] if ranking else None
    expected_tools_alternatives = [
        ["get_cluster_resources", "get_task_catalog", "schedule_task", "run_task_on_node"],
        ["get_cluster_resources", "get_task_overhead", "schedule_task", "run_task_on_node"],
    ]
    return {
        "intent": "execute_task",
        "should_schedule": True,
        "expected_tools": expected_tools_alternatives[0],
        "expected_tools_alternatives": expected_tools_alternatives,
        "expected_task": task_type,
        "eligible_nodes": [item["node_id"] for item in ranking],
        "selected_node": selected["node_id"] if selected else None,
        "selected_platform": selected["platform"] if selected else None,
        "ranking": ranking,
        "oracle_score": selected["score"] if selected else None
    }


def resource_expected_tools(query_id):
    if query_id in {"q_res_004", "q_res_005"}:
        return ["get_cluster_resources", "get_task_overhead"]
    if query_id == "q_res_002":
        return ["get_cluster_resources", "get_task_catalog"]
    return ["get_cluster_resources"]


def build_resource_ground_truth(query):
    overhead_arguments = None
    if query["query_id"] == "q_res_004":
        overhead_arguments = {"task_type": "YoloV5", "device_type": "RK3588"}
    elif query["query_id"] == "q_res_005":
        overhead_arguments = {"task_type": "MobileNet", "device_type": "RK3588"}

    return {
        "intent": "resource_query",
        "should_schedule": False,
        "expected_tools": resource_expected_tools(query["query_id"]),
        "expected_tool_arguments": {"get_task_overhead": overhead_arguments} if overhead_arguments else {},
        "expected_task": "resource_query",
        "eligible_nodes": [],
        "selected_node": None,
        "selected_platform": None,
        "ranking": [],
        "oracle_score": None
    }


def build_cases():
    queries = load_queries()
    states = load_states()
    catalog_tasks = load_catalog_tasks()
    cases = []

    for state in states:
        for query in queries:
            expected_task = query.get("expected_task")
            if expected_task in EXECUTION_TASKS:
                ground_truth = build_execution_ground_truth(query, state, catalog_tasks)
            elif expected_task == "resource_query":
                ground_truth = build_resource_ground_truth(query)
            else:
                raise RuntimeError(f"unsupported expected_task: {expected_task}")

            cases.append({
                "case_id": f"{state['state_id']}_{query['query_id']}",
                "state_id": state["state_id"],
                "query_id": query["query_id"],
                "query": query["query"],
                "query_path": query["_path"],
                "state_path": state["_path"],
                "expected_task": expected_task,
                "constraints": query.get("constraints", {}),
                "ground_truth": ground_truth
            })

    return {
        "benchmark": "EdgeAgentBench",
        "version": "0.1",
        "oracle": ORACLE,
        "case_count": len(cases),
        "cases": cases
    }


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path in OUTPUT_DIR.glob("*.json"):
        path.unlink()

    payload = build_cases()
    for case in payload["cases"]:
        case_path = OUTPUT_DIR / f"{case['case_id']}.json"
        case_payload = {
            "benchmark": payload["benchmark"],
            "version": payload["version"],
            "oracle": payload["oracle"],
            **case,
        }
        case_path.write_text(json.dumps(case_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    manifest = {
        "benchmark": payload["benchmark"],
        "version": payload["version"],
        "oracle": payload["oracle"],
        "case_count": payload["case_count"],
        "case_files": [f"{case['case_id']}.json" for case in payload["cases"]],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {payload['case_count']} case files to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
