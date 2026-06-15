# EdgeAgentBench

EdgeAgentBench 用真实设备采样得到的 `config_files/latency_profiles.json` 构造离线调度评测集，用来比较调度策略选择的节点是否等同于真实端到端时延最短的节点。

## 目录

```text
EdgeAgentBench/
├── cases/      # 生成后的 benchmark case
├── queries/    # 不同任务类型的自然语言 query
├── results/    # run_benchmark.py 输出的 CSV 和 summary JSON
├── scripts/
│   ├── generate_latency_profile_bench.py
│   ├── mock_case_server.py
│   ├── mock_mcp_server.py
│   ├── optimize_scheduler_weights.py
│   ├── run_agent_selection_benchmark.py
│   ├── run_benchmark.py
│   └── update_static_base_latency.py
└── states/     # 生成后的资源状态快照，便于单独查看
```

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

`ground_truth.node_id` 是当前 case 中在线、满足约束、且真实 `latency_profile.e2e_ms` 最短的节点。`container_running` 是调度输入特征，不改变 ground truth 时间。离线节点的 `e2e_ms` 和 `exec_ms` 为 `-1`，不会参与 GT 和评测候选。

## 生成 Benchmark

在仓库根目录运行：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/generate_latency_profile_bench.py
```

默认读取：

```text
config_files/latency_profiles.json
```

默认输出：

```text
EdgeAgentBench/states/*.json
EdgeAgentBench/cases/*.json
EdgeAgentBench/cases/manifest.json
```

参数：

```text
--profile PATH
  latency_profiles.json 路径，默认是 config_files/latency_profiles.json。

--states-per-model N
  每个模型生成多少个资源状态，默认 500。

--offline-every N
  每 N 个 state 人为设置一个节点 offline，默认 5。
  设置为 0 表示不生成 offline 节点。

--warm-every N
  每 N 个设备位置给一个 online 节点标记 container_running=true，默认 3。
  设置为 0 表示不生成已启动容器状态。

--resource-aligned-models MODELS
  逗号分隔的模型名，例如 YoloV5。
  对这些模型，只保留“真实端到端时延最短节点同时也是平均资源负载最低节点”的 state。
  这会让生成的 workload 更符合大模型“低负载更适合调度”的原始直觉；默认关闭。

--align-max-attempts N
  开启 --resource-aligned-models 时，每个模型最多扫描多少个候选 state，默认 10000。
```

示例：

```bash
python3 EdgeAgentBench/scripts/generate_latency_profile_bench.py \
  --states-per-model 500 \
  --offline-every 5 \
  --warm-every 3
```

如果希望 YoloV5 的 case 更符合大模型低负载直觉，可以显式开启资源对齐生成：

```bash
python3 EdgeAgentBench/scripts/generate_latency_profile_bench.py \
  --states-per-model 500 \
  --offline-every 5 \
  --warm-every 3 \
  --resource-aligned-models YoloV5 \
  --align-max-attempts 10000
```

生成逻辑：

- 按 `task_type + device_type` 读取真实时延画像。
- 每个 state 为同一个模型组合多个开发板的资源状态。
- 每个在线节点保留 `cpu_used`、`mem_used`、`npu_used`、`e2e_ms`、`exec_ms`。
- 每 5 个 state 默认人为设置一个 offline 节点。
- 每 3 个设备位置默认人为设置一个 `container_running=true` 的已启动容器节点。
- `container_running` 只影响调度策略输入，不改变 GT 的真实 `e2e_ms`。
- 如果使用 `--resource-aligned-models`，对应模型会跳过“最低时延节点与最低平均负载节点冲突”的 state。
- 如果某个 query 约束导致没有可用在线节点，则跳过该 case。

## 运行评测

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/run_benchmark.py \
  --policy scheduler \
  --output EdgeAgentBench/results/scheduler_results.csv
```

每次运行会输出：

```text
EdgeAgentBench/results/scheduler_results.csv
EdgeAgentBench/results/scheduler_results.summary.json
```

CSV 每个 case 会记录：

```text
expected_node
predicted_node
expected_ete_ms
predicted_ete_ms
latency_regret_ms
latency_ratio
```

其中 `predicted_ete_ms` 是调度方案选中的节点在该 case 真实采样画像里的端到端时延，`latency_regret_ms = predicted_ete_ms - expected_ete_ms`。summary JSON 会额外输出 `average_predicted_ete_ms`、`average_latency_regret_ms` 和 `task_average_ete_ms`，用于比较不同调度方案整体以及分 AI 任务的平均端到端时延。

参数：

```text
--case-dir PATH
  case 目录，默认 EdgeAgentBench/cases。

--case-pattern GLOB
  case 文件匹配模式，默认 *.json。
  例如只测 MobileNet：--case-pattern 'mobilenet_*.json'

--policy POLICY
  要评测的调度策略，默认 scheduler。

--ignore-constraints
  忽略 case.constraints 中的 require/exclude 节点约束。

--output PATH
  CSV 结果路径，默认 EdgeAgentBench/results/runner_results.csv。

--summary-output PATH
  summary JSON 路径。默认根据 --output 自动生成 .summary.json。

--failures N
  标准输出中最多打印多少条失败样例，默认 10。

--seed N
  random 策略的随机种子，默认 42。

--strict
  只要存在失败 case 就返回非 0 exit code，适合 CI 或 sanity check。
```

## Policy 含义

`ground_truth`

直接读取 `ground_truth.node_id`，应当始终为 100%。用于确认 case 文件和 runner 读取逻辑正确。

```bash
python3 EdgeAgentBench/scripts/run_benchmark.py --policy ground_truth --strict
```

`scheduler`

离线模拟优化后的 scheduler 资源打分逻辑，选择得分最低的节点：

```text
0.0003 * base_latency_ms + 0.01 * cpu_after + 0.8 * npu_after + 2.0 * mem_after
```

其中 `cpu_after`、`mem_after`、`npu_after` 直接使用 case 中的当前资源比例值；bench 资源值已经是用于调度的状态，不再二次叠加任务 overhead。`base_latency_ms` 来自 `config_files/static_info.json` 的 `taskOverhead.base_latency_ms`，由 `update_static_base_latency.py` 从真实采样中选择 CPU/MEM/NPU 总占用最低的端到端时延写入。该组权重来自 `optimize_scheduler_weights.py` 的全量 bench 搜索，目标是降低平均 `predicted_ete_ms`。

`resource_average`

选择平均负载最低的可行节点：

```text
(cpu_after + mem_after + npu_after) / 3
```

`cpu_min`、`npu_min`、`mem_min`

分别选择 CPU、NPU、内存占用最低的可行节点。

`container_first`

优先选择 `container_running=true` 的节点，再按平均负载排序。

`round_robin`

按固定顺序轮询可行节点：

```text
RK3588 -> ATLAS_L -> ATLAS_H
```

`random`

在可行节点中随机选择，使用 `--seed` 保持可复现。

## 搜索 Agent 权重

如果要先用离线 bench 找一组适合放进 Agent prompt 的资源权重，可以运行：

```bash
python3 EdgeAgentBench/scripts/optimize_scheduler_weights.py \
  --output EdgeAgentBench/results/weight_search_results.csv
```

该脚本的评估口径和 Agent 当前上下文一致：直接使用每个候选节点的当前资源值，用权重公式选择 `rank_score` 最小的节点，并以 `average_predicted_ete_ms` 作为主要排序目标。

常用参数：

```text
--base-grid / --cpu-grid / --mem-grid / --npu-grid
  逗号分隔的权重搜索列表。

--case-pattern GLOB
  只搜索某类任务，例如 --case-pattern 'bert_*.json'。

--limit N
  只取前 N 个 case，用于 smoke test。

--top N
  summary JSON 中保留前 N 组权重。
```

当前全量 bench 局部细化得到的 Agent 权重为：

```text
rank_score = 3*base + 1*cpu + 80*npu + 200*mem
```

这是离线权重换算到 Agent 百分比资源后的整数形式。Agent 上下文中的 `cpu`、`npu`、`mem` 是百分比，保留 3 位小数。

对应 `EdgeAgentBench/results/weight_search_refined.summary.json` 中的全量结果：

```text
case_count=12450
accuracy=0.7443
average_predicted_ete_ms=9592.809
average_latency_regret_ms=1720.23
```

## 常用命令

```bash
# 生成 benchmark
python3 EdgeAgentBench/scripts/generate_latency_profile_bench.py

# 从真实采样刷新每个任务/开发板的低负载端到端时延先验
python3 EdgeAgentBench/scripts/update_static_base_latency.py

# GT sanity check
python3 EdgeAgentBench/scripts/run_benchmark.py \
  --policy ground_truth \
  --output EdgeAgentBench/results/ground_truth_results.csv \
  --strict

# 当前 scheduler
python3 EdgeAgentBench/scripts/run_benchmark.py \
  --policy scheduler \
  --output EdgeAgentBench/results/scheduler_results.csv

# 平均负载 baseline
python3 EdgeAgentBench/scripts/run_benchmark.py \
  --policy resource_average \
  --output EdgeAgentBench/results/resource_average_results.csv

# 只测 YoloV5
python3 EdgeAgentBench/scripts/run_benchmark.py \
  --case-pattern 'yolov5_*.json' \
  --policy scheduler
```

## 测真实 Agent 选择

如果要测试真实 Agent 在每个 bench state 下会选择哪个开发板，需要用 mock MCP server 替代真实 gateway。mock MCP 会：

- 从当前 case 和配置文件一次返回 `get_dispatch_snapshot`，其中包含 `cluster_resources` 和过滤后的 `task_catalog`。
- 拦截 `run_task_on_node`，不执行真实模型，只返回 Agent 选择的 `target_global_id`。

mock 不会把 `latency_profile.e2e_ms` 或 ground truth 提供给 Agent。Agent 只能看到当前资源、任务目录和从 `static_info.json` 读取的低负载 `base_latency_ms`；bench 资源值已经是用于调度的状态，不再额外叠加任务启动 overhead。

先启动 mock case server，runner 会在每个请求前把当前 `case_id` 设置到这里，mock MCP 再从同一个 server 读取 case：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/mock_case_server.py \
  --host 127.0.0.1 \
  --port 18082 \
  --case-dir EdgeAgentBench/cases
```

启动 Agent：

```bash
cd /root/AgentScheduler
EDGE_BENCH_CASE_SERVER_URL=http://127.0.0.1:18082 \
MCP_SERVER_PATH=/root/AgentScheduler/EdgeAgentBench/scripts/mock_mcp_server.py \
python3 agent/server/agent.py --host 0.0.0.0 --port 8082
```

另开一个终端运行真实 Agent 选择评测：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/run_agent_selection_benchmark.py \
  --agent-url http://127.0.0.1:8082/v1/agent/chat \
  --case-server-url http://127.0.0.1:18082 \
  --progress-every 100 \
  --output EdgeAgentBench/results/agent_selection_results.csv
```

这个 runner 在每个请求前向 mock case server 写入当前 `case_id`，Agent 每次调用 mock MCP 的 `get_dispatch_snapshot` 时都会读取同一个 current case。评测时会比较 Agent 最终传给 `run_task_on_node` 的 `target_global_id` 和 `ground_truth.node_id`，同时把 Agent 选中节点对应的真实 `predicted_ete_ms` 写入 CSV，并在 summary JSON 中统计平均端到端时延。

运行时默认每 100 条 case 在 stderr 输出一次进度行。进度行会包含当前准确率、平均请求耗时、平均 LLM 推理耗时、最新 case、最新预测节点、最新总 LLM 耗时和开发板选择 LLM 耗时。CSV 中也会记录：

```text
predicted_node
elapsed_ms
agent_e2e_ms
llm_total_ms
task_llm_ms
dispatch_llm_ms
snapshot_tool_ms
run_tool_ms
predicted_ete_ms
latency_regret_ms
```

参数：

```text
--agent-url URL
  Agent HTTP 接口，默认 http://127.0.0.1:8082/v1/agent/chat。

--case-dir PATH
  case 目录，默认 EdgeAgentBench/cases。

--case-pattern GLOB
  case 文件匹配模式，默认 *.json。

--limit N
  只跑前 N 个 case，适合 smoke test。

--case-server-url URL
  mock case server 地址。必须和启动 Agent 时的 EDGE_BENCH_CASE_SERVER_URL 一致。

--image PATH
  vision/segmentation 任务上传给 Agent 的样例图片，默认 EdgeAgentBench/samples/images/dog1.jpg。

--output PATH
  CSV 输出路径。

--summary-output PATH
  summary JSON 输出路径，默认由 CSV 路径自动推导。

--timeout N
  单个 Agent 请求超时时间，默认 240 秒。

--failures N
  最多打印多少条失败样例，默认 10。

--progress-every N
  每 N 条 case 输出一次进度行，默认 100。

--no-progress
  关闭进度输出。

--no-force-task-type
  默认会把 case.task_type 作为 form 字段传给 Agent，只测试开发板选择。
  加上该参数后，不强制任务类型，可同时测试任务识别和开发板选择。

--strict
  存在失败时返回非 0 exit code。
```

快速测试：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/run_agent_selection_benchmark.py \
  --agent-url http://127.0.0.1:8082/v1/agent/chat \
  --case-server-url http://127.0.0.1:18082 \
  --case-pattern 'bert_001_*.json' \
  --limit 5 \
  --progress-every 1 \
  --failures 5 \
  --output EdgeAgentBench/results/agent_selection_smoke.csv
```

## LLM 调用 schedule_on_node 工具测试

`run_schedule_tool_agent_benchmark.py` 用于测试“LLM 选择工具参数 + 工具内部按 policy 调度并执行”的链路。它不会把 cluster resource 输入给 LLM，只给 LLM `schedule_on_node` 的工具描述，并在 prompt 中固定 `task_type` 和 `policy`，让 LLM 输出工具调用参数：

```json
{"tool":"schedule_on_node","arguments":{"task_type":"YoloV5","policy":"scheduler"}}
```

runner 随后调用 mock MCP 的 `schedule_on_node`，由工具内部读取当前 case 的节点状态、按指定 policy 选节点，并复用 `run_task_on_node` 的 mock 执行结果。

先启动 mock case server：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/mock_case_server.py \
  --case-dir EdgeAgentBench/cases \
  --host 127.0.0.1 \
  --port 18082
```

小规模 smoke test：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/run_schedule_tool_agent_benchmark.py \
  --llm-url http://127.0.0.1:8081/v1/chat/completions \
  --llm-model rkllm-default \
  --case-server-url http://127.0.0.1:18082 \
  --policy scheduler \
  --case-pattern 'yolov5_001_*.json' \
  --limit 5 \
  --progress-every 1 \
  --output EdgeAgentBench/results/schedule_tool_agent_smoke.csv
```

全量测试某个 policy：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/run_schedule_tool_agent_benchmark.py \
  --llm-url http://127.0.0.1:8081/v1/chat/completions \
  --llm-model rkllm-default \
  --case-server-url http://127.0.0.1:18082 \
  --policy scheduler \
  --progress-every 100 \
  --output EdgeAgentBench/results/schedule_tool_agent_scheduler_results.csv
```

可选 policy：

```text
ground_truth
scheduler
resource_average
cpu_min
npu_min
mem_min
container_first
```

## latency_profiles.json 来源

`config_files/latency_profiles.json` 由真实设备采样表转换生成。转换脚本在：

```text
agent/tools/convert_latency_xlsx.py
```

重新转换：

```bash
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
