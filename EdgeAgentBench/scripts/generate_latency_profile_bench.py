#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any


BENCH_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BENCH_ROOT.parent
DEFAULT_PROFILE_PATH = REPO_ROOT / "config_files" / "latency_profiles.json"
QUERY_ROOT = BENCH_ROOT / "queries"
STATE_ROOT = BENCH_ROOT / "states"
CASE_ROOT = BENCH_ROOT / "cases"
MANIFEST_PATH = CASE_ROOT / "manifest.json"

MODEL_TO_TASK = {
    "YoloV5": "object_detection",
    "MobileNet": "image_classification",
    "ResNet50": "image_classification",
    "deeplabv3": "semantic_segmentation",
    "Bert": "text_classification",
}

TASK_QUERY_DIR = {
    "object_detection": "object_dedection",
    "image_classification": "image_classification",
    "semantic_segmentation": "semantic_segmentation",
    "text_classification": "text_classification",
}

def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def percent_to_ratio(value: float | int | None) -> float | None:
    if value is None:
        return None
    return round(float(value) / 100.0, 6)


def model_slug(model: str) -> str:
    return {
        "YoloV5": "yolov5",
        "MobileNet": "mobilenet",
        "ResNet50": "resnet50",
        "deeplabv3": "deeplabv3",
        "Bert": "bert",
    }.get(model, model.lower())


