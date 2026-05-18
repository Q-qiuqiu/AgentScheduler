import argparse
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

lock = threading.Lock()
is_blocking = False
LLM_API_URL = os.environ.get("LLM_API_URL", "http://127.0.0.1:8081/v1/chat/completions")
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "rkllm-default")


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
    wants_text_model = bool(re.search(r"文本分类|句子分类|语义匹配|文本理解|classify text|text classification|bert", normalized_text))
    wants_segmentation = bool(re.search(r"语义分割|图片分割|图像分割|分割|segmentation|segment", normalized_text))
    wants_detection = bool(re.search(r"目标检测|物体检测|检测|框|detect|detection|object", normalized_text))
    wants_image_classification = bool(re.search(r"图片分类|图像分类|照片分类|分类|classify|classification", normalized_text))

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


def build_result_summary_messages(user_text, selected_execution, tool_result):
    return [
        {
            "role": "system",
            "content": (
                "You are a single agent returning the final answer.\n"
                "Summarize the actual tool execution result in concise Chinese.\n"
                "Use only the provided result. Do not include node ids, URLs, JSON, or debug fields."
            ),
        },
        {
            "role": "user",
            "content": (
                f"user_text:\n{user_text}\n\n"
                f"selected_execution:\n{json.dumps(selected_execution, ensure_ascii=False)}\n\n"
                f"tool_result:\n{json.dumps(redact_large_binary_fields(tool_result), ensure_ascii=False)}"
            ),
        },
    ]


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


def summarize_result(user_text, selected_execution, tool_result):
    deterministic_summary = build_deterministic_summary(selected_execution, tool_result)
    if deterministic_summary:
        return deterministic_summary, zero_usage()
    model_result = call_llm(
        build_result_summary_messages(user_text, selected_execution, tool_result),
        timing_label="model_inference.final_result_summary",
    )
    content = model_result["content"].strip()
    if not content:
        content = f"工具任务已完成，使用模型：{selected_execution.get('task_type', 'unknown')}。"
    return content, model_result


STATE_ALLOWED_ACTIONS = {
    "S0": {"get_task_catalog", "get_task_overhead", "get_cluster_resources", "final_response"},
    "S1": {"get_cluster_resources", "get_task_overhead", "schedule_task", "final_response"},
    "S2": {"get_task_catalog", "get_task_overhead", "schedule_task", "final_response"},
    "S3": {"run_task_on_node"},
    "S4": {"final_response"},
    "Error": {"retry", "get_task_overhead", "schedule_task", "run_task_on_node", "final_response"},
}

ACTION_TIMEOUTS = {
    "get_task_catalog": 30,
    "get_task_overhead": 30,
    "get_cluster_resources": 30,
    "schedule_task": 30,
    "run_task_on_node": 150,
}

MAX_AGENT_STEPS = 8


def infer_task_type_by_rules(user_text, has_file):
    route = route_by_rules(user_text, has_file)
    if route and route.get("route") == "execute_tool":
        return route.get("task_type")
    return None


def infer_schedule_constraints_by_rules(user_text):
    constraints = {}
    platform_patterns = [
        ("RK3588", r"RK3588|rk3588"),
        ("ATLAS_L", r"ATLAS[_ -]?L|Atlas[_ -]?L|atlas[_ -]?l"),
        ("ATLAS_H", r"ATLAS[_ -]?H|Atlas[_ -]?H|atlas[_ -]?h"),
    ]
    exclude_nodes = []
    require_nodes = []
    exclude_context = bool(re.search(r"不要|别用|不能用|不使用|排除|exclude|without|do not use", user_text, re.IGNORECASE))
    require_context = bool(re.search(r"必须|指定|只用|仅用|使用|在.*上|require|only|must", user_text, re.IGNORECASE))

    for platform, pattern in platform_patterns:
        if not re.search(pattern, user_text):
            continue
        if exclude_context:
            exclude_nodes.append(platform)
        elif require_context:
            require_nodes.append(platform)

    if exclude_nodes:
        constraints["exclude_nodes"] = sorted(set(exclude_nodes))
    if require_nodes:
        constraints["require_nodes"] = sorted(set(require_nodes))
    return constraints


