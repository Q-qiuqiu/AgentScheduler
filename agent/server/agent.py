import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from flask import Flask, Response, jsonify, request


app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_DIR = os.path.normpath(os.path.join(BASE_DIR, ".."))
REPO_ROOT = os.path.normpath(os.path.join(AGENT_DIR, ".."))
DEFAULT_MCP_SERVER_PATH = os.path.join(AGENT_DIR, "tools", "mcp_server.py")
DEFAULT_AGENT_INFO_PATH = os.path.join(REPO_ROOT, "config_files", "agent_info.json")

lock = threading.Lock()
is_blocking = False
LLM_API_URL = os.environ.get("LLM_API_URL", "http://127.0.0.1:8081/v1/chat/completions")
LLM_MODEL_NAME = os.environ.get("LLM_MODEL_NAME", "rkllm-default")

DIRECT_CHAT_ROUTE = "chat"
UNSUPPORTED_TASK_ROUTE = "unsupported_task"


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


def call_llm(messages):
    raw = post_json(
        LLM_API_URL,
        {
            "model": LLM_MODEL_NAME,
            "messages": messages,
            "stream": False,
        },
    )
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


def compact_tools_for_selection(agent_info):
    compact = {}
    for name, tool in agent_info["tools"].items():
        compact[name] = {
            "task_type": tool.get("task_type", name),
            "tool_kind": tool.get("tool_kind"),
            "requires_file": bool(tool.get("requires_file")),
            "capabilities": tool.get("capabilities", []),
            "description": tool.get("description", ""),
        }
    return compact


def build_tool_selection_messages(user_text, has_file, agent_info):
    return [
        {
            "role": "system",
            "content": (
                "You are a single-agent tool scheduler.\n"
                "Decide whether the user request needs one configured tool or direct chat.\n"
                "Return JSON only with this schema:\n"
                "{\"route\":\"chat\",\"task_type\":\"\",\"reason\":\"<short reason>\"}\n"
                "Rules:\n"
                "1. route must be chat, unsupported_task, or execute_tool.\n"
                "2. task_type must be one of the provided tools when route is execute_tool.\n"
                "3. Choose YoloV5 for image object detection.\n"
                "4. Choose MobileNet or ResNet50 for image classification; prefer MobileNet for speed and ResNet50 for accuracy.\n"
                "5. Choose deeplabv3 for image semantic segmentation.\n"
                "6. Choose Bert for text classification or text understanding model execution.\n"
                "7. If the selected tool requires a file but has_file is false, choose unsupported_task.\n"
                "8. Choose chat for normal conversation or questions that do not need edge tool execution.\n"
                "9. Never output anything except a single JSON object."
            ),
        },
        {
            "role": "user",
            "content": (
                f"user_text:\n{user_text}\n\n"
                f"has_file:\n{json.dumps(has_file)}\n\n"
                f"tools:\n{json.dumps(compact_tools_for_selection(agent_info), ensure_ascii=False)}"
            ),
        },
    ]


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
            "route": "execute_tool" if has_file else UNSUPPORTED_TASK_ROUTE,
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


def select_route(user_text, has_file, forced_task_type=None):
    agent_info = load_agent_info()
    tools = agent_info["tools"]
    if forced_task_type:
        if forced_task_type not in tools:
            raise RuntimeError(f"unsupported task_type: {forced_task_type}")
        if tools[forced_task_type].get("requires_file") and not has_file:
            return {"route": UNSUPPORTED_TASK_ROUTE, "task_type": "", "reason": f"{forced_task_type} requires an uploaded file", "_usage": zero_usage()}
        return {"route": "execute_tool", "task_type": forced_task_type, "reason": "task_type explicitly requested by client", "_usage": zero_usage()}

    rule_route = route_by_rules(user_text, has_file)
    if rule_route is not None:
        return rule_route

    try:
        model_result = call_llm(build_tool_selection_messages(user_text, has_file, agent_info))
        selection = parse_json_object(model_result["content"])
    except Exception as exc:
        print(f"tool route selection failed: {exc}", flush=True)
        return {"route": DIRECT_CHAT_ROUTE, "task_type": "", "reason": "route model failed; fallback to direct chat", "_usage": zero_usage()}

    route = selection.get("route", DIRECT_CHAT_ROUTE)
    task_type = selection.get("task_type", "")
    if route == "execute_tool":
        if task_type not in tools:
            route = DIRECT_CHAT_ROUTE
            task_type = ""
            selection["reason"] = "model selected an unsupported task; fallback to direct chat"
        elif tools[task_type].get("requires_file") and not has_file:
            route = UNSUPPORTED_TASK_ROUTE
            task_type = ""
            selection["reason"] = "selected tool requires an uploaded file"
    elif route not in {DIRECT_CHAT_ROUTE, UNSUPPORTED_TASK_ROUTE}:
        route = DIRECT_CHAT_ROUTE
        task_type = ""
        selection["reason"] = "model selected an unsupported route; fallback to direct chat"

    selection["route"] = route
    selection["task_type"] = task_type
    selection["_usage"] = {
        "prompt_tokens": model_result["prompt_tokens"],
        "completion_tokens": model_result["completion_tokens"],
    }
    return selection


