#!/bin/sh
export TMPDIR=/var/tmp/gliner-tmp HF_HOME=/var/tmp/gliner-tmp/hf
P=/var/tmp/gliner-tmp/venv/bin/python
cd "$(dirname "$0")"
export BENCH_SIZES=35:5,300:3,1100:2,4500:1
M="REDACTION_NLP_ENGINE=gliner"
env $M REDACTION_GLINER_MODEL=onnx-community/gliner_multi_pii-v1 REDACTION_GLINER_ONNX_FILE=onnx/model.onnx ONNX_THREADS=1 taskset -c 0 $P bench.py gliner-onnx-1t
env $M REDACTION_GLINER_MODEL=onnx-community/gliner_multi_pii-v1 REDACTION_GLINER_ONNX_FILE=onnx/model.onnx ONNX_THREADS=4 taskset -c 0-3 $P bench.py gliner-onnx-4t
echo ALLDONE
