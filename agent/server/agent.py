import argparse
import contextvars
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from flask import Flask, Response, jsonify, request


app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.normpath(os.path.join(BASE_DIR, ".."))
REPO_ROOT = os.path.normpath(os.path.join(AGENT_DIR, ".."))
DEFAULT_MCP_SERVER_PATH = os.environ.get(
    "MCP_SERVER_PATH",
    os.path.join(AGENT_DIR, "tools", "mcp_server.py"),
)
DEFAULT_AGENT_INFO_PATH = os.path.join(REPO_ROOT, "config_files", "agent_info.json")
DEFAULT_STATIC_INFO_PATH = os.path.join(REPO_ROOT, "config_files", "static_info.json")
DEFAULT_DISPATCH_HISTORY_PATH = os.path.join(REPO_ROOT, "config_files", "dispatch_history.json")
DISPATCH_HISTORY_ENABLED = os.environ.get("DISPATCH_HISTORY_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
HISTORY_RESOURCE_TOLERANCE_PERCENT = float(os.environ.get("DISPATCH_HISTORY_TOLERANCE_PERCENT", "1.0"))

lock = threading.Lock()
is_blocking = False
LLM_API_URL = os.environ.get("LLM_API_URL", "http://127.0.0.1:8081/v1/chat/completions")
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "rkllm-default")
TIMING_LOG_ENABLED = contextvars.ContextVar("TIMING_LOG_ENABLED", default=False)


def start_timing():
    return {
        "start_time": datetime.now().isoformat(timespec="milliseconds"),
        "start_perf": time.perf_counter(),
    }


def finish_timing(marker):
    return {
        "start_time": marker["start_time"],
        "end_time": datetime.now().isoformat(timespec="milliseconds"),
        "elapsed_ms": round((time.perf_counter() - marker["start_perf"]) * 1000, 3),
    }


def log_timing(segment, **fields):
    if not TIMING_LOG_ENABLED.get():
        return
    payload = {"segment": segment, **fields}
    print(f"[TIMING] {json.dumps(payload, ensure_ascii=False)}", flush=True)


def post_json(url, payload, timeout=180):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM API returned HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"failed to reach LLM API at {url}: {exc.reason}") from exc


def call_llm(messages, timing_label="model_inference"):
    print(f"[INFO] LLM CALLED {timing_label}", flush=True)
    timing_marker = start_timing()
    raw = post_json(
        LLM_API_URL,
        {
            "model": LLM_MODEL_NAME,
            "messages": messages,
            "stream": False,
        },
    )
    timing = finish_timing(timing_marker)
    log_timing(timing_label, **timing)
    payload = json.loads(raw)
    choices = payload.get("choices", [])
    if not choices:
        raise RuntimeError("LLM API returned no choices")
    message = choices[0].get("message", {})
    content = message.get("content", "")
    if not content:
        raise RuntimeError("LLM API returned empty content")
    usage = payload.get("usage", {})
    return {
        "content": content,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "timing": {"segment": timing_label, **timing},
    }


def call_mcp_server(requests, timeout=60):
    process = subprocess.Popen(
        [sys.executable, DEFAULT_MCP_SERVER_PATH],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    request_payload = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests)

    try:
        stdout, stderr = process.communicate(request_payload, timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        raise RuntimeError("MCP request timed out")

    if process.returncode != 0:
        raise RuntimeError(f"MCP server exited with code {process.returncode}: {stderr.strip()}")

    responses = []
    for line in stdout.splitlines():
        line = line.strip()
        if line:
            responses.append(json.loads(line))
    return responses


def call_mcp_tool(tool_name, arguments=None, timeout=60):
    print(f"[INFO] {tool_name} TOOL CALLED", flush=True)
    responses = call_mcp_server(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool_name, "arguments": arguments or {}}},
        ],
        timeout=timeout,
    )
    for item in responses:
        if item.get("id") != 2:
            continue
        result = item.get("result", {})
        if result.get("isError"):
            content = result.get("content", [])
            if content and isinstance(content[0], dict):
                raise RuntimeError(content[0].get("text", "MCP tool returned an error"))
            raise RuntimeError("MCP tool returned an error")
        content = result.get("content", [])
        if not content or not isinstance(content[0], dict):
            raise RuntimeError("MCP tool returned empty content")
        return content[0].get("text", "")
    raise RuntimeError("MCP tool did not return a tools/call response")


def parse_json_object(text):
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1]).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start == -1:
            raise
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(cleaned)):
            ch = cleaned[index]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == "\"":
                    in_string = False
                continue
            if ch == "\"":
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(cleaned[start:index + 1])
        raise


def extract_text_fields_from_form(form_data):
    text_fields = {}
    for key in form_data.keys():
        value = form_data.get(key)
        if value is not None and value.strip():
            text_fields[key] = value.strip()
    return text_fields


