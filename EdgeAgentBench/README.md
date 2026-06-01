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
│   ├── mock_mcp_server.py
│   ├── run_agent_selection_benchmark.py
│   └── run_benchmark.py
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
    "ete_time": 1360.207
  }
}
```

`ground_truth.node_id` 是当前 case 中在线、满足约束、且 `latency_profile.e2e_ms` 最短的节点。离线节点的 `e2e_ms` 和 `exec_ms` 为 `-1`，不会参与 GT 和评测候选。

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
```

示例：

```bash
python3 EdgeAgentBench/scripts/generate_latency_profile_bench.py \
  --states-per-model 500 \
  --offline-every 5
```

生成逻辑：

- 按 `task_type + device_type` 读取真实时延画像。
- 每个 state 为同一个模型组合多个开发板的资源状态。
- 每个在线节点保留 `cpu_used`、`mem_used`、`npu_used`、`e2e_ms`、`exec_ms`。
- 每 5 个 state 默认人为设置一个 offline 节点。
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

离线模拟当前 gateway 的调度打分逻辑，选择得分最低的节点：

```text
0.4 * cpu_after + 0.8 * npu_after + 0.25 * mem_after + 0.1 * cold_start
```

当前 latency-profile case 没有 `ready_containers` 字段，因此 runner 按 warm container 处理，`cold_start = false`。

`resource_average`

选择平均负载最低的可行节点：

```text
(cpu_after + mem_after + npu_after) / 3
```

`cpu_min`、`npu_min`、`mem_min`

分别选择 CPU、NPU、内存占用最低的可行节点。

`container_first`

优先选择 warm container 节点，再按平均负载排序。当前简化 case 没有容器状态，所以效果等同于 `resource_average`。

`round_robin`

按固定顺序轮询可行节点：

```text
RK3588 -> ATLAS_L -> ATLAS_H
```

`random`

在可行节点中随机选择，使用 `--seed` 保持可复现。

## 常用命令

```bash
# 生成 benchmark
python3 EdgeAgentBench/scripts/generate_latency_profile_bench.py

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

- 从当前 case 返回 `get_cluster_resources`。
- 从配置文件返回 `get_task_catalog`。
- 拦截 `run_task_on_node`，不执行真实模型，只返回 Agent 选择的 `target_global_id`。

启动 Agent：

```bash
cd /root/AgentScheduler
EDGE_BENCH_CURRENT_CASE_FILE=/tmp/edgeagentbench_current_case.txt \
MCP_SERVER_PATH=/root/AgentScheduler/EdgeAgentBench/scripts/mock_mcp_server.py \
python3 agent/server/agent.py --host 0.0.0.0 --port 8082
```

另开一个终端运行真实 Agent 选择评测：

```bash
cd /root/AgentScheduler
python3 EdgeAgentBench/scripts/run_agent_selection_benchmark.py \
  --agent-url http://127.0.0.1:8082/v1/agent/chat \
  --output EdgeAgentBench/results/agent_selection_results.csv
```

这个 runner 在每个请求前把当前 case 路径写入 `/tmp/edgeagentbench_current_case.txt`，Agent 每次调用 mock MCP 时都会读取该 case。评测时只比较 Agent 最终传给 `run_task_on_node` 的 `target_global_id` 和 `ground_truth.node_id`。

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

--current-case-file PATH
  当前 case 指针文件。必须和启动 Agent 时的 EDGE_BENCH_CURRENT_CASE_FILE 一致。

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

--no-force-task-type
  默认会把 case.task_type 作为 form 字段传给 Agent，只测试开发板选择。
  加上该参数后，不强制任务类型，可同时测试任务识别和开发板选择。

--strict
  存在失败时返回非 0 exit code。
```

快速测试：

```bash
python3 EdgeAgentBench/scripts/run_agent_selection_benchmark.py \
  --case-pattern 'mobilenet_001_*.json' \
  --limit 5 \
  --failures 5
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
