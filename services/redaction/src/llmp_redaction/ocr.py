"""Reading pictures of text with RapidOCR.

RapidOCR (Apache-2.0) runs PaddleOCR's PP-OCRv6 detection and recognition
models (Apache-2.0, converted to ONNX by RapidOCR) on onnxruntime (MIT). The
v6 "small" recognizer is one multilingual model that covers Italian and English
together, accents included, so there is no per-language switch to get wrong.

Offline by construction: the model files are read from
``EXTRACTOR_MODELS_DIR/RapidOcr`` and RapidOCR's own downloader is never
reached, because its default is to fetch from the internet at first use. A
deployment whose image lacks the files gets `OcrUnavailable`, which the caller
reports as "this extractor cannot read scans" rather than a hang or a download.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Final

from llmp_redaction.engines import Settings, settings

if TYPE_CHECKING:
    from PIL import Image

logger = logging.getLogger(__name__)

DET_FILE: Final = "PP-OCRv6_det_small.onnx"
REC_FILE: Final = "PP-OCRv6_rec_small.onnx"

#: A bitmap larger than this is downscaled before detection; recognition of a
#: 300-DPI A4 page does not improve past about this and cost grows with area.
MAX_SIDE: Final = 2600

OCR_MISSING: Final = "OCR is not available in this extractor image"


class OcrUnavailable(Exception):
    """RapidOCR or its model files are not in this image."""


_lock = threading.Lock()
_engine: Any = None
_engine_key: tuple[object, ...] | None = None


def available(config: Settings | None = None) -> bool:
    """Whether OCR can run here: the library imports and the weights are present."""
    config = config or settings()
    folder = config.models_dir / "RapidOcr"
    if not ((folder / DET_FILE).is_file() and (folder / REC_FILE).is_file()):
        return False
    try:
        import rapidocr  # noqa: F401
    except ImportError:
        return False
    return True


def _load(config: Settings) -> Any:
    global _engine, _engine_key
    key = (config.models_dir, config.threads)
    if _engine is not None and key == _engine_key:
        return _engine
    if not available(config):
        raise OcrUnavailable(OCR_MISSING)
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import OCRVersion

    folder = config.models_dir / "RapidOcr"
    _engine = RapidOCR(
        params={
            "Global.use_cls": False,
            "Global.log_level": "warning",
            "Det.ocr_version": OCRVersion.PPOCRV6,
            "Det.model_path": str(folder / DET_FILE),
            "Rec.ocr_version": OCRVersion.PPOCRV6,
            "Rec.model_path": str(folder / REC_FILE),
            "EngineConfig.onnxruntime.intra_op_num_threads": config.threads,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        }
    )
    _engine_key = key
    return _engine


def read_image(image: Image.Image, config: Settings | None = None) -> str:
    """Text in one picture, lines in reading order, one per line.

    Serialised: one ONNX session shared by every request, and a second page
    running beside the first would double the memory and the thread count the
    operator capped.
    """
    import numpy as np

    config = config or settings()
    rgb = image.convert("RGB")
    longest = max(rgb.size)
    if longest > MAX_SIDE:
        factor = MAX_SIDE / longest
        rgb = rgb.resize((max(1, int(rgb.width * factor)), max(1, int(rgb.height * factor))))
    with _lock:
        engine = _load(config)
        result = engine(np.asarray(rgb)[:, :, ::-1])  # RapidOCR takes BGR, like OpenCV
    texts = getattr(result, "txts", None) or ()
    return "\n".join(str(line) for line in texts if str(line).strip())