def choose_user_text(text_fields):
    preferred_keys = ["text", "prompt", "query", "message", "instruction"]
    for key in preferred_keys:
        value = text_fields.get(key)
        if value:
            return value
    if text_fields:
        return "\n".join(f"{key}: {value}" for key, value in text_fields.items())
    raise RuntimeError("no non-empty text field found in multipart form data")


def parse_bool_form_value(value):
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def save_uploaded_file(uploaded_file):
    suffix = os.path.splitext(uploaded_file.filename or "")[1] or ".bin"
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    uploaded_file.save(temp_file.name)
    temp_file.close()
    return temp_file.name


def load_agent_info():
    config_path = os.environ.get("AGENT_INFO_PATH", DEFAULT_AGENT_INFO_PATH)
    try:
        with open(config_path, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except OSError as exc:
        raise RuntimeError(f"failed to read agent config {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in agent config {config_path}: {exc}") from exc

    agent_info = payload.get("agent")
    if not isinstance(agent_info, dict):
        raise RuntimeError("agent_info.json must contain an object field named 'agent'")
    tools = agent_info.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise RuntimeError("agent_info.json agent.tools must be a non-empty object")
    return agent_info


def route_by_rules(user_text, has_file):
    normalized_text = user_text.lower()
    wants_text_model = bool(re.search(r"文本分类|句子分类|语义匹配|文本理解|文本.*类别|这段文本|这句话.*类别|classify text|text classification|bert", normalized_text))
    wants_segmentation = bool(re.search(r"语义分割|图片分割|图像分割|分割|segmentation|segment", normalized_text))
    wants_detection = bool(re.search(r"目标检测|物体检测|检测|框|detect|detection|object", normalized_text))
    wants_image_classification = bool(re.search(r"图片分类|图像分类|照片分类|分类|类别|属于什么|classify|classification", normalized_text))

    if wants_text_model and not has_file:
        return {"route": "execute_tool", "task_type": "Bert", "reason": "matched text model request", "_usage": zero_usage()}
    if wants_segmentation:
        return {
            "route": "execute_tool" if has_file else "unsupported_task",
            "task_type": "deeplabv3" if has_file else "",
            "reason": "matched image segmentation request" if has_file else "image segmentation requires an uploaded image",
            "_usage": zero_usage(),
        }
    if has_file and wants_detection:
        return {"route": "execute_tool", "task_type": "YoloV5", "reason": "matched image detection request", "_usage": zero_usage()}
    if has_file and wants_image_classification:
        return {"route": "execute_tool", "task_type": "MobileNet", "reason": "matched image classification request", "_usage": zero_usage()}
    return None


def zero_usage():
    return {"prompt_tokens": 0, "completion_tokens": 0}


def redact_large_binary_fields(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key.endswith("_b64") or key in {"binary_b64", "image_b64"}:
                redacted[key] = "<binary data omitted>"
            else:
                redacted[key] = redact_large_binary_fields(item)
        return redacted
    if isinstance(value, list):
        return [redact_large_binary_fields(item) for item in value]
    return value


def build_deterministic_summary(selected_execution, tool_result):
    task_type = selected_execution.get("task_type")
    exec_time = tool_result.get("exec_time") or tool_result.get("gateway_time") if isinstance(tool_result, dict) else None
    results = tool_result.get("results") if isinstance(tool_result, dict) else None

    if task_type in {"MobileNet", "ResNet50"} and isinstance(results, list) and results:
        top_result = results[0]
        if isinstance(top_result, dict) and top_result.get("class"):
            confidence = top_result.get("confidence")
            parts = [f"图像分类结果：最可能是 {top_result.get('class')}"]
            if isinstance(confidence, (int, float)):
                parts.append(f"置信度 {confidence * 100:.2f}%")
            if isinstance(exec_time, (int, float)):
                parts.append(f"耗时 {exec_time:.2f} ms")
            parts.append(f"使用模型：{task_type}")
            return "，".join(parts) + "。"

    if task_type == "YoloV5" and isinstance(results, list):
        parts = [f"目标检测完成，共检测到 {len(results)} 个结果"]
        if isinstance(exec_time, (int, float)):
            parts.append(f"耗时 {exec_time:.2f} ms")
        parts.append("使用模型：YoloV5")
        return "，".join(parts) + "。"

    if task_type == "deeplabv3" and isinstance(tool_result, dict):
        return "图像分割任务已完成，使用模型：deeplabv3。"

    return None


ACTION_TIMEOUTS = {
    "get_dispatch_snapshot": 30,
    "get_task_catalog": 30,
    "get_cluster_resources": 30,
    "run_task_on_node": 150,
}


def infer_task_type_by_rules(user_text, has_file):
    route = route_by_rules(user_text, has_file)
    if route and route.get("route") == "execute_tool":
        return route.get("task_type")
    return None


def compact_agent_context(context):
    compact = {}
    for key, value in context.items():
        if key == "input_path":
            compact[key] = "<uploaded_file>" if value else None
        elif key in {"cluster_resources", "task_catalog"}:
            compact[key] = value
        elif key == "tool_result":
            compact[key] = redact_large_binary_fields(value)
        else:
            compact[key] = value
    return compact


def get_task_catalog_entry(task_type, context):
    task_catalog = context.get("task_catalog")
    if isinstance(task_catalog, dict):
        for item in task_catalog.get("result", []):
            if isinstance(item, dict) and item.get("task_type") == task_type:
                return item
    return {}


def get_configured_tool_metadata(task_type):
    return load_agent_info().get("tools", {}).get(task_type, {})


def validate_task_input_requirements(task_type, has_file, context=None):
    catalog_entry = get_task_catalog_entry(task_type, context or {})
    tool_metadata = catalog_entry or get_configured_tool_metadata(task_type)
    if tool_metadata.get("requires_file") and not has_file:
        raise RuntimeError(f"{task_type} requires an uploaded image/file")


def get_execution_metadata(task_type, context):
    catalog_entry = get_task_catalog_entry(task_type, context)
    configured_metadata = get_configured_tool_metadata(task_type)
    return {
        "real_url": catalog_entry.get("real_url") or configured_metadata.get("real_url") or "predict",
        "file_field_name": catalog_entry.get("file_field_name") or configured_metadata.get("file_field_name") or "image",
    }


def cluster_nodes(cluster_resources):
    if isinstance(cluster_resources, dict):
        nodes = cluster_resources.get("result", [])
    else:
        nodes = cluster_resources
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


def online_cluster_nodes(cluster_resources):
    return [
        node
        for node in cluster_nodes(cluster_resources)
        if node.get("online", True) is not False
    ]


def node_global_id(node):
    return node.get("global_id") or node.get("target_global_id") or node.get("node_id")


def available_device_types_from_cluster(cluster_resources):
    return sorted(
        {
            node.get("type")
            for node in online_cluster_nodes(cluster_resources)
            if node.get("type")
        }
    )


def task_types_from_catalog(task_catalog):
    if not isinstance(task_catalog, dict):
        return set()
    return {
        item.get("task_type")
        for item in task_catalog.get("result", [])
        if isinstance(item, dict) and item.get("task_type")
    }


def compact_task_catalog_for_selection(task_catalog):
    if not isinstance(task_catalog, dict):
        return []
    result = []
    for item in task_catalog.get("result", []):
        if not isinstance(item, dict):
            continue
        result.append({
            "task_type": item.get("task_type"),
            "requires_file": item.get("requires_file", False),
            "capabilities": item.get("capabilities", []),
        })
    return result


def load_static_info():
    config_path = os.environ.get("STATIC_INFO_PATH", DEFAULT_STATIC_INFO_PATH)
    try:
        with open(config_path, "r", encoding="utf-8") as file_obj:
            return json.load(file_obj)
    except OSError as exc:
        raise RuntimeError(f"failed to read static info {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON in static info {config_path}: {exc}") from exc


def resource_value_percent(resource, *keys):
    if not isinstance(resource, dict):
        return None
    for key in keys:
        value = resource.get(key)
        if isinstance(value, (int, float)):
            percent = value * 100 if value <= 1 else value
            return round(percent, 3)
    return None


def node_resource_percent(node):
    resource = node.get("resource") if isinstance(node, dict) else None
    return {
        "cpu_percent": resource_value_percent(resource, "cpu_used", "cpu"),
        "mem_percent": resource_value_percent(resource, "mem_used", "mem"),
        "npu_percent": resource_value_percent(resource, "npu_used", "xpu_used", "npu", "xpu"),
    }


def ratio_to_percent(value):
    if not isinstance(value, (int, float)):
        return None
    return round(value * 100, 3)


def task_device_base_latency_ms(static_info, task_type, device_type):
    task_entry = static_info.get(task_type, {}) if isinstance(static_info, dict) else {}
    device_entry = task_entry.get(device_type, {}) if isinstance(task_entry, dict) else {}
    overhead = device_entry.get("taskOverhead", {}) if isinstance(device_entry, dict) else {}
    value = overhead.get("base_latency_ms")
    return round(value, 3) if isinstance(value, (int, float)) else None


def dispatch_history_path():
    return os.environ.get("DISPATCH_HISTORY_PATH", DEFAULT_DISPATCH_HISTORY_PATH)


def load_dispatch_history():
    path = dispatch_history_path()
    if not os.path.exists(path):
        return {"version": 2, "by_task": {}}
    try:
        with open(path, "r", encoding="utf-8") as file_obj:
            payload = json.load(file_obj)
    except (OSError, json.JSONDecodeError):
        return {"version": 2, "by_task": {}}
    if not isinstance(payload, dict):
        return {"version": 2, "by_task": {}}
    if isinstance(payload.get("by_task"), dict):
        payload.setdefault("version", 2)
        return payload
    if isinstance(payload.get("records"), list):
        migrated = {"version": 2, "by_task": {}}
        for record in payload["records"]:
            if not isinstance(record, dict):
                continue
            task_type = record.get("task_type")
            device_type = record.get("device_type")
            if not task_type or not device_type:
                continue
            migrated["by_task"].setdefault(task_type, {}).setdefault(device_type, []).append(record)
        return migrated
    return {"version": 2, "by_task": {}}


def write_dispatch_history(payload):
    path = dispatch_history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2)
        file_obj.write("\n")
    os.replace(tmp_path, path)


def history_resource_distance(current, record_resource):
    distances = []
    for key in ("cpu_percent", "mem_percent", "npu_percent"):
        current_value = current.get(key) if isinstance(current, dict) else None
        record_value = record_resource.get(key) if isinstance(record_resource, dict) else None
        if not isinstance(current_value, (int, float)) or not isinstance(record_value, (int, float)):
            return None
        distances.append(abs(current_value - record_value))
    return {
        "max_abs_percent": round(max(distances), 3),
        "avg_abs_percent": round(sum(distances) / len(distances), 3),
    }


def nearest_history_record(task_type, candidate, history, tolerance_percent=HISTORY_RESOURCE_TOLERANCE_PERCENT):
    current = candidate.get("current_resource_percent", {})
    target_global_id = candidate.get("target_global_id")
    device_type = candidate.get("device_type")
    matches = []
    records = (
        history.get("by_task", {})
        .get(task_type, {})
        .get(device_type, [])
    )
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("target_global_id") not in {None, target_global_id}:
            continue
        ete_ms = record.get("ete_ms")
        if not isinstance(ete_ms, (int, float)):
            continue
        distance = history_resource_distance(current, record.get("resource_percent", {}))
        if not distance or distance["max_abs_percent"] > tolerance_percent:
            continue
        matches.append((distance["max_abs_percent"], distance["avg_abs_percent"], record))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], item[1], -float(item[2].get("timestamp_ms", 0))))
    distance = {
        "max_abs_percent": matches[0][0],
        "avg_abs_percent": matches[0][1],
    }
    return {**matches[0][2], "distance": distance}