def is_catalog_request(user_text):
    return bool(re.search(r"支持.*(任务|模型)|有哪些.*(任务|模型)|task catalog|catalog|supported.*(tasks|models)", user_text, re.IGNORECASE))


def is_overhead_request(user_text):
    return bool(re.search(r"overhead|资源开销|资源消耗|启动开销|开销|耗费|cost|startup", user_text, re.IGNORECASE))


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


def allowed_arguments_for_action(action, context):
    if action in {"get_cluster_resources", "get_task_catalog", "final_response", "retry"}:
        return {}
    if action == "get_task_overhead":
        allowed_args = {}
        task_type = context.get("task_type") or context.get("inferred_task_type")
        if task_type:
            allowed_args["task_type"] = task_type
        return allowed_args
    if action == "schedule_task":
        allowed_args = {}
        task_type = context.get("task_type") or context.get("inferred_task_type")
        if task_type:
            allowed_args["task_type"] = task_type
        for key, value in context.get("schedule_constraints", {}).items():
            if key in {"exclude_nodes", "require_node", "require_nodes"}:
                allowed_args[key] = value
        return allowed_args
    if action == "run_task_on_node":
        allowed_args = {}
        task_type = context.get("task_type") or context.get("inferred_task_type")
        if task_type:
            allowed_args["task_type"] = task_type
        scheduled_node = context.get("scheduled_node") or {}
        target_global_id = scheduled_node.get("target_global_id")
        if target_global_id:
            allowed_args["target_global_id"] = target_global_id
        return allowed_args
    return {}


def allowed_arguments_for_actions(allowed_actions, context):
    return {
        action: allowed_arguments_for_action(action, context)
        for action in sorted(allowed_actions)
    }


def constrain_action_arguments(action, arguments, context):
    allowed_args = allowed_arguments_for_action(action, context)
    return {
        key: value
        for key, value in allowed_args.items()
        if value is not None
    }


def build_constrained_action_messages(user_text, has_file, state, context, allowed_actions, allowed_arguments):
    return [
        {
            "role": "system",
            "content": (
                "You are a constrained tool-calling edge agent.\n"
                "You must choose exactly one next action from allowed_actions.\n"
                "Return JSON only in this shape:\n"
                "{\"action\":\"<one of allowed_actions>\",\"arguments\":{},\"reason\":\"<short reason>\"}\n"
                "State meanings:\n"
                "S0 Task Understanding; S1 Task Catalog Retrieved; S2 Resource Retrieved; "
                "S3 Node Scheduled; S4 Task Executed; S5 Final Response; Error Recovery.\n"
                "Tool intent:\n"
                "get_task_catalog gets supported task/model config from JSON config files; call it before choosing an unknown task_type.\n"
                "get_task_overhead gets scheduler-only task overhead/resource-cost data when the user explicitly asks resource cost or startup requirements.\n"
                "get_cluster_resources gets live cluster resource state.\n"
                "schedule_task asks gateway scheduler to choose a node for a task_type.\n"
                "run_task_on_node runs the selected task on the scheduled node.\n"
                "final_response ends the loop and answers the user.\n"
                "Rules:\n"
                "1. Never choose an action outside allowed_actions.\n"
                "2. Never invent argument keys or values. Use only allowed_arguments for the chosen action.\n"
                "3. For schedule_task, use the task_type and node constraints exactly as listed in allowed_arguments.\n"
                "4. For image/file tools, only schedule them when has_file is true.\n"
                "5. If the user only asks about cluster status, call get_cluster_resources then final_response.\n"
                "6. If the user asks supported task types or models, call get_task_catalog then final_response.\n"
                "7. If the user asks overhead, resource cost, or startup requirements for a task, call get_task_overhead then final_response.\n"
                "8. If execution is needed, retrieve missing catalog/resources before scheduling when useful.\n"
                "9. Never invent node ids, model outputs, overhead data, or cluster status."
            ),
        },
        {
            "role": "user",
            "content": (
                f"user_text:\n{user_text}\n\n"
                f"has_file:\n{json.dumps(has_file)}\n\n"
                f"state:\n{state}\n\n"
                f"allowed_actions:\n{json.dumps(sorted(allowed_actions), ensure_ascii=False)}\n\n"
                f"allowed_arguments:\n{json.dumps(allowed_arguments, ensure_ascii=False)}\n\n"
                f"context:\n{json.dumps(compact_agent_context(context), ensure_ascii=False)}"
            ),
        },
    ]


