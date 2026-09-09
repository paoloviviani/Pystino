"""Knowledge bases: documents in, passages out, and the money in between.

The pipeline is **extract → chunk → embed → store**, and each stage is a
separate module because each is separately configurable by an administrator and
separately expensive:

* ``chunking`` is pure text and costs nothing;
* extraction goes through ``/v1/ocr`` and is charged **per page**;
* embedding goes through ``/v1/embeddings`` and is charged **per token**;
* ``store`` is the vector index, and is free but not fast.

The reason extraction and embedding go out through this gateway's own ``/v1``
surfaces rather than straight to a provider is ADR 0020's sharpest consequence:
indexing is billable, and a large ingestion run can cost more than the chat
traffic it serves. Calling an embedding endpoint directly would make that spend
invisible to every quota and every report — which is the one failure this
project exists to prevent.
"""