def build_history_dispatch_context(selected_task_type, candidate_nodes):
    if not DISPATCH_HISTORY_ENABLED:
        return None
    history = load_dispatch_history()
    matched_nodes = []
    matched_count = 0
    for candidate in candidate_nodes:
        matched = nearest_history_record(selected_task_type, candidate, history)
        if matched:
            matched_count += 1
            matched_nodes.append([
                candidate.get("target_global_id"),
                round(matched["ete_ms"], 3),
            ])
    if matched_count != len(candidate_nodes):
        return None
    return {
        "task_type": selected_task_type,
        "history_policy": "choose min latency_ms",
        "history_coverage": "full",
        "columns": ["id", "latency_ms"],
        "nodes": matched_nodes,
    }


def compact_dispatch_node(candidate):
    current = candidate.get("current_resource_percent", {}) if isinstance(candidate, dict) else {}
    base_latency_ms = candidate.get("base_latency_ms")
    if not isinstance(base_latency_ms, (int, float)):
        base_latency_ms = 0
    return [
        candidate.get("target_global_id"),
        round(base_latency_ms, 3),
        round(current.get("cpu_percent"), 3) if isinstance(current.get("cpu_percent"), (int, float)) else 100,
        round(current.get("npu_percent"), 3) if isinstance(current.get("npu_percent"), (int, float)) else 100,
        round(current.get("mem_percent"), 3) if isinstance(current.get("mem_percent"), (int, float)) else 100,
    ]


