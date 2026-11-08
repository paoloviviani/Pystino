"""Download the extractor's model weights at image build time, verified.

    python fetch_models.py --target /opt/extractor-models --ocr      # RapidOCR only
    python fetch_models.py --target /opt/extractor-models --docling  # + layout, tables

Reproducible and offline afterwards: Hugging Face repositories are fetched at the
commit hashes in `models.lock.json`, RapidOCR's files from the URL pinned there,
and **every file is checked against its SHA-256** before the build continues. At
runtime nothing is downloaded (the service sets HF_HUB_OFFLINE and points
Docling and RapidOCR at this directory).

Layout (what Docling and `llmp_redaction.ocr` expect):

    <target>/RapidOcr/*.onnx
    <target>/docling-project--docling-layout-heron/
    <target>/docling-project--docling-models/model_artifacts/tableformer/accurate/

The weights are licensed separately from the code: the notice this prints is
informational, and `NOTICE.md` (written beside the weights) travels with the image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any

LOCK = Path(__file__).with_name("models.lock.json")

NOTICE = """
  Extractor model weights and their licences
  ------------------------------------------
  RapidOCR / PP-OCRv6 ............ Apache-2.0
  docling-layout-heron ........... Apache-2.0
  docling-models (TableFormer v1 table structure weights):
      CDLA-Permissive-2.0 (the model card at the pinned revision also lists
      Apache-2.0). This image includes those weights under CDLA-Permissive-2.0,
      which asks that its text travel with them: it is in
      <target>/CDLA-Permissive-2.0.txt, with NOTICE.md listing every model,
      revision and licence. See docs/extractor-engines.md.
"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, expected: str) -> None:
    actual = sha256(path)
    if actual != expected:
        raise SystemExit(f"SHA-256 mismatch for {path}: expected {expected}, got {actual}")


def fetch_rapidocr(lock: dict[str, Any], target: Path) -> None:
    folder = target / "RapidOcr"
    folder.mkdir(parents=True, exist_ok=True)
    for name, entry in lock["rapidocr"]["files"].items():
        path = folder / name
        if not path.is_file():
            print(f"fetching {name}", file=sys.stderr)
            with urllib.request.urlopen(entry["url"], timeout=120) as response:
                path.write_bytes(response.read())
        verify(path, entry["sha256"])


def fetch_docling(lock: dict[str, Any], target: Path) -> None:
    from huggingface_hub import snapshot_download

    for repo, entry in lock["huggingface"].items():
        folder = target / repo.replace("/", "--")
        print(f"fetching {repo}@{entry['revision'][:12]}", file=sys.stderr)
        snapshot_download(
            repo_id=repo,
            revision=entry["revision"],
            local_dir=folder,
            allow_patterns=entry["allow_patterns"],
        )
        for name, expected in entry["files"].items():
            verify(folder / name, expected)
        # Docling reads the cache bookkeeping as clutter; drop it.
        shutil.rmtree(folder / ".cache", ignore_errors=True)


def write_notice(lock: dict[str, Any], target: Path, docling: bool) -> None:
    """NOTICE.md beside the weights: each model, where it came from, its revision
    and licence, and (Docling) the text of CDLA-Permissive-2.0, which that licence
    asks to accompany the weights. Generated from the lock, so it cannot drift."""
    lines = [
        "# Model weights in this image",
        "",
        "Each file below is baked in at build time from the pinned revision and verified by",
        "SHA-256 (`models.lock.json`). The code around them is Apache-2.0 (Pystino).",
        "",
        "| Model | Source | Revision | Licence |",
        "|---|---|---|---|",
    ]
    ocr = lock["rapidocr"]
    lines.append(
        f"| PP-OCRv6 detection and recognition (RapidOCR ONNX conversion of PaddleOCR models) | "
        f"{ocr['source']} | v3.10.0 | {ocr['licence']} |"
    )
    if docling:
        for repo, entry in lock["huggingface"].items():
            source = f"https://huggingface.co/{repo}"
            lines.append(f"| {repo} | {source} | {entry['revision']} | {entry['licence']} |")
        lines += [
            "",
            "## Attribution",
            "",
            "TableFormer table-structure weights (`docling-project/docling-models`) are by the",
            "Docling Project (IBM Research and contributors), distributed under the Community",
            "Data License Agreement - Permissive, Version 2.0, whose text is in",
            "`CDLA-Permissive-2.0.txt` next to this file. Layout weights",
            "(`docling-project/docling-layout-heron`) are Apache-2.0 by the same project.",
        ]
        shutil.copy(LOCK.with_name("CDLA-Permissive-2.0.txt"), target / "CDLA-Permissive-2.0.txt")
    (target / "NOTICE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--ocr", action="store_true", help="RapidOCR weights")
    parser.add_argument("--docling", action="store_true", help="RapidOCR + Docling weights")
    args = parser.parse_args()
    if not (args.ocr or args.docling):
        parser.error("name --ocr or --docling")
    lock = json.loads(LOCK.read_text())
    args.target.mkdir(parents=True, exist_ok=True)
    fetch_rapidocr(lock, args.target)
    if args.docling:
        print(NOTICE, file=sys.stderr)
        fetch_docling(lock, args.target)
    write_notice(lock, args.target, args.docling)


if __name__ == "__main__":
    main()