def allowed_actions_for_context(user_text, state, context):
    allowed_actions = set(STATE_ALLOWED_ACTIONS[state])
    wants_catalog = is_catalog_request(user_text)
    wants_overhead = is_overhead_request(user_text)
    needs_online_scope = bool(context.get("inferred_task_type")) or wants_catalog or wants_overhead

    if needs_online_scope and not context.get("cluster_resources") and "get_cluster_resources" in allowed_actions:
        return {"get_cluster_resources"}
    if wants_overhead and not context.get("task_overhead") and "get_task_overhead" in allowed_actions:
        return {"get_task_overhead"}
    if (context.get("inferred_task_type") or wants_catalog) and not context.get("task_catalog") and "get_task_catalog" in allowed_actions:
        return {"get_task_catalog"}
    if context.get("inferred_task_type") and not context.get("scheduled_node") and "schedule_task" in allowed_actions:
        return {"schedule_task"}
    return allowed_actions


def choose_constrained_action(user_text, has_file, state, context):
    allowed_actions = allowed_actions_for_context(user_text, state, context)
    allowed_arguments = allowed_arguments_for_actions(allowed_actions, context)
    model_result = call_llm(
        build_constrained_action_messages(user_text, has_file, state, context, allowed_actions, allowed_arguments),
        timing_label=f"model_inference.action_selection.{state}",
    )
    try:
        action_payload = parse_json_object(model_result["content"])
    except Exception as exc:
        raise RuntimeError(f"action model returned invalid JSON: {exc}") from exc

    action = action_payload.get("action")
    if action not in allowed_actions:
        if context.get("inferred_task_type") and not context.get("tool_result"):
            task_type = context["inferred_task_type"]
            if state in {"S1", "S2"} and "schedule_task" in allowed_actions:
                return "schedule_task", {"task_type": task_type}, "execution request must schedule before final response", model_result
            if state == "S3" and "run_task_on_node" in allowed_actions:
                return "run_task_on_node", {"task_type": task_type}, "execution request must run tool before final response", model_result
            if state == "Error" and context.get("scheduled_node") and "run_task_on_node" in allowed_actions:
                return "run_task_on_node", {"task_type": task_type}, "execution request must run tool before final response", model_result
            if state == "Error" and "schedule_task" in allowed_actions:
                return "schedule_task", {"task_type": task_type}, "execution request must schedule before final response", model_result
        if action == "get_task_catalog" and context.get("task_catalog") and "final_response" in allowed_actions:
            return "final_response", {}, "task catalog already retrieved; answer from context", model_result
        if action == "get_cluster_resources" and context.get("cluster_resources") and "final_response" in allowed_actions:
            return "final_response", {}, "cluster resources already retrieved; answer from context", model_result
        raise RuntimeError(f"action {action} is not allowed in state {state}; allowed={sorted(allowed_actions)}")
    arguments = action_payload.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise RuntimeError("action arguments must be an object")
    arguments = constrain_action_arguments(action, arguments, context)

    if action == "final_response" and context.get("inferred_task_type") and not context.get("tool_result"):
        task_type = context["inferred_task_type"]
        if state == "S0" and "get_task_catalog" in allowed_actions:
            return "get_task_catalog", {}, "execution request must load task catalog before final response", model_result
        if state in {"S1", "S2"} and "schedule_task" in allowed_actions:
            return "schedule_task", {"task_type": task_type}, "execution request must schedule before final response", model_result
        if state == "S3" and "run_task_on_node" in allowed_actions:
            return "run_task_on_node", {"task_type": task_type}, "execution request must run tool before final response", model_result
        if state == "Error" and context.get("scheduled_node") and "run_task_on_node" in allowed_actions:
            return "run_task_on_node", {"task_type": task_type}, "execution request must run tool before final response", model_result
        if state == "Error" and "schedule_task" in allowed_actions:
            return "schedule_task", {"task_type": task_type}, "execution request must schedule before final response", model_result

    return action, arguments, action_payload.get("reason", ""), model_result


