#!/bin/bash
# Experiment driver. Set REPO (checkout root), DML_PYTHON (onnxruntime-directml interpreter) and SCRATCH (work dir) first.
# usage: run_e2e.sh <name> <python> [ENV=val ...]
name=$1; py=$2; shift 2
mkdir -p $SCRATCH/e2e/$name
cp -r $SCRATCH/src_backyard/. $SCRATCH/e2e/$name/
cd "$REPO"
env "$@" PYTHONIOENCODING=utf8 "$py" analyzer/cli.py $SCRATCH/e2e/$name --gpu --parallel-prefetch 3 > $SCRATCH/e2e/$name.log 2>&1
echo "exit $?" >> $SCRATCH/e2e/$name.log
