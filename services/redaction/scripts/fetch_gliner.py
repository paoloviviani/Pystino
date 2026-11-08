"""Bake a GLiNER checkpoint (and the encoder tokenizer it loads) into HF_HOME at build time.

Usage: fetch_gliner.py MODEL REVISION TOKENIZER_BASE

A pinned revision is stored under ``refs/main`` too, because at runtime the
service asks for ``main`` and HF_HUB_OFFLINE cannot resolve anything else.
"""

import os
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

model, revision, tokenizer_base = sys.argv[1:4]
snapshot_download(
    model,
    revision=revision,
    allow_patterns=[
        "*.json", "*.model", "*.txt", "*.md", ".gitattributes", "pytorch_model.bin", "*.safetensors"
    ],
    ignore_patterns=["*.onnx"],
)
snapshot_download(
    tokenizer_base, allow_patterns=["*.json", "*.model", "*.txt", "*.md", ".gitattributes"]
)
refs = Path(os.environ["HF_HOME"]) / "hub" / f"models--{model.replace('/', '--')}" / "refs"
refs.mkdir(parents=True, exist_ok=True)
if revision != "main":
    (refs / "main").write_text(revision)