def next_state_after_action(state, action):
    if action == "get_task_catalog":
        return "S1"
    if action == "get_task_overhead":
        return "S1"
    if action == "get_cluster_resources":
        return "S2"
    if action == "schedule_task":
        return "S3"
    if action == "run_task_on_node":
        return "S4"
    if action == "final_response":
        return "S5"
    if action == "retry":
        return "S0"
    return state


def resolve_task_type(arguments, context):
    task_type = arguments.get("task_type") or context.get("task_type") or context.get("inferred_task_type")
    if not task_type:
        raise RuntimeError("task_type is required for this action")
    task_catalog = context.get("task_catalog")
    if isinstance(task_catalog, dict):
        available_task_types = {
            item.get("task_type")
            for item in task_catalog.get("result", [])
            if isinstance(item, dict) and item.get("task_type")
        }
        if available_task_types and task_type not in available_task_types:
            raise RuntimeError(f"task_type {task_type} is not available in current task catalog")
    return task_type


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


def run_constrained_tool_action(action, arguments, context, user_text, has_file, text_fields, input_path):
    if action == "get_cluster_resources":
        return json.loads(call_mcp_tool("get_cluster_resources", timeout=ACTION_TIMEOUTS[action]))

    if action == "get_task_catalog":
        available_device_types = None
        cluster_resources = context.get("cluster_resources")
        if isinstance(cluster_resources, dict):
            available_device_types = sorted(
                {
                    node.get("type")
                    for node in cluster_resources.get("result", [])
                    if isinstance(node, dict) and node.get("type")
                }
            )
        args = {}
        if available_device_types is not None:
            args["available_device_types"] = available_device_types
        return json.loads(call_mcp_tool("get_task_catalog", args, timeout=ACTION_TIMEOUTS[action]))

    if action == "get_task_overhead":
        args = dict(arguments)
        cluster_resources = context.get("cluster_resources")
        if isinstance(cluster_resources, dict) and not args.get("device_type") and not args.get("available_device_types"):
            available_device_types = sorted(
                {
                    node.get("type")
                    for node in cluster_resources.get("result", [])
                    if isinstance(node, dict) and node.get("type")
                }
            )
            if available_device_types:
                args["available_device_types"] = available_device_types
        return json.loads(call_mcp_tool("get_task_overhead", args, timeout=ACTION_TIMEOUTS[action]))

    if action == "schedule_task":
        task_type = resolve_task_type(arguments, context)
        validate_task_input_requirements(task_type, has_file, context)
        context["task_type"] = task_type
        schedule_args = {
            key: arguments[key]
            for key in ("exclude_nodes", "require_node", "require_nodes")
            if key in arguments
        }
        for key, value in context.get("schedule_constraints", {}).items():
            schedule_args.setdefault(key, value)
        schedule_args["task_type"] = task_type
        return json.loads(call_mcp_tool("schedule_task", schedule_args, timeout=ACTION_TIMEOUTS[action]))

    if action == "run_task_on_node":
        task_type = resolve_task_type(arguments, context)
        validate_task_input_requirements(task_type, has_file, context)
        execution_metadata = get_execution_metadata(task_type, context)
        scheduled_node = context.get("scheduled_node") or {}
        target_global_id = arguments.get("target_global_id") or scheduled_node.get("target_global_id")
        if not target_global_id:
            raise RuntimeError("run_task_on_node requires a scheduled target_global_id")
        fields = dict(text_fields)
        fields.setdefault("text", user_text)
        return json.loads(
            call_mcp_tool(
                "run_task_on_node",
                {
                    "task_type": task_type,
                    "target_global_id": target_global_id,
                    "input_path": input_path,
                    "real_url": execution_metadata["real_url"],
                    "file_field_name": execution_metadata["file_field_name"],
                    "fields": fields,
                },
                timeout=ACTION_TIMEOUTS[action],
            )
        )

    if action == "retry":
        return {"status": "retry"}

    raise RuntimeError(f"unsupported action executor: {action}")


