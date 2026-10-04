"""The `/extract` surface.

The endpoint's own promise, as distinct from the extractor's: an unreadable
document is a 200 with a reason, never a 4xx. A caller has to decide whether to
forward the attachment either way, and an HTTP error makes "we could not read
this" indistinguishable from "the extractor is down" — at which point a
fail-open caller forwards an uninspected file and a fail-closed one refuses
every request while the service is healthy.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from llmp_redaction.app import create_app
from llmp_shared import ExtractionKind
from test_documents import DOCX, IBAN, docx_bytes, pdf_bytes

# markitdown is services/redaction's dependency, not the gateway's, and the root
# suite deliberately runs this directory (pyproject's `testpaths`) so these tests
# do not rot in a corner nobody executes. In the gateway's environment the
# extractor's library is absent, so these skip — visibly, with a reason — and
# run for real under `uv run --python 3.13 pytest` in services/redaction, which
# is where the 37-package tree is installed.
pytest.importorskip(
    "markitdown",
    reason="document extraction needs services/redaction's own environment",
)


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    # No NER: extraction never needs a model, and loading ~900 MB of spaCy to
    # test a zip reader would make this suite unrunnable on the small host.
    import os

    os.environ["REDACTION_NLP_ENGINE"] = "disabled"
    with TestClient(create_app()) as started:
        yield started


def test_a_word_document_comes_back_as_text(client: TestClient) -> None:
    response = client.post(
        "/extract", content=docx_bytes(f"Invoice {IBAN}"), headers={"content-type": DOCX}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == ExtractionKind.TEXT
    assert IBAN in body["text"]
    assert body["extractor"] == "markitdown"


def test_a_pdf_reports_its_page_count(client: TestClient) -> None:
    response = client.post(
        "/extract", content=pdf_bytes("hello"), headers={"content-type": "application/pdf"}
    )
    assert response.json()["pages"] == 1


def test_a_scanned_pdf_is_a_200_that_says_it_was_not_read(client: TestClient) -> None:
    """The whole reason the endpoint answers 200 for a failure."""
    response = client.post(
        "/extract", content=pdf_bytes(None), headers={"content-type": "application/pdf"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == ExtractionKind.NO_TEXT_LAYER
    assert body["text"] == ""
    # It still has a page: a caller deciding what to do wants to know whether
    # this is one page or two hundred.
    assert body["pages"] == 1


def test_an_unreadable_document_is_a_200_with_a_reason(client: TestClient) -> None:
    response = client.post(
        "/extract", content=b"PK\x03\x04 nonsense", headers={"content-type": DOCX}
    )
    assert response.status_code == 200
    assert response.json()["kind"] == ExtractionKind.UNREADABLE


def test_the_filename_header_rescues_an_octet_stream(client: TestClient) -> None:
    """Several clients send `application/octet-stream` for every upload."""
    response = client.post(
        "/extract",
        content=docx_bytes("hello"),
        headers={"content-type": "application/octet-stream", "x-filename": "notes.docx"},
    )
    assert response.json()["kind"] == ExtractionKind.TEXT


def test_an_unsupported_type_is_named_not_guessed(client: TestClient) -> None:
    response = client.post(
        "/extract", content=b"\x89PNG\r\n", headers={"content-type": "image/png"}
    )
    body = response.json()
    assert body["kind"] == ExtractionKind.UNSUPPORTED
    assert "image/png" in body["detail"]


def test_a_legacy_word_file_labelled_docx_is_read_or_explained(client: TestClient) -> None:
    """The reported case, end to end: the response body carries either the text
    or the specific reason — never a bare 'unsupported'."""
    import shutil

    from legacy_office import FIXTURE

    response = client.post(
        "/extract",
        content=FIXTURE.read_bytes(),
        headers={"content-type": DOCX, "x-filename": "menu.doc.docx"},
    )
    assert response.status_code == 200
    body = response.json()
    if shutil.which("antiword"):
        assert body["kind"] == ExtractionKind.TEXT
        assert "caffè" in body["text"]
    else:
        assert body["kind"] == ExtractionKind.UNSUPPORTED
        assert "antiword" in body["detail"]


def test_a_refusal_reason_is_in_the_response_body(client: TestClient) -> None:
    from legacy_office import ppt_bytes

    response = client.post(
        "/extract", content=ppt_bytes(), headers={"content-type": "application/vnd.ms-powerpoint"}
    )
    body = response.json()
    assert body["kind"] == ExtractionKind.UNSUPPORTED
    assert ".ppt" in body["detail"] and ".pptx" in body["detail"]


def test_page_images_are_returned_only_when_asked_for(client: TestClient) -> None:
    from test_documents import scan_pdf

    pdf = scan_pdf(2, (200, 300))
    plain = client.post("/extract", content=pdf, headers={"content-type": "application/pdf"})
    assert plain.json()["page_images"] == []

    asked = client.post(
        "/extract",
        content=pdf,
        headers={
            "content-type": "application/pdf",
            "x-page-images": "20",
            "x-page-image-long-side": "100",
        },
    )
    body = asked.json()
    assert body["kind"] == ExtractionKind.NO_TEXT_LAYER
    assert [image["page"] for image in body["page_images"]] == [1, 2]
    assert body["pages"] == 2 and body["images_truncated"] is False

    junk = client.post(
        "/extract",
        content=pdf,
        headers={"content-type": "application/pdf", "x-page-images": "lots"},
    )
    assert junk.json()["page_images"] == []
