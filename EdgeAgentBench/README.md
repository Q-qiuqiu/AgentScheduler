# EdgeAgentBench

EdgeAgentBench is a local benchmark for evaluating an edge-agent scheduler on heterogeneous edge boards. It combines user queries, task catalog metadata, synthetic cluster states, and per-case ground truth.

## Directory Layout

```text
EdgeAgentBench/
├── catalog/
│   └── catalog.json              # Static task catalog used by the benchmark
├── cases/
│   ├── manifest.json             # Case file index and oracle metadata
│   └── state_001_q_cls_001.json  # One benchmark case per JSON file
├── queries/
│   ├── image_classification/     # Image classification queries
│   ├── object_dedection/         # Object detection queries
│   ├── resource/                 # Resource/catalog/overhead queries
│   ├── semantic_segmentation/    # Semantic segmentation queries
│   └── text_classification/      # Text classification queries
├── results/                      # Runner output CSV files
├── samples/
│   └── images/                   # Sample input images
├── scripts/
│   ├── generate_states.py        # Generate synthetic cluster states
│   ├── generate_cases.py         # Generate case JSON files from queries + states
│   ├── mock_mcp_server.py        # Mock MCP tool server for trace-only agent tests
│   ├── run_tool_call_benchmark.py # Tool-call trace evaluator
│   └── run_benchmark.py          # Offline benchmark runner
└── states/
    └── state_001.json            # Synthetic cluster load states
```

## Data Files

`queries/` contains natural-language user requests. Each query has an `expected_task`, and some queries include scheduling constraints:

- `exclude_nodes`: node ids or device types that cannot be selected
- `require_node`: one node id or device type that must be selected from
- `require_nodes`: a list of allowed node ids or device types

`catalog/catalog.json` defines supported task types and which nodes can run them.

`states/` contains 100 synthetic cluster-load scenarios. Each state has four nodes:

- `RK3588_1`
- `RK3588_2`
- `ATLAS_L`
- `ATLAS_H`

Each node includes:

- `online`
- `cpu_used`
- `mem_used`
- `npu_used`
- `support_tasks`
- `ready_containers`
- `network_latency`

`network_latency` is currently fixed to `1` in all states and is not used by the benchmark oracle.

`cases/` contains generated benchmark cases. Each case combines one state and one query. File names use:

```text
state_001_q_cls_001.json
```

The `ground_truth` field contains the expected tool path and selected node.

## Ground Truth Rules

Execution tasks are:

- `object_detection`
- `image_classification`
- `semantic_segmentation`
- `text_classification`

For execution tasks, ground truth requires scheduling:

```json
"expected_tools": ["get_task_catalog", "schedule_task", "run_task_on_node"]
```

The case oracle filters candidates by:

- node is online
- task is supported by `catalog/catalog.json`
- task is present in the state node's `support_tasks`
- query constraints are respected

Then it scores candidates with:

```text
score = 0.50 * npu_used
      + 0.20 * cpu_used
      + 0.20 * mem_used
      + 0.10 * cold_start_penalty
```

`cold_start_penalty` is `0` when the target task is in `ready_containers`, otherwise `1`.

Resource queries do not schedule tasks. Their ground truth selects a read-only tool:

- current nodes or current resource usage: `get_cluster_resources`
- supported AI tasks/models: `get_task_catalog`
- startup overhead/resource cost: `get_task_overhead`

## State Scenarios

The 100 states are generated from 25 scenario families with 4 variants each. The variants change load intensity and container warm-start placement while keeping the same scenario objective.

| State range | Scenario family | Test objective |
|---|---|---|
| `state_001`-`state_004` | Global low load | Validate baseline scheduling under light load and warm containers |
| `state_005`-`state_008` | Global medium load | Validate steady-state scheduling under normal cluster usage |
| `state_009`-`state_012` | `RK3588_1` high NPU load | Avoid a single RK accelerator hotspot |
| `state_013`-`state_016` | `RK3588_2` high NPU load | Migrate between homogeneous RK boards |
| `state_017`-`state_020` | `ATLAS_L` high NPU load | Avoid Atlas_L for segmentation/text workloads when busy |
| `state_021`-`state_024` | `ATLAS_H` high NPU load | Fall back from the high-performance Atlas board when saturated |
| `state_025`-`state_028` | `RK3588_1` memory pressure | Check memory feasibility before cold start |
| `state_029`-`state_032` | `RK3588_2` memory pressure | Avoid memory-constrained homogeneous RK nodes |
| `state_033`-`state_036` | `ATLAS_L` memory pressure | Switch Atlas-only workloads away from memory pressure |
| `state_037`-`state_040` | `ATLAS_H` memory pressure | Fall back when the preferred Atlas board lacks memory |
| `state_041`-`state_044` | `RK3588_1` high CPU load | Test CPU saturation in the combined score |
| `state_045`-`state_048` | `RK3588_2` high CPU load | Avoid RK CPU hotspots |
| `state_049`-`state_052` | `ATLAS_L` high CPU load | Evaluate CPU-side overhead on Atlas_L |
| `state_053`-`state_056` | `ATLAS_H` high CPU load | Evaluate candidate switching when Atlas_H CPU is busy |
| `state_057`-`state_060` | `RK3588_1` offline | Test single RK board failure recovery |
| `state_061`-`state_064` | `RK3588_2` offline | Test the other RK board failure recovery |
| `state_065`-`state_068` | `ATLAS_L` offline | Route segmentation/text tasks to Atlas_H |
| `state_069`-`state_072` | `ATLAS_H` offline | Route segmentation/text tasks to Atlas_L |
| `state_073`-`state_076` | All containers cold | Measure pure resource and overhead-based scheduling |
| `state_077`-`state_080` | All task containers warm | Select the best node among warm candidates |
| `state_081`-`state_084` | RK pair vision hotspot | Move vision workloads from hot RK boards to Atlas candidates |
| `state_085`-`state_088` | Atlas pair AI hotspot | Handle segmentation/text pressure on Atlas-only workloads |
| `state_089`-`state_092` | Memory fragmentation | Filter multiple near-memory-limit nodes |
| `state_093`-`state_096` | NPU fragmentation | Filter multiple near-NPU-limit nodes |
| `state_097`-`state_100` | Mixed CPU/MEM/NPU stress | Evaluate scheduler behavior under realistic mixed pressure |

