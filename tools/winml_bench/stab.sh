#!/bin/bash
# Experiment driver. Set REPO (checkout root), DML_PYTHON (onnxruntime-directml interpreter) and SCRATCH (work dir) first.
cd "$REPO"
V312="$DML_PYTHON"
WM="$PWD/.venv-winml/Scripts/python.exe"
S=$SCRATCH/stab_status.txt
echo "dml start $(date +%T)" > $S
PYTHONIOENCODING=utf8 "$V312" analyzer/cli.py $SCRATCH/stab --gpu --parallel-prefetch 3 > $SCRATCH/stab_dml.log 2>&1
echo "dml done $(date +%T) exit $?" >> $S
mv $SCRATCH/stab/.kestrel $SCRATCH/stab_kestrel_dml
echo "ov start $(date +%T)" >> $S
KESTREL_WINML_DEVICE=ov_gpu PYTHONIOENCODING=utf8 "$WM" analyzer/cli.py $SCRATCH/stab --gpu --parallel-prefetch 3 > $SCRATCH/stab_ov.log 2>&1
echo "ov done $(date +%T) exit $?" >> $S