def build_direct_chat_messages(user_text, reason=None):
    return [
        {
            "role": "system",
            "content": (
                "You are a single edge scheduling agent.\n"
                "Answer the user directly in concise natural Chinese.\n"
                "Do not mention JSON, internal routes, node ids, or debug details."
            ),
        },
        {"role": "user", "content": f"user_text:\n{user_text}\n\nroute_reason:\n{reason or 'direct chat'}"},
    ]


def make_direct_chat_answer(user_text, reason=None):
    model_result = call_llm(build_direct_chat_messages(user_text, reason))
    return model_result["content"].strip(), model_result


def execute_with_gateway_scheduler(task_type, user_text, input_path, text_fields):
    agent_info = load_agent_info()
    tool_profile = agent_info["tools"][task_type]
    cluster_resources = json.loads(call_mcp_tool("get_cluster_resources", timeout=30))
    available_device_types = sorted(
        {
            node.get("type")
            for node in cluster_resources.get("result", [])
            if isinstance(node, dict) and node.get("type")
        }
    )
    task_catalog = json.loads(call_mcp_tool("get_task_catalog", {"available_device_types": available_device_types}, timeout=30))
    if not any(item.get("task_type") == task_type for item in task_catalog.get("result", []) if isinstance(item, dict)):
        raise RuntimeError(f"task_type {task_type} is not available on the current cluster")

    schedule_result = json.loads(call_mcp_tool("schedule_task", {"task_type": task_type}, timeout=30))
    scheduled_node = schedule_result.get("result", {})
    target_global_id = scheduled_node.get("target_global_id")
    if not target_global_id:
        raise RuntimeError(f"gateway scheduler did not return target_global_id: {schedule_result}")

    fields = dict(text_fields)
    fields.setdefault("text", user_text)
    tool_result = json.loads(
        call_mcp_tool(
            "run_task_on_node",
            {
                "task_type": task_type,
                "target_global_id": target_global_id,
                "input_path": input_path,
                "real_url": tool_profile.get("real_url", "predict"),
                "file_field_name": tool_profile.get("file_field_name", "image"),
                "fields": fields,
            },
            timeout=150,
        )
    )
    return {
        "selected_execution": {
            "task_type": task_type,
            "target_global_id": target_global_id,
            "real_url": tool_profile.get("real_url", "predict"),
            "scheduled_node": scheduled_node,
        },
        "tool_result": tool_result,
        "startup_failures": [],
    }


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
    model_result = call_llm(build_result_summary_messages(user_text, selected_execution, tool_result))
    content = model_result["content"].strip()
    if not content:
        content = f"工具任务已完成，使用模型：{selected_execution.get('task_type', 'unknown')}。"
    return content, model_result


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

        route_selection = select_route(user_text, has_file, forced_task_type)
        route = route_selection["route"]
        if route in {DIRECT_CHAT_ROUTE, UNSUPPORTED_TASK_ROUTE}:
            final_message, final_usage = make_direct_chat_answer(user_text, route_selection.get("reason", ""))
            response_payload = {
                "object": "agent.chat",
                "message": final_message,
            }
            if debug_response:
                prompt_tokens = route_selection["_usage"]["prompt_tokens"] + final_usage.get("prompt_tokens", 0)
                completion_tokens = route_selection["_usage"]["completion_tokens"] + final_usage.get("completion_tokens", 0)
                response_payload.update({
                    "route_selection": {
                        "route": route,
                        "reason": route_selection.get("reason", ""),
                    },
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                })
            return Response(json.dumps(response_payload, ensure_ascii=False), content_type="application/json")

        task_type = route_selection["task_type"]
        execution = execute_with_gateway_scheduler(task_type, user_text, temp_input_path, text_fields)
        final_message, summary_usage = summarize_result(user_text, execution["selected_execution"], execution["tool_result"])

        prompt_tokens = route_selection["_usage"]["prompt_tokens"] + summary_usage.get("prompt_tokens", 0)
        completion_tokens = route_selection["_usage"]["completion_tokens"] + summary_usage.get("completion_tokens", 0)
        response_payload = {
            "object": "agent.chat",
            "message": final_message,
        }
        if execution["tool_result"].get("result_image"):
            response_payload["result_image"] = execution["tool_result"]["result_image"]
        if debug_response:
            response_payload.update({
                "route_selection": {
                    "route": route_selection["route"],
                    "task_type": task_type,
                    "reason": route_selection.get("reason", ""),
                },
                "selected_execution": execution["selected_execution"],
                "tool_result": redact_large_binary_fields(execution["tool_result"]),
                "startup_failures": execution["startup_failures"],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
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
