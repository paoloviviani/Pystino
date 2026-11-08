# PII detection service.
#
# The default build is MIT-licensed throughout. Adding the Italian NER model is a
# deliberate, per-deployment decision because those weights are CC BY-NC-SA 3.0
# (non-commercial, share-alike) — see docs/adr/0026-pluggable-detection.md:
#
#   docker build --build-arg SPACY_MODELS="en_core_web_lg it_core_news_lg" .
#
# For a small box the medium models (en_core_web_md, it_core_news_md) are about
# 40 MB each, roughly twice as fast as lg and somewhat less accurate on names.
#
# The extractor variants (docs/extractor-engines.md) add scan reading and the
# Docling engine, and are built locally, never published:
#
#   --build-arg EXTRACTOR=ocr      RapidOCR for scans and pictures. Every weight
#                                  in it is Apache-2.0.
#   --build-arg EXTRACTOR=docling  + Docling (layout, tables). Its table weights
#                                  carry CDLA-Permissive-2.0 (the build prints
#                                  the notice); torch is CPU-only.
#
# Italian *identifiers* (fiscal code, VAT, ID card, driving licence, passport) are
# pattern and checksum recognisers and work without any Italian model. Only
# Italian personal and place names need one.

FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.4 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# The repo layout is mirrored rather than flattened, because pyproject.toml
# points at the shared contract package with a relative path and uv refuses a
# path that escapes the project root.
WORKDIR /src/services/redaction

# Manifests first, so the dependency layer caches independently of source changes.
COPY packages/shared-py /src/packages/shared-py
COPY services/redaction/pyproject.toml services/redaction/README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv \
    && VIRTUAL_ENV=/opt/venv uv pip install --no-cache .

# The extractor variant: none (the default image, unchanged), ocr or docling.
# Installed in its own layer, after the base tree, so the default build never
# sees it. Constrained by uv.lock (exported below) so a rebuild resolves to the
# versions this was tested with, and so torch is the CPU wheel.
ARG EXTRACTOR=none
COPY services/redaction/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    set -eux; \
    case "${EXTRACTOR}" in \
        none) exit 0 ;; \
        ocr|docling) ;; \
        *) echo "EXTRACTOR must be none, ocr or docling, not ${EXTRACTOR}" >&2; exit 1 ;; \
    esac; \
    uv export --frozen --no-hashes --no-emit-project --no-emit-package llmp-shared \
        --extra "${EXTRACTOR}" > /tmp/constraints.txt; \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache -c /tmp/constraints.txt ".[${EXTRACTOR}]"

# The weights: pinned revisions, SHA-256 verified, baked in so the service never
# downloads anything (scripts/models.lock.json). Prints the licence notice.
COPY services/redaction/scripts /src/scripts
RUN set -eux; \
    case "${EXTRACTOR}" in \
        none) mkdir -p /opt/extractor-models ;; \
        ocr) /opt/venv/bin/python /src/scripts/fetch_models.py --target /opt/extractor-models --ocr ;; \
        docling) /opt/venv/bin/python /src/scripts/fetch_models.py --target /opt/extractor-models --docling ;; \
    esac

# Space-separated spaCy models. Default is English only, which keeps the image
# MIT-licensed end to end. Building with SPACY_MODELS= (empty) installs no
# weights at all: pair that with REDACTION_NLP_ENGINE=disabled at runtime for a
# pattern-only deployment (cards, IBANs, phones, fiscal codes) that uses a
# fraction of the memory and reports the model-backed entities as unavailable
# rather than pretending to find them.
ARG SPACY_MODELS="en_core_web_lg"

# Downloaded from the pinned GitHub release URLs rather than `spacy download`,
# which resolves a "compatible" version at build time and therefore produces a
# different image tomorrow. The loop over an empty SPACY_MODELS downloads
# nothing, which is the whole no-NER build.
RUN set -eux; \
    for model in ${SPACY_MODELS}; do \
        case "${model}" in \
            en_core_web_lg) version=3.8.0 ;; \
            en_core_web_md) version=3.8.0 ;; \
            en_core_web_sm) version=3.8.0 ;; \
            it_core_news_lg) version=3.8.0 ;; \
            it_core_news_md) version=3.8.0 ;; \
            it_core_news_sm) version=3.8.0 ;; \
            *) echo "unknown model ${model}: add its pinned version here" >&2; exit 1 ;; \
        esac; \
        case "${model}" in \
            it_*) \
                echo "" >&2; \
                echo "  LICENCE NOTICE: ${model} is CC BY-NC-SA 3.0 —" >&2; \
                echo "  non-commercial and share-alike, unlike everything else in" >&2; \
                echo "  this image. You are choosing to accept that obligation for" >&2; \
                echo "  Italian name detection. See docs/adr/0026-pluggable-detection.md" >&2; \
                echo "" >&2; \
                ;; \
        esac; \
        VIRTUAL_ENV=/opt/venv uv pip install --no-cache \
            "https://github.com/explosion/spacy-models/releases/download/${model}-${version}/${model}-${version}-py3-none-any.whl"; \
    done

# Recorded in the image so the running service reports exactly what it loaded,
# and cannot claim a language whose weights are not present.
RUN set -eux; \
    languages=""; \
    for model in ${SPACY_MODELS}; do \
        case "${model}" in \
            en_*) languages="${languages}${languages:+,}en=${model}" ;; \
            it_*) languages="${languages}${languages:+,}it=${model}" ;; \
        esac; \
    done; \
    echo "REDACTION_SPACY_MODELS=${languages}" > /opt/venv/models.env


FROM python:3.13-slim AS runtime

# antiword reads legacy binary Word (.doc), which no Python library in the tree
# can (llmp_redaction/documents.py). GPL-2.0 and run as a separate process, never
# linked. It brings its own mapping files, including the UTF-8 one the extractor
# asks for, so Italian accents survive. Pinned by the base image's Debian
# release rather than by version, as Debian only carries one.
RUN apt-get update \
    && apt-get install -y --no-install-recommends antiword \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 1001 detect \
    && useradd --system --uid 1001 --gid detect --no-create-home detect

COPY --from=builder --chown=detect:detect /opt/venv /opt/venv
COPY --from=builder /opt/extractor-models /opt/extractor-models
COPY --chown=detect:detect services/redaction/src /app/src
# The contract package is installed into the venv as an editable path
# dependency, so the path it points at has to exist here too. Copied rather than
# installed non-editably because it keeps one pyproject.toml serving both the
# image and local development, and it is three files.
COPY --chown=detect:detect packages/shared-py /src/packages/shared-py

# What the build made this image into; the service reads it for its defaults
# (EXTRACTOR_OCR is on in `ocr` and `docling`), and nothing is ever fetched at
# runtime: the weights are in /opt/extractor-models.
ARG EXTRACTOR=none
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    REDACTION_PORT=8080 \
    EXTRACTOR_VARIANT=${EXTRACTOR} \
    EXTRACTOR_MODELS_DIR=/opt/extractor-models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HOME=/tmp

# Baked at build time from the models actually installed above.
RUN set -eux; cat /opt/venv/models.env >> /etc/environment

USER detect
EXPOSE 8080

# Startup loads the model, which takes seconds; the long start period stops the
# orchestrator killing a container that is merely still warming up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

# Reads models.env itself: ENV cannot be set from a build-time file, and an
# entrypoint that sources it keeps the value in one place — the layer that
# installed the models.
CMD ["sh", "-c", "export $(cat /opt/venv/models.env) && exec python -m llmp_redaction"]
