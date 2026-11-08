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
for the person building the image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

LOCK = Path(__file__).with_name("models.lock.json")

NOTICE = """
  LICENCE NOTICE (extractor model weights)
  ----------------------------------------
  RapidOCR / PP-OCRv6 .. Apache-2.0
  docling-layout-heron . Apache-2.0
  docling-models (TableFormer v1 table structure weights):
      the model card at the pinned revision declares
      "CDLA-Permissive-2.0 OR Apache-2.0"; the revision Docling itself pins
      declares CDLA-Permissive-2.0 alone, for the same weight files.
      CDLA-Permissive-2.0 is permissive but is not an OSI-approved licence.
      Building a Docling image means you accept it. Do not publish this image
      without that decision. See docs/extractor-engines.md.
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
        for clutter in (folder / ".cache",):
            if clutter.is_dir():
                import shutil

                shutil.rmtree(clutter)


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


if __name__ == "__main__":
    main()
