#!/usr/bin/env bash
set -e

mkdir -p EdgeAgentBench/results

policies=(
  scheduler
  weight_schedule
  resource_average
  mem_min
  cpu_min
  npu_min
  container_first
  round_robin
)

for policy in "${policies[@]}"; do
  echo "Running policy: $policy"

  python3 EdgeAgentBench/scripts/run_benchmark.py \
    --policy "$policy" \
    --output "EdgeAgentBench/results/${policy}_results.csv"
done

echo "Running policy: random"

python3 EdgeAgentBench/scripts/run_benchmark.py \
  --policy random \
  --seed 42 \
  --output EdgeAgentBench/results/random_results.csv
