#!/bin/sh
# Sequential benchmark matrix. Run under a heavy-lock slot.
export TMPDIR=/var/tmp/gliner-tmp HF_HOME=/var/tmp/gliner-tmp/hf
P=/var/tmp/gliner-tmp/venv/bin/python
cd "$(dirname "$0")"
G="urchade/gliner_multi_pii-v1"
S="12:1" # placeholder
export BENCH_SIZES=35:12,300:8,1100:5,4500:3
REDACTION_NLP_ENGINE=spacy REDACTION_SPACY_MODELS=it=it_core_news_md taskset -c 0 $P bench.py spacy-md-1c
REDACTION_NLP_ENGINE=spacy REDACTION_SPACY_MODELS=it=it_core_news_lg taskset -c 0 $P bench.py spacy-lg-1c
export BENCH_SIZES=35:5,300:3,1100:2,4500:1
REDACTION_NLP_ENGINE=gliner REDACTION_GLINER_MODEL=$G taskset -c 0 $P bench.py gliner-torch-1c
REDACTION_NLP_ENGINE=gliner REDACTION_GLINER_MODEL=$G taskset -c 0-3 $P bench.py gliner-torch-4c
REDACTION_NLP_ENGINE=gliner REDACTION_GLINER_MODEL=onnx-community/gliner_multi_pii-v1 REDACTION_GLINER_ONNX_FILE=onnx/model.onnx taskset -c 0 $P bench.py gliner-onnx-1c
REDACTION_NLP_ENGINE=gliner REDACTION_GLINER_MODEL=onnx-community/gliner_multi_pii-v1 REDACTION_GLINER_ONNX_FILE=onnx/model.onnx taskset -c 0-3 $P bench.py gliner-onnx-4c
ATEN_CPU_CAPABILITY=default REDACTION_NLP_ENGINE=gliner REDACTION_GLINER_MODEL=$G taskset -c 0 $P bench.py gliner-torch-1c-noavx
REDACTION_NLP_ENGINE=spacy REDACTION_SPACY_MODELS=it=it_core_news_md REDACTION_GLINER_MODEL=$G REDACTION_GLINER_LABELS="medical condition=MEDICAL_CONDITION,medication=MEDICATION,health insurance number=HEALTH_INSURANCE_ID" taskset -c 0 $P bench.py hybrid-md-gliner-medical-1c
echo ALLDONE
