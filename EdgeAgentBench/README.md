# EdgeAgentBench

EdgeAgentBench 使用真实设备采样得到的 `config_files/latency_profiles.json` 构造本地异构集群调度评测集。评测目标是：在给定用户请求、节点在线状态、资源状态、容器状态和用户约束时，比较不同调度策略选出的节点对应的真实端到端时延。

## 目录

```text
EdgeAgentBench/
├── cases/      # 每个 query/state 组合生成的 benchmark case
├── queries/    # 各 AI 任务的自然语言 query 模板
├── results/    # CSV、summary JSON、负载/场景统计结果
├── scripts/    # 评测、mock、聚合脚本
└── states/     # 每个任务的集群资源状态快照
```

## 脚本用途

`generate_latency_profile_bench.py`

从 `config_files/latency_profiles.json` 生成 `states/` 和 `cases/`。它负责组合多开发板资源状态、加入 offline 节点、加入 `container_running` 状态、套用 query 约束，并写入每个 case 的 ground truth。

`update_static_base_latency.py`

从真实采样画像中为每个 `task_type + device_type` 选择低负载状态下的端到端时延，写入 `config_files/static_info.json` 的 `taskOverhead.base_latency_ms`。Agent 和 scheduler 的 `base_latency` 先验来自这里。

`run_benchmark.py`

离线运行纯 Python baseline。支持 `scheduler`、`base-oasis`、`random`、`round_robin`、`resource_average`、`weight_schedule`、`cpu_min`、`npu_min`、`mem_min`、`container_first`、`ground_truth`。

`run_all_baselines.sh`

批量运行常用离线 baseline，并把结果写入 `EdgeAgentBench/results/*_results.csv`。

`optimize_scheduler_weights.py`

遍历 scheduler/Agent prompt 的权重组合，按准确率、平均预测端到端时延和 regret 排序，用于寻找合适的调度权重。

`build_dispatch_history_from_cases.py`

从 benchmark case 生成完整历史库 `dispatch_history.json`。用于测试 Agent 在“已有历史记录”场景下是否能基于相近状态和历史 latency 选择节点。

`mock_case_server.py`

保存当前 benchmark case id，并给 mock MCP server 读取。真实 Agent 测试时，runner 每个请求前都会先设置 current case。

`mock_mcp_server.py`

替代真实 gateway/MCP。它根据 current case 返回 `get_dispatch_snapshot`，并拦截 `run_task_on_node`，不执行真实模型，只返回 Agent 选择节点在 case 中的真实 `e2e_ms`。

`run_agent_selection_benchmark.py`

调用真实 Agent HTTP 接口，在 mock MCP 环境中逐条测试 Agent 的开发板选择。输出每条 case 的 `predicted_node`、`predicted_ete_ms`、LLM 推理耗时、请求总耗时等。

`aggregate_bench_results.py`

聚合 `load.json` 和 `scenario.json` 中的类别，统计九种策略在 CPU/MEM/NPU 负载类别，以及 `node_offline`、`user_constraint`、`container_status` 场景下的平均 `predicted_ete_ms`，输出 `bench.json`。

## Case 格式

每个 case 是一个独立 JSON，文件名类似 `mobilenet_044_q_cls_001.json`：

```json
{
  "case_id": "mobilenet_044_q_cls_001",
  "query": "请对这张图片进行分类",
  "task_type": "MobileNet",
  "nodes": [
    {
      "node_id": "ATLAS_H",
      "platform": "ATLAS_H",
      "online": true,
      "cpu_used": 0.305,
      "mem_used": 0.61779,
      "npu_used": 0.61,
      "network_latency": 1,
      "container_running": true,
      "latency_profile": {
        "model": "MobileNet",
        "device_type": "ATLAS_H",
        "e2e_ms": 1360.207,
        "exec_ms": 40.779
      }
    }
  ],
  "constraints": {},
  "ground_truth": {
    "node_id": "ATLAS_H",
    "ete_time": 1360.207,
    "container_running": true
  }
}
```

