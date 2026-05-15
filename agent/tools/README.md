# Agent Tools

## `mcp_server.py`

Minimal MCP server over `stdio` that exposes tools backed by separate modules:

- `get_cluster_resources`
- `get_task_catalog`
- `get_task_overhead`
- `schedule_task`
- `run_task_auto_schedule`
- `run_task_on_node`

Implementation modules:

- `get_cluster_resources_tool.py`
- `get_task_catalog_tool.py`
- `get_task_overhead_tool.py`
- `schedule_task_tool.py`
- `run_task_auto_schedule_tool.py`
- `run_task_on_node_tool.py`

Shared helpers that are not MCP tools:

- `tool_common.py`

The tool reads cluster state from the gateway HTTP API:

- default URL: `http://127.0.0.1:6666/cluster_resources`
- execution URL: `http://127.0.0.1:6666/quest_on_node`

`get_task_catalog` reads compact task metadata from:

- task metadata file: `/root/AgentScheduler/config_files/agent_info.json`

It returns a flat list of items shaped like:

- `device_type`
- `model_name`
- `task_type`
- `tool_kind`
- `requires_file`
- `file_field_name`
- `real_url`
- `capabilities`
- `description`

Scheduler-only overhead fields from `static_info.json` are intentionally not returned to the agent.

It can also be filtered by `available_device_types` so only models matching currently available node platforms are returned.

`get_task_overhead` reads scheduler-only overhead data from `static_info.json`. It supports optional filters:

- `task_type`: for example `YoloV5`, `MobileNet`, `ResNet50`, `Bert`, or `deeplabv3`
- `device_type`: for example `RK3588`, `ATLAS_L`, or `ATLAS_H`

It returns `proc_time`, `mem_usage`, `cpu_usage`, and `xpu_usage` only when explicitly requested through this dedicated tool.

The default execution settings for `run_task_on_node` are:

- `real_url=predict`
- `file_field_name=image`

`schedule_task` calls the gateway `/schedule_task` endpoint and returns the board selected by the gateway scheduler for a `task_type`.

`run_task_auto_schedule` calls the gateway `/quest` endpoint. The caller only supplies the task payload; gateway scheduling chooses the target board and starts or reuses the tool container.

`run_task_on_node` accepts:

- `task_type`: `YoloV5`, `MobileNet`, `ResNet50`, `Bert`, or `deeplabv3`
- `target_global_id`: node UUID returned by `get_cluster_resources`
- `input_path`: optional local file path for image/file tools
- `fields`: optional form fields forwarded to the tool container, such as `text`

Optional environment variables:

- `GATEWAY_HOST`
- `GATEWAY_PORT`
- `GATEWAY_CLUSTER_RESOURCES_PATH`
- `GATEWAY_SCHEDULE_TASK_PATH`
- `GATEWAY_QUEST_PATH`
- `GATEWAY_QUEST_ON_NODE_PATH`
- `STATIC_INFO_PATH`
- `AGENT_INFO_PATH`

Run manually:

```bash
python3 /root/AgentScheduler/agent/tools/mcp_server.py
```

Example MCP client config command:

```json
{
  "command": "python3",
  "args": ["/root/AgentScheduler/agent/tools/mcp_server.py"],
  "env": {
    "GATEWAY_HOST": "127.0.0.1",
    "GATEWAY_PORT": "6666"
  }
}
```