## Generate Cases

Run this after editing `queries/`, `states/`, or `catalog/`.

Generate the 100 synthetic states:

```bash
cd /root/AgentScheduler/EdgeAgentBench
python3 scripts/generate_states.py
```

Expected output:

```text
wrote 100 states to /root/AgentScheduler/EdgeAgentBench/states
```

Generate cases from the current queries and states:

```bash
cd /root/AgentScheduler/EdgeAgentBench
python3 scripts/generate_cases.py
```

This writes:

- `cases/manifest.json`
- 2500 case files, one JSON file per case

Expected output:

```text
wrote 2500 case files to /root/AgentScheduler/EdgeAgentBench/cases
```

## Run Benchmark

Run the offline scheduler-style benchmark:

```bash
cd /root/AgentScheduler/EdgeAgentBench
python3 scripts/run_benchmark.py --policy scheduler
```

## Run Tool-Call Benchmark

This experiment checks whether the local agent calls the expected tools for each case. It does not require the real gateway. Start the agent with the mock MCP server:

```bash
cd /root/AgentScheduler
MCP_SERVER_PATH=/root/AgentScheduler/EdgeAgentBench/scripts/mock_mcp_server.py \
python3 agent/server/agent.py --host 0.0.0.0 --port 8082
```

Then run the tool-call evaluator:

```bash
cd /root/AgentScheduler/EdgeAgentBench
python3 scripts/run_tool_call_benchmark.py \
  --agent-url http://127.0.0.1:8082/v1/agent/chat \
  --output results/tool_call_results.csv
```

For a quick smoke test:

```bash
python3 scripts/run_tool_call_benchmark.py --limit 20 --failures 5
```

The evaluator sends each case query to `/v1/agent/chat` with `debug=1`, extracts `trace[].action`, removes `final_response`, and compares the resulting tool sequence with:

```json
ground_truth.expected_tools
```

For overhead queries, it also checks:

```json
ground_truth.expected_tool_arguments
```

Matching modes:

```bash
# Strict: actual tool sequence must exactly equal ground truth
python3 scripts/run_tool_call_benchmark.py --match exact

# Lenient: ground truth must appear as an ordered subsequence
python3 scripts/run_tool_call_benchmark.py --match subsequence
```

The output metric is `tool_call_accuracy`.

Default output:

```text
results/runner_results.csv
```

Use a custom output path:

```bash
python3 scripts/run_benchmark.py \
  --policy scheduler \
  --output results/scheduler_results.csv
```

Print fewer failure examples:

```bash
python3 scripts/run_benchmark.py --policy scheduler --failures 3
```

Return non-zero when any case fails:

```bash
python3 scripts/run_benchmark.py --policy scheduler --strict
```

## Runner Policies

`ground_truth`

Reads the expected node directly from each case. This should always reach 100% and is useful for checking file loading and result writing.

```bash
python3 scripts/run_benchmark.py --policy ground_truth
```

`case_oracle`

Recomputes the benchmark oracle from case + state. This should also reach 100% if cases were generated correctly.

```bash
python3 scripts/run_benchmark.py --policy case_oracle
```

`scheduler`

Offline simulation of the current scheduler logic. It maps abstract benchmark tasks to real model names:

```text
object_detection      -> YoloV5
image_classification  -> MobileNet
semantic_segmentation -> deeplabv3
text_classification   -> Bert
```

It applies static overhead from `/root/AgentScheduler/config_files/static_info.json` during cold start, checks CPU/MEM/XPU feasibility, and selects the node with the smallest `cpu_after + npu_after + 0.1 * mem_after`.

```bash
python3 scripts/run_benchmark.py --policy scheduler
```

## Notes

- `case` files are benchmark samples, not direct `schedule_task` inputs.
- `schedule_task` should receive only a task type, for example `{"task_type": "YoloV5"}`.
- `state` is used by the benchmark runner to simulate cluster load.
- `ground_truth` is used only by the evaluator and should not be provided to the agent or scheduler.
- The current runner is offline. To test the live HTTP gateway, a future runner should add a state-injection or mock-gateway layer before calling `/schedule_task`.