def build_loop_final_messages(user_text, context, trace=None):
    user_content = f"user_text:\n{user_text}\n\ncontext:\n{json.dumps(compact_agent_context(context), ensure_ascii=False)}"
    if trace is not None:
        user_content = f"{user_content}\n\ntrace:\n{json.dumps(trace, ensure_ascii=False)}"

    return [
        {
            "role": "system",
            "content": (
                "You are the final response generator for a constrained edge agent.\n"
                "Answer in concise natural Chinese.\n"
                "Use only the provided context.\n"
                "Do not expose internal JSON unless the user explicitly asks for raw data.\n"
                "For cluster status, summarize each node and resource usage.\n"
                "For task catalog, summarize each supported task/model and its supported_device_types.\n"
                "For task overhead, summarize proc_time, mem_usage, cpu_usage, and xpu_usage by task and device type.\n"
                "For executed tasks, summarize the actual tool result."
            ),
        },
        {"role": "user", "content": user_content},
    ]


def make_loop_final_response(user_text, context, trace, include_trace=False):
    if context.get("tool_result") and context.get("selected_execution"):
        return summarize_result(user_text, context["selected_execution"], context["tool_result"])
    model_result = call_llm(
        build_loop_final_messages(user_text, context, trace if include_trace else None),
        timing_label="model_inference.final_response",
    )
    return model_result["content"].strip(), model_result


