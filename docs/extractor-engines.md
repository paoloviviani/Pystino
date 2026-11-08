# Document extractor engines

The `extractor` service reads documents for `/v1/ocr`, for redaction of
attachments and for the chat's knowledge base. It takes one document, answers
with text, and has the same API and response shape whichever engine reads it.
This page is about choosing the engine, what each reads, what each costs, and
the licences of everything that is in the image.

## The engines

| Engine | Image | Reads | Does not read |
|---|---|---|---|
| `markitdown` (default) | `-pattern`, unchanged and small (about 1.1 GB with the redaction service) | Word, Excel, PowerPoint, old `.xls`, old `.doc` (antiword), PDFs with a text layer | scanned pages (reported as `no_text_layer`, with page images if asked for), pictures |
| `markitdown` + OCR | `-ocr` (about 1.5 GB) | everything above, **plus** scanned PDF pages and PNG/JPEG pictures, by RapidOCR | tables in scans keep no structure; text only |
| `docling` | `-docling` (about 3.9 GB) | PDFs (text and scanned): layout, reading order, tables as Markdown, OCR by RapidOCR; Word, PowerPoint and Excel (OOXML) through Docling | old `.doc`, `.xls`, `.ppt`: these never go to Docling |

Which engine reads which format, with `EXTRACTOR_ENGINE=docling`:

| Format | Reader | If it fails or returns no text |
|---|---|---|
| PDF | Docling (RapidOCR for pages without a text layer) | markitdown path (with page-by-page OCR when OCR is on), which also draws page images |
| `.docx`, `.pptx`, `.xlsx` | Docling | markitdown |
| `.doc` (Word 97) | antiword | refused with a reason, as before |
| `.xls` | markitdown (xlrd) | refused with a reason |
| `.ppt`, encrypted Office files | refused with a reason, as before | |
| PNG, JPEG | RapidOCR (only when OCR is on; otherwise `unsupported`, as before) | |

`extractor` in the response names who actually read the document
(`markitdown`, `docling`, `pypdfium2+rapidocr`, `rapidocr`, `antiword`), and
`fallback_reason` says why it was not the engine asked for (`docling
unavailable`, `docling busy`, `docling timeout`, `docling failed`, `docling
returned no text`; empty when it was). Fixed phrases, never content. The
fallback readers share what is left of the document's time budget
(`EXTRACTOR_TIMEOUT_SECONDS`), they do not start a fresh one.

**Docling reads one document at a time**: the converter holds the models in
memory and two layouts racing on a small CPU are slower than one after the
other. A document that cannot get the converter within 60 seconds is read by the
other engines (`fallback_reason: docling busy`) rather than queuing behind a
slow one, but throughput for Docling is one document at a time, per container.
OCR on the markitdown path is serialised the same way.

## Choosing the engine

| Setting | Meaning |
|---|---|
| `EXTRACTOR_ENGINE` | `markitdown` (default) or `docling`. |
| `X-Extractor-Engine` request header | `markitdown`, `ocr` (markitdown with OCR switched on for this one request; `fallback_reason` says `ocr unavailable` on a build without the weights) or `docling`; anything else is ignored. The gateway sends it when a [privacy pipeline](privacy-routing.md) reads with the built-in extractor and names an engine, and never otherwise. |
| `EXTRACTOR_OCR` | `rapidocr` or `none`. Default: on in the `-ocr` and `-docling` images, off elsewhere. Docling always uses RapidOCR for scans. |
| `EXTRACTOR_THREADS` | Intra-op threads for ONNX and torch. Default: the cores this container may use (the CPU-affinity mask, which follows `--cpuset` and not a CPU quota: set it explicitly with `cpus:` limits). |
| `EXTRACTOR_MAX_PAGES` | Pages read by OCR or Docling per document (default 200). Further pages are not read and the response says `truncated`. Pages are counted by pdfium (pdfminer as a fallback); when neither can count, the cap is still passed to Docling. A page is drawn for OCR at 200 DPI but never larger than 2600 pixels on its long side, so a 200-inch page cannot become a multi-gigabyte bitmap. |
| `EXTRACTOR_TIMEOUT_SECONDS` | A soft time budget per document (default 300), checked between pages. A page already being read is not interrupted, so the real bound is this plus one page. Docling stops and returns what it has (`truncated`). |
| `EXTRACTOR_MODELS_DIR` | Where the baked weights are (`/opt/extractor-models`). |