def candidate_nodes_for_task(cluster_resources, task_catalog, task_type, static_info):
    catalog_entry = get_task_catalog_entry(task_type, {"task_catalog": task_catalog})
    supported_device_types = set(catalog_entry.get("supported_device_types") or [])
    candidates = []
    for node in online_cluster_nodes(cluster_resources):
        target_global_id = node_global_id(node)
        device_type = node.get("type")
        if not target_global_id or not device_type:
            continue
        if supported_device_types and device_type not in supported_device_types:
            continue
        current_percent = node_resource_percent(node)
        candidate = {
            "target_global_id": target_global_id,
            "device_type": device_type,
            "base_latency_ms": task_device_base_latency_ms(static_info, task_type, device_type),
            "current_resource_percent": current_percent,
        }
        candidates.append(candidate)
    return candidates


def build_dispatch_context(selected_task_type, candidate_nodes):
    return {
        "task_type": selected_task_type,
        "policy": "minimize 8*base_latency + 12*cpu + 20*npu + 100*mem",
        "columns": ["id", "base_latency", "cpu", "npu", "mem"],
        "nodes": [compact_dispatch_node(candidate) for candidate in candidate_nodes],
    }


def build_direct_task_type_messages(user_text, has_file, text_fields, task_catalog):
    task_context = {
        "user_request": {
            "text": user_text,
            "fields": text_fields,
            "has_file": has_file,
        },
        "task_catalog": compact_task_catalog_for_selection(task_catalog),
    }
    return [
        {
            "role": "system",
            "content": (
                "Select one executable task_type from task_catalog for the user request.\n"
                "Return JSON only: {\"task_type\":\"<catalog task_type>\"}. "
                "Use only listed task types; if has_file=false avoid file-required tasks."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(task_context, ensure_ascii=False),
        },
    ]


def choose_direct_task_type(user_text, has_file, text_fields, task_catalog):
    model_result = call_llm(
        build_direct_task_type_messages(user_text, has_file, text_fields, task_catalog),
        timing_label="model_inference.task_type_selection",
    )
    try:
        selection = parse_json_object(model_result["content"])
    except Exception as exc:
        raise RuntimeError(f"task type model returned invalid JSON: {exc}") from exc

    task_type = selection.get("task_type")
    if task_type not in task_types_from_catalog(task_catalog):
        raise RuntimeError(f"model selected unavailable task_type: {task_type}")
    validate_task_input_requirements(task_type, has_file, {"task_catalog": task_catalog})
    return task_type, "", model_result


def resolve_direct_task_type(user_text, has_file, text_fields, task_catalog, forced_task_type=None):
    if forced_task_type:
        validate_task_input_requirements(forced_task_type, has_file, {"task_catalog": task_catalog})
        print(f"[INFO] TASK_TYPE_SELECTED {forced_task_type} source=forced", flush=True)
        return forced_task_type, "forced by request metadata", zero_usage(), "forced"

    inferred_task_type = infer_task_type_by_rules(user_text, has_file)
    if inferred_task_type and inferred_task_type in task_types_from_catalog(task_catalog):
        validate_task_input_requirements(inferred_task_type, has_file, {"task_catalog": task_catalog})
        print(f"[INFO] TASK_TYPE_SELECTED {inferred_task_type} source=rules", flush=True)
        return inferred_task_type, "matched local task routing rules", zero_usage(), "rules"

    task_type, reason, usage = choose_direct_task_type(user_text, has_file, text_fields, task_catalog)
    print(f"[INFO] TASK_TYPE_SELECTED {task_type} source=model", flush=True)
    return task_type, reason, usage, "model"


def build_direct_selection_messages(
    dispatch_context,
):
    selected_task_type = dispatch_context.get("task_type", "")
    if dispatch_context.get("history_policy"):
        system_content = (
            f"Task type is fixed: {selected_task_type}. Choose the node with the smallest latency_ms.\n"
            "Use only latency_ms values in user.nodes; do not use resources or formulas.\n"
            "Return JSON only: {\"task_type\":\"<task_type>\",\"target_global_id\":\"<id>\"}."
        )
    else:
        system_content = (
            f"Task type is fixed: {selected_task_type}. Choose one node by minimizing policy.\n"
            "You must calculate with the exact formula in user.policy and select the smallest value.\n"
            "Return JSON only: {\"task_type\":\"<task_type>\",\"target_global_id\":\"<id>\"}."
        )

    return [
        {
            "role": "system",
            "content": system_content,
        },
        {
            "role": "user",
            "content": json.dumps(dispatch_context, ensure_ascii=False),
        },
    ]


def choose_direct_execution(
    has_file,
    cluster_resources,
    task_catalog,
    dispatch_context,
):
    model_result = call_llm(
        build_direct_selection_messages(
            dispatch_context,
        ),
        timing_label="model_inference.direct_selection",
    )
    try:
        selection = parse_json_object(model_result["content"])
    except Exception as exc:
        raise RuntimeError(f"direct selection model returned invalid JSON: {exc}") from exc

    task_type = selection.get("task_type")
    target_global_id = selection.get("target_global_id")
    selected_task_type = dispatch_context.get("task_type")
    if selected_task_type and task_type != selected_task_type:
        raise RuntimeError(f"model selected task_type {task_type}, but selected task_type is {selected_task_type}")
    if task_type not in task_types_from_catalog(task_catalog):
        raise RuntimeError(f"model selected unavailable task_type: {task_type}")

    nodes_by_id = {
        node_global_id(node): node
        for node in online_cluster_nodes(cluster_resources)
        if node_global_id(node)
    }
    selected_node = nodes_by_id.get(target_global_id)
    if not selected_node:
        raise RuntimeError(f"model selected unavailable target_global_id: {target_global_id}")

    candidate_ids = set()
    for node in dispatch_context.get("nodes", []):
        if isinstance(node, list) and node:
            candidate_ids.add(node[0])
    if target_global_id not in candidate_ids:
        raise RuntimeError(f"model selected target_global_id outside dispatch nodes: {target_global_id}")

    catalog_entry = get_task_catalog_entry(task_type, {"task_catalog": task_catalog})
    supported_device_types = set(catalog_entry.get("supported_device_types") or [])
    node_type = selected_node.get("type")
    if supported_device_types and node_type not in supported_device_types:
        raise RuntimeError(f"task_type {task_type} does not support selected node type {node_type}")
    validate_task_input_requirements(task_type, has_file, {"task_catalog": task_catalog})

    return {
        "task_type": task_type,
        "target_global_id": target_global_id,
        "selected_node": selected_node,
        "reason": "",
        "_usage": model_result,
    }


def choose_single_candidate_execution(task_type, candidate_nodes, cluster_resources):
    if len(candidate_nodes) != 1:
        raise RuntimeError("single candidate selection requires exactly one candidate node")
    target_global_id = candidate_nodes[0].get("target_global_id")
    selected_node = {
        node_global_id(node): node
        for node in online_cluster_nodes(cluster_resources)
        if node_global_id(node)
    }.get(target_global_id)
    if not selected_node:
        raise RuntimeError(f"single candidate node is unavailable: {target_global_id}")
    print(
        f"[INFO] NODE_SELECTION_SKIPPED_LLM task_type={task_type} "
        f"target_global_id={target_global_id} reason=single_candidate",
        flush=True,
    )
    return {
        "task_type": task_type,
        "target_global_id": target_global_id,
        "selected_node": selected_node,
        "reason": "only one online candidate node supports the selected task",
        "_usage": zero_usage(),
        "_selection_source": "single_candidate",
    }


def extract_ete_ms(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("ete_ms", "ete_time", "e2e_ms", "end_to_end_ms", "gateway_time", "total_time_ms"):
        value = payload.get(key)
        if isinstance(value, (int, float)):
            return round(value, 3)
    for key in ("result", "results", "data"):
        value = payload.get(key)
        if isinstance(value, dict):
            nested = extract_ete_ms(value)
            if nested is not None:
                return nested
    return None


def record_dispatch_history(task_type, target_global_id, selected_node, candidate_nodes, tool_result):
    ete_ms = extract_ete_ms(tool_result)
    if ete_ms is None:
        return None
    candidate = next(
        (
            item for item in candidate_nodes
            if item.get("target_global_id") == target_global_id
        ),
        None,
    )
    if not candidate:
        return None
    resource_percent = candidate.get("current_resource_percent", {})
    record = {
        "timestamp_ms": int(time.time() * 1000),
        "task_type": task_type,
        "target_global_id": target_global_id,
        "device_type": selected_node.get("type") if isinstance(selected_node, dict) else candidate.get("device_type"),
        "resource_percent": {
            "cpu_percent": round(resource_percent.get("cpu_percent"), 3) if isinstance(resource_percent.get("cpu_percent"), (int, float)) else None,
            "mem_percent": round(resource_percent.get("mem_percent"), 3) if isinstance(resource_percent.get("mem_percent"), (int, float)) else None,
            "npu_percent": round(resource_percent.get("npu_percent"), 3) if isinstance(resource_percent.get("npu_percent"), (int, float)) else None,
        },
        "ete_ms": ete_ms,
    }
    payload = load_dispatch_history()
    records = (
        payload.setdefault("by_task", {})
        .setdefault(task_type, {})
        .setdefault(record["device_type"], [])
    )
    for existing in records:
        if not isinstance(existing, dict):
            continue
        if existing.get("resource_percent") != record["resource_percent"]:
            continue
        existing.update({
            "timestamp_ms": record["timestamp_ms"],
            "task_type": record["task_type"],
            "target_global_id": record["target_global_id"],
            "device_type": record["device_type"],
            "ete_ms": record["ete_ms"],
        })
        write_dispatch_history(payload)
        return {**existing, "updated": True}
    records.append(record)
    max_records = int(os.environ.get("DISPATCH_HISTORY_MAX_RECORDS", "5000"))
    if max_records > 0:
        records[:] = records[-max_records:]
    write_dispatch_history(payload)
    return record


def build_direct_final_message(selected_execution, tool_result):
    deterministic_summary = build_deterministic_summary(selected_execution, tool_result)
    if deterministic_summary:
        return deterministic_summary
    task_type = selected_execution.get("task_type", "unknown")
    if isinstance(tool_result, dict):
        result_payload = tool_result.get("result")
        if result_payload is None:
            result_payload = tool_result.get("results")
        if result_payload is not None:
            if isinstance(result_payload, str):
                return f"任务已完成，使用模型：{task_type}，结果：{result_payload}"
            return f"任务已完成，使用模型：{task_type}，结果：{json.dumps(redact_large_binary_fields(result_payload), ensure_ascii=False)}"
    status = tool_result.get("status") if isinstance(tool_result, dict) else None
    if status:
        return f"任务已完成，使用模型：{task_type}，执行状态：{status}。"
    return f"任务已完成，使用模型：{task_type}。"


def run_direct_agent_execution(user_text, has_file, input_path, text_fields, forced_task_type=None, debug_response=False):
    e2e_marker = start_timing()
    total_usage = zero_usage()
    trace = []

    if forced_task_type:
        if not get_configured_tool_metadata(forced_task_type):
            raise RuntimeError(f"unsupported task_type: {forced_task_type}")
        validate_task_input_requirements(forced_task_type, has_file)

    snapshot_timing_marker = start_timing()
    dispatch_snapshot = json.loads(call_mcp_tool("get_dispatch_snapshot", timeout=ACTION_TIMEOUTS["get_dispatch_snapshot"]))
    snapshot_timing = finish_timing(snapshot_timing_marker)
    log_timing("tool.get_dispatch_snapshot", **snapshot_timing)
    if dispatch_snapshot.get("status") != "success":
        raise RuntimeError(f"get_dispatch_snapshot failed: {dispatch_snapshot}")
    cluster_resources = dispatch_snapshot.get("cluster_resources")
    task_catalog = dispatch_snapshot.get("task_catalog")
    if not isinstance(cluster_resources, dict) or not isinstance(task_catalog, dict):
        raise RuntimeError(f"get_dispatch_snapshot returned invalid payload: {dispatch_snapshot}")
    trace.append({
        "action": "get_dispatch_snapshot",
        "arguments": {},
        "tool_timing": {"segment": "tool.get_dispatch_snapshot", **snapshot_timing},
        "result": {
            "cluster_resources": redact_large_binary_fields(cluster_resources),
            "task_catalog": task_catalog,
        },
        "generated_by_model": False,
    })

    selected_task_type, task_reason, task_usage, task_selection_source = resolve_direct_task_type(
        user_text,
        has_file,
        text_fields,
        task_catalog,
        forced_task_type,
    )
    total_usage["prompt_tokens"] += task_usage.get("prompt_tokens", 0)
    total_usage["completion_tokens"] += task_usage.get("completion_tokens", 0)
    task_trace_item = {
        "action": "select_task_type",
        "arguments": {},
        "result": {"task_type": selected_task_type},
        "reason": task_reason,
        "selection_source": task_selection_source,
        "generated_by_model": task_selection_source == "model",
    }
    if task_usage.get("timing"):
        task_trace_item["model_timing"] = task_usage["timing"]
    trace.append(task_trace_item)

    static_info = load_static_info()
    candidate_nodes = candidate_nodes_for_task(
        cluster_resources,
        task_catalog,
        selected_task_type,
        static_info,
    )
    if not candidate_nodes:
        raise RuntimeError(f"no online candidate nodes support task_type: {selected_task_type}")

    history_dispatch_context = build_history_dispatch_context(selected_task_type, candidate_nodes)
    dispatch_context = history_dispatch_context or build_dispatch_context(
        selected_task_type,
        candidate_nodes,
    )
    if len(candidate_nodes) == 1:
        selection = choose_single_candidate_execution(
            selected_task_type,
            candidate_nodes,
            cluster_resources,
        )
    else:
        selection = choose_direct_execution(
            has_file,
            cluster_resources,
            task_catalog,
            dispatch_context,
        )
    selection_usage = selection.pop("_usage")
    node_selection_source = selection.pop("_selection_source", "model")
    total_usage["prompt_tokens"] += selection_usage.get("prompt_tokens", 0)
    total_usage["completion_tokens"] += selection_usage.get("completion_tokens", 0)

    execution_metadata = get_execution_metadata(selection["task_type"], {"task_catalog": task_catalog})
    fields = dict(text_fields)
    fields.setdefault("text", user_text)
    run_arguments = {
        "task_type": selection["task_type"],
        "target_global_id": selection["target_global_id"],
        "input_path": input_path,
        "real_url": execution_metadata["real_url"],
        "file_field_name": execution_metadata["file_field_name"],
        "fields": fields,
    }

    run_timing_marker = start_timing()
    tool_result = json.loads(call_mcp_tool("run_task_on_node", run_arguments, timeout=ACTION_TIMEOUTS["run_task_on_node"]))
    run_timing = finish_timing(run_timing_marker)
    log_timing("tool.run_task_on_node", **run_timing)
    history_record = record_dispatch_history(
        selection["task_type"],
        selection["target_global_id"],
        selection["selected_node"],
        candidate_nodes,
        tool_result,
    )
    selected_execution = {
        "task_type": selection["task_type"],
        "target_global_id": selection["target_global_id"],
        "scheduled_node": selection["selected_node"],
        "real_url": execution_metadata["real_url"],
        "history_recorded": history_record is not None,
    }
    trace.append({
        "action": "run_task_on_node",
        "arguments": run_arguments,
        "reason": selection.get("reason", ""),
        "selection_source": node_selection_source,
        "model_timing": selection_usage.get("timing"),
        "tool_timing": {"segment": "tool.run_task_on_node", **run_timing},
        "history_record": history_record,
        "result": redact_large_binary_fields(tool_result),
        "generated_by_model": node_selection_source == "model",
    })

    context = {
        "has_file": has_file,
        "input_path": input_path,
        "cluster_resources": cluster_resources,
        "task_catalog": task_catalog,
        "selected_task_type": selected_task_type,
        "candidate_nodes": candidate_nodes,
        "dispatch_context": dispatch_context,
        "selected_execution": selected_execution,
        "tool_result": tool_result,
        "history_record": history_record,
    }
    total_usage["total_tokens"] = total_usage["prompt_tokens"] + total_usage["completion_tokens"]
    e2e_timing = finish_timing(e2e_marker)
    total_usage["timing"] = {"segment": "agent_e2e", **e2e_timing}
    log_timing("agent_e2e", **e2e_timing)
    return build_direct_final_message(selected_execution, tool_result), context, trace, total_usage


def get_forced_task_type(text_fields):
    forced_task_type = (text_fields.get("task_type") or text_fields.get("tool") or "").strip()
    return forced_task_type or None


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "object": "single_agent.health"})