`ground_truth.node_id` 是当前 case 中在线、满足用户约束、且真实 `latency_profile.e2e_ms` 最短的节点。离线节点的 `e2e_ms` 和 `exec_ms` 为 `-1`，不会参与 GT 和评测候选。`container_running` 是调度输入特征，不改变真实时延。

## 生成 Benchmark

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/generate_latency_profile_bench.py \
  --states-per-model 500 \
  --offline-every 5 \
  --warm-every 3
```

常用参数：

```text
--profile PATH
  latency_profiles.json 路径，默认 config_files/latency_profiles.json。

--states-per-model N
  每个模型生成多少个资源状态，默认 500。

--offline-every N
  每 N 个 state 设置一个节点 offline，默认 5；0 表示不生成 offline。

--warm-every N
  每 N 个设备位置设置一个 online 节点 container_running=true，默认 3；0 表示不生成已启动容器状态。

--resource-aligned-models MODELS
  只保留“最低时延节点同时也是平均资源负载最低节点”的 state，例如 YoloV5。

--align-max-attempts N
  开启资源对齐时每个模型最多扫描多少候选 state，默认 10000。
```

## 运行离线 Baseline

单个策略：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/run_benchmark.py \
  --policy scheduler \
  --output EdgeAgentBench/results/scheduler_results.csv
```

批量策略：

```bash
cd /root/AgentScheduler
bash EdgeAgentBench/scripts/run_all_baselines.sh
```

常用参数：

```text
--case-dir PATH
  case 目录，默认 EdgeAgentBench/cases。

--case-pattern GLOB
  case 文件匹配模式，例如 --case-pattern 'yolov5_*.json'。

--policy POLICY
  调度策略名称。

--ignore-constraints
  忽略 case.constraints 中的 require/exclude 节点约束。

--output PATH
  CSV 输出路径。

--summary-output PATH
  summary JSON 输出路径，默认由 --output 推导。

--seed N
  random 策略随机种子，默认 42。

--strict
  存在失败 case 时返回非 0 exit code。
```

CSV 中核心字段：

```text
expected_node
predicted_node
expected_ete_ms
predicted_ete_ms
latency_regret_ms
latency_ratio
```

`predicted_ete_ms` 是策略选中节点在该 case 中的真实端到端时延，`latency_regret_ms = predicted_ete_ms - expected_ete_ms`。

## Baseline 策略

`scheduler`

当前主调度策略：

```text
0.0003 * base_latency_ms + 0.01 * cpu_after + 0.8 * npu_after + 2.0 * mem_after
```

`base-oasis`

纯 Python 复现 Agent prompt 中的整数公式：

```text
3 * base_latency + 1 * cpu + 80 * npu + 200 * mem
```

这里 CPU/NPU/MEM 是百分比。该公式与 `scheduler` 只在单位和整体缩放上不同，排序应一致。

`weight_schedule`

只使用当前资源状态，不使用 `base_latency_ms`：

```text
5 * cpu_after + 17 * npu_after + 39 * mem_after
```

`resource_average`

选择 `(cpu_after + mem_after + npu_after) / 3` 最低的节点。

`cpu_min`、`npu_min`、`mem_min`

分别选择对应资源占用最低的合法在线节点。

`container_first`

优先选择 `container_running=true` 的合法在线节点，再按平均负载排序。

`round_robin`

按固定顺序轮询合法在线节点：

```text
RK3588 -> ATLAS_L -> ATLAS_H
```

`random`

在合法在线节点中随机选择，使用 `--seed` 保持可复现。

`ground_truth`

直接选择 `ground_truth.node_id`，用于 sanity check。

## 搜索权重

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/optimize_scheduler_weights.py \
  --output EdgeAgentBench/results/weight_search_results.csv
```

常用参数：

```text
--base-grid / --cpu-grid / --mem-grid / --npu-grid
  逗号分隔的权重列表。

--case-pattern GLOB
  只搜索某类任务，例如 --case-pattern 'bert_*.json'。

--limit N
  只取前 N 个 case 做 smoke test。

--top N
  summary JSON 中保留前 N 组权重。