The size limit (25 MB), the character cap (2,000,000) and the archive checks are
the same for every engine. A document the engine cannot use is handed to the
markitdown path; a document nobody can read gets the same `unreadable` or
`unsupported` answer as before, never a 4xx.

Nothing is ever downloaded at runtime. The weights are baked in at build time,
`HF_HUB_OFFLINE` is set, Docling's remote services and external plugins are off,
and an image missing a weight reports the engine as unavailable and falls back.
The gateway waits `GATEWAY_EXTRACTOR__TIMEOUT_SECONDS` (180 by default) for an
answer: raise it with `docling` on a small CPU (the kit sets 900).

## Building the images

Built locally for now, like `-ner` (publishing them is a separate decision, because of the 3.9 GB size; the licences below allow it):

```sh
docker build -f services/redaction/Dockerfile \
  --build-arg SPACY_MODELS= --build-arg EXTRACTOR=docling \
  --build-arg WEIGHTS_LICENCES="Apache-2.0; CDLA-Permissive-2.0 (TableFormer)" \
  -t pystino-redaction:<version>-docling .
```

`EXTRACTOR` is `none` (default), `ocr` or `docling`. The Python packages are
installed from `services/redaction/uv.lock` (so torch is the CPU wheel, not the
CUDA one) and the weights by `services/redaction/scripts/fetch_models.py` from
revisions pinned in `scripts/models.lock.json`, with every file checked against
its SHA-256. Wheels are installed with `--require-hashes` from the hashes in
`uv.lock`, so a tampered index or mirror fails the build. Each variant image
carries the label `dev.pystino.extractor-variant` (`ocr` or `docling`). The build prints the licence notice below. In the deploy kit:
`dev/build.sh --docling` (or `--ocr`), then `./configure --extractor docling`.

The extractor and the redaction service may use different images (the kit's
`EXTRACTOR_IMAGE` and `REDACTION_IMAGE`): the extractor needs no spaCy and the
redaction service needs no torch, so the variants are built with
`SPACY_MODELS=` and are separate from `-ner`.

## Licences

Checked at source on 2026-11-08 (package metadata of the versions in
`uv.lock`; model cards and repositories at the pinned revisions).

| Component | Version | Licence | OSI-approved |
|---|---|---|---|
| docling, docling-core, docling-parse, docling-ibm-models | 2.149.0, 2.106.0, 7.24.2, 4.0.4 | MIT | yes |
| rapidocr | 3.10.0 | Apache-2.0 | yes |
| onnxruntime | 1.20.1 | MIT | yes |
| torch (CPU), torchvision | 2.15.0, 0.29.2 | BSD-3-Clause (with Apache-2.0, MIT, BSL-1.0 and other permissive parts) | yes |
| transformers, huggingface-hub, tokenizers, safetensors | | Apache-2.0 | yes |
| opencv-python-headless | 5.0.0.93 | Apache-2.0 (OpenCV) and MIT (the packaging); the wheel **bundles FFmpeg shared libraries under LGPL-2.1** and OpenSSL 1.1.1k, neither used here | yes (LGPL-2.1 is OSI-approved, not GPL) |
| other dependencies (scipy, pandas, shapely, pyclipper, lxml, tqdm, certifi, …) | | BSD, MIT, Apache-2.0, MPL-2.0 (certifi, tqdm), PSF | yes |
| **Weights:** PP-OCRv6 detection and recognition (RapidOCR ONNX conversion of PaddleOCR models) | RapidOCR v3.10.0 | Apache-2.0 (RapidOCR README: the upstream models are Apache-2.0 and the converted ONNX files are redistributed under the same terms) | yes |
| **Weights:** `docling-project/docling-layout-heron` (layout) | `8f39ad3` | Apache-2.0 (model card) | yes |
| **Weights:** `docling-project/docling-models` (TableFormer v1, table structure) | `2199320` | model card front matter: `cdla-permissive-2.0` and `apache-2.0`; the `v2.3.0` revision Docling itself pins says `cdla-permissive-2.0` alone, for byte-identical weight files | CDLA-Permissive-2.0 is permissive and allows redistribution (its one condition: the agreement text accompanies the shared weights) but is not OSI-approved; the Apache-2.0 alternative is stated only on the newer card |