def run_constrained_agent_loop(user_text, has_file, input_path, text_fields, forced_task_type=None, debug_response=False):
    e2e_marker = start_timing()

    def finish_agent_loop():
        e2e_timing = finish_timing(e2e_marker)
        total_usage["timing"] = {"segment": "agent_e2e", **e2e_timing}
        log_timing("agent_e2e", **e2e_timing)

    if forced_task_type:
        if not get_configured_tool_metadata(forced_task_type):
            raise RuntimeError(f"unsupported task_type: {forced_task_type}")
        validate_task_input_requirements(forced_task_type, has_file)
    context = {
        "has_file": has_file,
        "input_path": input_path,
        "inferred_task_type": forced_task_type or infer_task_type_by_rules(user_text, has_file),
        "schedule_constraints": infer_schedule_constraints_by_rules(user_text),
    }
    trace = []
    state = "S0"
    total_usage = zero_usage()

    for _ in range(MAX_AGENT_STEPS):
        try:
            action, arguments, reason, usage = choose_constrained_action(user_text, has_file, state, context)
        except Exception as exc:
            context["last_error"] = str(exc)
            fallback_actions = allowed_actions_for_context(user_text, state, context)
            if "get_cluster_resources" in fallback_actions:
                action, arguments, reason, usage = "get_cluster_resources", {}, "fallback after invalid model action", zero_usage()
            elif "get_task_catalog" in fallback_actions:
                action, arguments, reason, usage = "get_task_catalog", {}, "fallback after invalid model action", zero_usage()
            elif "schedule_task" in fallback_actions and context.get("inferred_task_type"):
                arguments = {"task_type": context["inferred_task_type"], **context.get("schedule_constraints", {})}
                action, arguments, reason, usage = "schedule_task", arguments, "fallback after invalid model action", zero_usage()
            elif "run_task_on_node" in fallback_actions:
                action, arguments, reason, usage = "run_task_on_node", {}, "fallback after invalid model action", zero_usage()
            else:
                final_message, final_usage = make_loop_final_response(user_text, context, trace, debug_response)
                total_usage["prompt_tokens"] += final_usage.get("prompt_tokens", 0)
                total_usage["completion_tokens"] += final_usage.get("completion_tokens", 0)
                total_usage["total_tokens"] = total_usage["prompt_tokens"] + total_usage["completion_tokens"]
                finish_agent_loop()
                return final_message, context, trace, total_usage
        total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        trace_item = {
            "state": state,
            "action": action,
            "arguments": arguments,
            "reason": reason,
        }
        if usage.get("timing"):
            trace_item["model_timing"] = usage["timing"]

        if action == "final_response":
            final_message, final_usage = make_loop_final_response(user_text, context, trace, debug_response)
            total_usage["prompt_tokens"] += final_usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += final_usage.get("completion_tokens", 0)
            total_usage["total_tokens"] = total_usage["prompt_tokens"] + total_usage["completion_tokens"]
            if final_usage.get("timing"):
                trace_item["final_model_timing"] = final_usage["timing"]
            trace.append(trace_item)
            finish_agent_loop()
            return final_message, context, trace, total_usage

        tool_timing_marker = None
        try:
            tool_timing_marker = start_timing()
            result = run_constrained_tool_action(action, arguments, context, user_text, has_file, text_fields, input_path)
            tool_timing = finish_timing(tool_timing_marker)
            trace_item["tool_timing"] = {"segment": f"tool.{action}", **tool_timing}
            log_timing(f"tool.{action}", **tool_timing)
            trace_item["result"] = redact_large_binary_fields(result)
            if action == "get_cluster_resources":
                context["cluster_resources"] = result
            elif action == "get_task_catalog":
                context["task_catalog"] = result
            elif action == "get_task_overhead":
                context["task_overhead"] = result
            elif action == "schedule_task":
                context["schedule_result"] = result
                context["scheduled_node"] = result.get("result", {})
                context["task_type"] = resolve_task_type(arguments, context)
            elif action == "run_task_on_node":
                context["tool_result"] = result
                scheduled_node = context.get("scheduled_node", {})
                task_type = context.get("task_type") or context.get("inferred_task_type")
                context["selected_execution"] = {
                    "task_type": task_type,
                    "target_global_id": scheduled_node.get("target_global_id"),
                    "scheduled_node": scheduled_node,
                    "real_url": get_execution_metadata(task_type, context)["real_url"],
                }
            state = next_state_after_action(state, action)
        except Exception as exc:
            if tool_timing_marker is not None:
                tool_timing = finish_timing(tool_timing_marker)
                trace_item["tool_timing"] = {"segment": f"tool.{action}", **tool_timing}
                log_timing(f"tool.{action}", status="error", **tool_timing)
            trace_item["error"] = str(exc)
            context["last_error"] = str(exc)
            state = "Error"

        trace.append(trace_item)

        if state == "S5":
            final_message, final_usage = make_loop_final_response(user_text, context, trace, debug_response)
            total_usage["prompt_tokens"] += final_usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += final_usage.get("completion_tokens", 0)
            total_usage["total_tokens"] = total_usage["prompt_tokens"] + total_usage["completion_tokens"]
            if trace and final_usage.get("timing"):
                trace[-1]["final_model_timing"] = final_usage["timing"]
            finish_agent_loop()
            return final_message, context, trace, total_usage

    raise RuntimeError(f"agent loop exceeded {MAX_AGENT_STEPS} steps; trace={trace}")


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
    try:
        is_blocking = True
        if has_file:
            temp_input_path = save_uploaded_file(uploaded_file)

        final_message, loop_context, loop_trace, usage = run_constrained_agent_loop(
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
                "agent_mode": "constrained_react_loop",
                "final_state_context": compact_agent_context(loop_context),
                "trace": loop_trace,
                "usage": usage,
            })
        return Response(json.dumps(response_payload, ensure_ascii=False), content_type="application/json")
    except Exception as exc:
        return jsonify({
            "error": {
                "message": str(exc),
                "type": "server_error",
                "param": None,
                "code": None,
            }
        }), 500
    finally:
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
