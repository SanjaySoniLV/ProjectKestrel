#!/bin/bash
# Experiment driver. Set REPO (checkout root), DML_PYTHON (onnxruntime-directml interpreter) and SCRATCH (work dir) first.
# Timing block: per-model latency (DirectML 1.24.4 vs Windows ML) then 2x end-to-end each. Writes progress to timing_status.txt
WT="$REPO"
V312="$DML_PYTHON"
WM="$REPO/.venv-winml/Scripts/python.exe"
B=$SCRATCH/bench
S=$SCRATCH/timing_status.txt
mkdir -p $SCRATCH/bench
FEEDS=$SCRATCH/feeds.pkl
M="detector_mdv5a,sam_enc,sam_dec,bioclip_wdq16,siglip_wdq16,siglip_text,quality_legacy"
echo "start $(date +%T)" > $S
cd "$WT"
"$V312" tools/winml_bench/run_matrix.py --python "$V312" --devices dml --models $M --feeds $FEEDS --outdir $B/v312 --runs 20 --warmup 3 --timeout 600 > $SCRATCH/bench_v312_dml.log 2>&1
echo "v312 dml models done $(date +%T)" >> $S
"$V312" tools/winml_bench/run_matrix.py --python "$V312" --devices cpu --models $M,bioclip_int8,siglip_int8 --feeds $FEEDS --outdir $B/v312 --runs 8 --warmup 2 --timeout 600 > $SCRATCH/bench_v312_cpu.log 2>&1
echo "v312 cpu models done $(date +%T)" >> $S
"$WM" tools/winml_bench/run_matrix.py --python "$WM" --devices dml --models $M --feeds $FEEDS --outdir $B/winml --runs 20 --warmup 3 --timeout 600 > $SCRATCH/bench_winml_dml.log 2>&1
"$WM" tools/winml_bench/run_matrix.py --python "$WM" --devices ov_gpu --models detector_mdv5a,sam_enc,bioclip_wdq16,siglip_wdq16,siglip_text --feeds $FEEDS --outdir $B/winml --runs 20 --warmup 3 --timeout 600 > $SCRATCH/bench_winml_ovgpu.log 2>&1
"$WM" tools/winml_bench/run_matrix.py --python "$WM" --devices ov_npu --models sam_dec,quality_legacy,bioclip_wdq16,siglip_wdq16 --feeds $FEEDS --outdir $B/winml --runs 20 --warmup 3 --timeout 900 > $SCRATCH/bench_winml_npu.log 2>&1
echo "winml models done $(date +%T)" >> $S
cd $SCRATCH
for cfg in dml_t1 ov_t1 ov_t2 dml_t2; do
  case $cfg in
    dml*) ./run_e2e.sh $cfg "$V312" X=1 ;;
    ov*)  ./run_e2e.sh $cfg "$WM" KESTREL_WINML_DEVICE=ov_gpu ;;
  esac
  echo "e2e $cfg done $(date +%T)" >> $S
done
echo "ALL DONE $(date +%T)" >> $S