```

## 构建历史库

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/build_dispatch_history_from_cases.py \
  --case-dir EdgeAgentBench/cases \
  --output config_files/dispatch_history_full.json
```

如果要让 Agent 使用该完整历史库：

```bash
DISPATCH_HISTORY_ENABLED=1 \
DISPATCH_HISTORY_PATH=/root/AgentScheduler/config_files/dispatch_history_full.json \
EDGE_BENCH_CASE_SERVER_URL=http://127.0.0.1:18082 \
MCP_SERVER_PATH=/root/AgentScheduler/EdgeAgentBench/scripts/mock_mcp_server.py \
python3 agent/server/agent.py --host 0.0.0.0 --port 8082
```

## 测真实 Agent 选择

先启动 mock case server：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/mock_case_server.py \
  --host 127.0.0.1 \
  --port 18082 \
  --case-dir EdgeAgentBench/cases
```

再启动 Agent：

```bash
cd /root/AgentScheduler
EDGE_BENCH_CASE_SERVER_URL=http://127.0.0.1:18082 \
MCP_SERVER_PATH=/root/AgentScheduler/EdgeAgentBench/scripts/mock_mcp_server.py \
python3 agent/server/agent.py --host 0.0.0.0 --port 8082
```

运行评测：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/run_agent_selection_benchmark.py \
  --agent-url http://127.0.0.1:8082/v1/agent/chat \
  --case-server-url http://127.0.0.1:18082 \
  --progress-every 100 \
  --output EdgeAgentBench/results/agent_selection_full_results.csv
```

常用参数：

```text
--case-pattern GLOB
  只测试一部分 case。

--limit N
  只跑前 N 个 case，适合 smoke test。

--image PATH
  视觉任务上传给 Agent 的样例图片。

--prompt-log PATH
  额外保存每条 case 的 dispatch_context 和重建后的选择 prompt，格式为 JSONL。

--no-force-task-type
  默认会强制传入 case.task_type，只测试节点选择；加上该参数会同时测试任务识别。

--progress-every N
  每 N 条 case 打印一次进度。
```

关闭历史和资源信息的消融示例：

```bash
EDGE_BENCH_CASE_SERVER_URL=http://127.0.0.1:18082 \
MCP_SERVER_PATH=/root/AgentScheduler/EdgeAgentBench/scripts/mock_mcp_server.py \
DISPATCH_INCLUDE_RESOURCE_STATE=0 \
DISPATCH_HISTORY_ENABLED=0 \
python3 agent/server/agent.py --host 0.0.0.0 --port 8082
```

## 聚合负载和场景结果

`load.json` 记录 CPU/MEM/NPU 主导负载类别，`scenario.json` 记录 `node_offline`、`user_constraint`、`container_status` 三类场景。生成或更新各策略 CSV 后运行：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/aggregate_bench_results.py \
  --output EdgeAgentBench/results/bench.json
```

默认聚合九种策略：

```text
Random
Round-Robin
Container-First
MEM-Min
NPU-Min
CPU-Min
Average-Load
Weighted-Schedule
agent_with_history
```

默认 `agent_with_history` 读取：

```text
EdgeAgentBench/results/agent_selection_full_results.csv
```

如需替换某个策略文件：

```bash
python3 EdgeAgentBench/scripts/aggregate_bench_results.py \
  --policy-file agent_with_history=EdgeAgentBench/results/agent_selection_hotstart_results.csv \
  --output EdgeAgentBench/results/bench.json
```

输出 `bench.json` 包含：

```text
load.overall_by_category
load.by_category_by_task
scenario.overall_by_category
scenario.by_category_by_task
policy_overall
```

## latency_profiles.json 来源

`config_files/latency_profiles.json` 由真实设备采样表转换生成，转换脚本位于：

```text
agent/tools/convert_latency_xlsx.py
```

重新转换：

```bash
cd /root/AgentScheduler
python3 agent/tools/convert_latency_xlsx.py
```

画像数据主要字段：

```text
task_type
device_type
avg_cpu_percent
avg_mem_percent
avg_npu_percent
e2e_ms
exec_ms
```

benchmark 的 GT 只看 `e2e_ms`，即端到端时延。