No AGPL or GPL component is in the image (checked by scanning the installed
package metadata). antiword, which the image already carried for `.doc`, is
GPL-2.0 and runs as a separate process.

What was **not** audited: the licences of the models' training data, and the
`MODEL_LICENSES.md` that RapidOCR's README points to (the link returns 404 at
the time of writing; the statement relied on is in its README).

**Policy.** The owner decided that a non-OSI licence is acceptable for shipped
weights when its terms allow redistribution, and CDLA-Permissive-2.0 does. So the
`-docling` image is publishable by design: it carries the licences with it.
`/opt/extractor-models/NOTICE.md` (generated from `scripts/models.lock.json`)
lists each model, its source, revision and licence, with the attribution;
`/opt/extractor-models/CDLA-Permissive-2.0.txt` is the agreement's text, which is
the licence's only condition for sharing; the image labels
`dev.pystino.weights-licences` and `dev.pystino.weights-notice` point to them. The
build prints the same information. The image is **built locally for now and may be
published later**; it is not in `images.yml`. Alternatives, should the choice be
reopened: TableFormerV2 (`docling-project/TableFormerV2`, Apache-2.0, but a
preview model), or no table structure recognition (`do_table_structure=False`).
The `-ocr` image has only Apache-2.0 weights.

## Measured performance

The 5-core build box (x86-64, AVX2), one process, Docling 2.149 and RapidOCR 3.10
with CPU torch and onnxruntime, models warm (`load_s` is the first-document
cost), on synthetic A4 pages (`scripts/bench_extractor.py`): a 10-page text
PDF, a 6-page two-column report with a ruled table, and 3 scanned pages
(150 DPI bitmaps of dense Italian text). Pages per minute; peak resident set of
the process.

| Document | Engine | 4 threads (4 cores) | 1 thread (1 core) | Peak memory |
|---|---|---|---|---|
| Text PDF | markitdown | 459 ppm | 461 ppm | 346 MB |
| Text PDF | docling | 48 ppm | 20 ppm | 1.8 GB |
| Two-column + table | markitdown | 1290 ppm | 1363 ppm | 347 MB |
| Two-column + table | docling | 26 ppm | 12 ppm | 1.7 GB |
| Scan | markitdown | 0 text (`no_text_layer`) | same | 339 MB |
| Scan | markitdown + OCR | 16 ppm | 6.3 ppm | 0.9-1.0 GB |
| Scan | docling | 12 ppm | 4.9 ppm | 1.9 GB |

First-use model load: 10-12 s at 4 threads, 21 s at 1 thread (Docling); 6-11 s
(OCR only). The service loads lazily on the first request and keeps the models.

**Estimate for a Celeron N5105** (4 cores, no AVX2, 0.3-0.5x the build box's
per-core speed), from the 4-thread column, so the time per page is 2 to 3.3
times longer: Docling about 16-24 ppm on text PDFs, 9-13 on two-column pages
with tables, 4-6 on scans (10-15 s a page); markitdown with OCR 5-8 ppm on
scans; markitdown on text PDFs still hundreds. A 30-page scanned report would
take about 5-8 minutes with Docling. That is an estimate, not a measurement: it
has not been run on that CPU, and a build of onnxruntime or torch that
falls back from AVX2 kernels could be slower than the scaling says. The first
document also pays 30-60 s of model loading. Quality over speed is the right
trade here because a document is read once, at upload; allow `EXTRACTOR_MEM_LIMIT`
of 4 GB and raise the gateway's wait.

## Tests

* `tests/test_engines.py`: engine selection (environment, header, unknown
  names), fallback per format with a fake Docling, OCR on scanned pages with a
  fake RapidOCR, the page cap, picture files. Runs everywhere.
* `tests/test_docling_integration.py`: the real engines on a generated Italian
  medical-style report (two columns, a ruled table, an image-only scanned page).
  Asserts reading order, the table as a Markdown table and the OCR text with its
  accents. Skipped unless `EXTRACTOR_MODELS_DIR` holds the weights
  (`scripts/fetch_models.py --target DIR --docling`); the variant images carry
  them, so `docker run --rm -e EXTRACTOR_MODELS_DIR=/opt/extractor-models ...`
  runs it there.