def load_profiles(profile_path: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    payload = load_json(profile_path)
    records = payload.get("records", [])
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        model = record.get("task_type")
        device_type = record.get("device_type")
        if model not in MODEL_TO_TASK or not device_type:
            continue
        if not isinstance(record.get("e2e_ms"), (int, float)):
            continue
        if not isinstance(record.get("avg_cpu_percent"), (int, float)):
            continue
        if not isinstance(record.get("avg_mem_percent"), (int, float)):
            continue
        if not isinstance(record.get("avg_npu_percent"), (int, float)):
            continue
        grouped.setdefault(model, {}).setdefault(device_type, []).append(record)

    for by_device in grouped.values():
        for records_for_device in by_device.values():
            records_for_device.sort(key=lambda item: (
                item.get("source", {}).get("row", 0),
                item.get("e2e_ms", 0),
            ))
    return grouped


def pick_record(records: list[dict[str, Any]], state_index: int, device_index: int) -> dict[str, Any]:
    # Prime-ish strides avoid pairing the same row offsets across devices every time.
    strides = [1, 7, 13, 17, 19]
    offsets = [0, 11, 23, 37, 53]
    stride = strides[device_index % len(strides)]
    offset = offsets[device_index % len(offsets)]
    return records[(state_index * stride + offset) % len(records)]


def build_states(
    grouped_profiles: dict[str, dict[str, list[dict[str, Any]]]],
    states_per_model: int,
    offline_every: int,
) -> list[dict[str, Any]]:
    states = []
    for model in sorted(grouped_profiles):
        by_device = grouped_profiles[model]
        devices = sorted(by_device)
        for state_index in range(states_per_model):
            offline_device = None
            if offline_every > 0 and (state_index + 1) % offline_every == 0 and devices:
                offline_device = devices[(state_index // offline_every) % len(devices)]
            nodes = []
            for device_index, device_type in enumerate(devices):
                record = pick_record(by_device[device_type], state_index, device_index)
                online = device_type != offline_device
                latency_profile = {
                    "model": model,
                    "device_type": device_type,
                    "e2e_ms": record.get("e2e_ms") if online else -1,
                    "exec_ms": record.get("exec_ms") if online else -1,
                }
                nodes.append({
                    "node_id": device_type,
                    "platform": device_type,
                    "online": online,
                    "cpu_used": percent_to_ratio(record.get("avg_cpu_percent")),
                    "mem_used": percent_to_ratio(record.get("avg_mem_percent")),
                    "npu_used": percent_to_ratio(record.get("avg_npu_percent")),
                    "network_latency": 1,
                    "latency_profile": latency_profile,
                })
            state_id = f"{model_slug(model)}_{state_index + 1:03d}"
            states.append({
                "state_id": state_id,
                "nodes": nodes,
            })
    return states


def load_execution_queries(task: str) -> list[dict[str, Any]]:
    query_dir = QUERY_ROOT / TASK_QUERY_DIR[task]
    queries = []
    for path in sorted(query_dir.glob("*.json")):
        query = load_json(path)
        queries.append(query)
    return queries


def is_excluded(node: dict[str, Any], constraints: dict[str, Any]) -> bool:
    excluded = set((constraints or {}).get("exclude_nodes", []))
    return node.get("node_id") in excluded or node.get("platform") in excluded


def is_required(node: dict[str, Any], constraints: dict[str, Any]) -> bool:
    required = set((constraints or {}).get("require_nodes", []))
    require_node = (constraints or {}).get("require_node")
    if require_node:
        required.add(require_node)
    if not required:
        return True
    return node.get("node_id") in required or node.get("platform") in required


def build_ground_truth(model: str, state: dict[str, Any], constraints: dict[str, Any]) -> dict[str, Any]:
    ranking = []
    for node in state.get("nodes", []):
        if not node.get("online", False):
            continue
        profile = node.get("latency_profile", {})
        if profile.get("model") != model:
            continue
        if is_excluded(node, constraints):
            continue
        if not is_required(node, constraints):
            continue
        if not isinstance(profile.get("e2e_ms"), (int, float)) or profile.get("e2e_ms") < 0:
            continue
        ranking.append({
            "node_id": node.get("node_id"),
            "platform": node.get("platform"),
            "e2e_ms": profile.get("e2e_ms"),
            "exec_ms": profile.get("exec_ms"),
            "cpu_used": node.get("cpu_used"),
            "mem_used": node.get("mem_used"),
            "npu_used": node.get("npu_used"),
        })
    ranking.sort(key=lambda item: (item["e2e_ms"], item["node_id"]))
    selected = ranking[0] if ranking else None
    if not selected:
        return {}
    return {
        "node_id": selected["node_id"],
        "ete_time": selected["e2e_ms"],
    }


def build_cases(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queries_by_task = {
        task: load_execution_queries(task)
        for task in sorted(set(MODEL_TO_TASK.values()))
    }
    cases = []
    for state in states:
        first_node = state.get("nodes", [{}])[0]
        model = first_node.get("latency_profile", {}).get("model")
        if not model:
            continue
        task = MODEL_TO_TASK[model]
        for query in queries_by_task[task]:
            constraints = query.get("constraints", {})
            ground_truth = build_ground_truth(model, state, constraints)
            if not ground_truth:
                continue
            case_id = f"{state['state_id']}_{query['query_id']}"
            cases.append({
                "case_id": case_id,
                "query": query["query"],
                "task_type": model,
                "nodes": state["nodes"],
                "constraints": constraints,
                "ground_truth": ground_truth
            })
    return cases


def clear_generated_json(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for path in directory.glob("*.json"):
        path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate EdgeAgentBench from latency_profiles.json.")
    parser.add_argument("--profile", default=str(DEFAULT_PROFILE_PATH))
    parser.add_argument("--states-per-model", type=int, default=500)
    parser.add_argument(
        "--offline-every",
        type=int,
        default=5,
        help="Mark one node offline every N generated states per model. Use 0 to disable.",
    )
    args = parser.parse_args()

    grouped_profiles = load_profiles(Path(args.profile))
    states = build_states(grouped_profiles, args.states_per_model, args.offline_every)
    cases = build_cases(states)

    clear_generated_json(STATE_ROOT)
    clear_generated_json(CASE_ROOT)

    for state in states:
        write_json(STATE_ROOT / f"{state['state_id']}.json", state)
    for case in cases:
        write_json(
            CASE_ROOT / f"{case['case_id']}.json",
            case,
        )

    manifest = {
        "benchmark": "EdgeAgentBench",
        "state_count": len(states),
        "case_count": len(cases),
        "states_per_model": args.states_per_model,
        "offline_every": args.offline_every,
        "case_files": [f"{case['case_id']}.json" for case in cases],
    }
    write_json(MANIFEST_PATH, manifest)

    print(f"wrote {len(states)} latency-profile states to {STATE_ROOT}")
    print(f"wrote {len(cases)} latency-profile case files to {CASE_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
