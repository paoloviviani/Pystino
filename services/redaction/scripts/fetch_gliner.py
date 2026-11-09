"""Bake a GLiNER checkpoint (and the encoder tokenizer it loads) into HF_HOME at build time.

Usage: fetch_gliner.py MODEL REVISION TOKENIZER_BASE TOKENIZER_REVISION

A pinned revision is stored under ``refs/main`` too, because at runtime the
service asks for ``main`` and HF_HUB_OFFLINE cannot resolve anything else.
"""

import os
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

model, revision, tokenizer_base, tokenizer_revision = sys.argv[1:5]
snapshot_download(
    model,
    revision=revision,
    allow_patterns=[
        "*.json", "*.model", "*.txt", "*.md", ".gitattributes", "pytorch_model.bin", "*.safetensors"
    ],
    ignore_patterns=["*.onnx"],
)
snapshot_download(
    tokenizer_base,
    revision=tokenizer_revision,
    allow_patterns=["*.json", "*.model", "*.txt", "*.md", ".gitattributes"],
)
for repo, pinned in ((model, revision), (tokenizer_base, tokenizer_revision)):
    refs = Path(os.environ["HF_HOME"]) / "hub" / f"models--{repo.replace('/', '--')}" / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    if pinned != "main":
        (refs / "main").write_text(pinned)