@app.route("/v1/agent/chat", methods=["POST"])
def agent_chat():
    global is_blocking

    if is_blocking:
        return jsonify({
            "error": {
                "message": "Single agent is busy! Please try again later.",
                "type": "server_error",
                "param": None,
                "code": None,
            }
        }), 503

    uploaded_file = request.files.get("image") or request.files.get("file")
    has_file = uploaded_file is not None and bool(uploaded_file.filename)
    text_fields = extract_text_fields_from_form(request.form)
    debug_response = parse_bool_form_value(text_fields.pop("debug", None))
    timing_log_response = parse_bool_form_value(text_fields.pop("timing", None))

    if not has_file and not text_fields:
        return jsonify({
            "error": {
                "message": "multipart form must include an image/file field or a non-empty text field",
                "type": "invalid_request_error",
                "param": None,
                "code": None,
            }
        }), 400

    try:
        user_text = choose_user_text(text_fields)
        forced_task_type = get_forced_task_type(text_fields)
    except RuntimeError as exc:
        return jsonify({
            "error": {
                "message": str(exc),
                "type": "invalid_request_error",
                "param": "text",
                "code": None,
            }
        }), 400

    lock.acquire()
    temp_input_path = None
    timing_log_token = None
    try:
        is_blocking = True
        timing_log_token = TIMING_LOG_ENABLED.set(timing_log_response)
        if has_file:
            temp_input_path = save_uploaded_file(uploaded_file)

        final_message, loop_context, loop_trace, usage = run_direct_agent_execution(
            user_text,
            has_file,
            temp_input_path,
            text_fields,
            forced_task_type,
            debug_response,
        )

        response_payload = {
            "object": "agent.chat",
            "message": final_message,
        }
        tool_result = loop_context.get("tool_result")
        if isinstance(tool_result, dict) and tool_result.get("result_image"):
            response_payload["result_image"] = tool_result["result_image"]
        if debug_response:
            response_payload.update({
                "agent_mode": "direct_context_selection",
                "final_state_context": compact_agent_context(loop_context),
                "trace": loop_trace,
                "usage": usage,
            })
        return Response(json.dumps(response_payload, ensure_ascii=False), content_type="application/json")
    except Exception as exc:
        print(f"[ERROR] agent_chat failed: {exc}", flush=True)
        return jsonify({
            "error": {
                "message": str(exc),
                "type": "server_error",
                "param": None,
                "code": None,
            }
        }), 500
    finally:
        if timing_log_token is not None:
            TIMING_LOG_ENABLED.reset(timing_log_token)
        if temp_input_path and os.path.exists(temp_input_path):
            os.unlink(temp_input_path)
        lock.release()
        is_blocking = False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0", help="Bind host for the single agent server")
    parser.add_argument("--port", type=int, default=8082, help="Bind port for the single agent server")
    parser.add_argument("--llm_api_url", default=LLM_API_URL, help="OpenAI-compatible local LLM chat completions endpoint")
    parser.add_argument("--llm_model_name", default=LLM_MODEL_NAME, help="Model name passed to the local LLM API")
    args = parser.parse_args()

    LLM_API_URL = args.llm_api_url
    LLM_MODEL_NAME = args.llm_model_name

    app.run(host=args.host, port=args.port, threaded=True, debug=False)
