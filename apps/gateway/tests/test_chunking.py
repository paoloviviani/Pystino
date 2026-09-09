"""The chunker, which is pure logic and therefore has no excuse.

Retrieval quality is decided here and the failures are silent: a sentence cut in
half still embeds, still ranks, and still gets handed to a model. So these
assertions are about *boundaries* rather than about counts wherever possible —
a test that pins "this text makes 4 chunks" breaks on every tuning change and
proves nothing about whether the splits were in sensible places.
"""

from __future__ import annotations

import pytest
from gateway.knowledge.chunking import MIN_CHUNK_CHARS, chunk_markdown


def test_empty_text_produces_nothing() -> None:
    assert chunk_markdown("") == []
    assert chunk_markdown("   \n\n  \t ") == []


def test_short_text_is_one_chunk_and_keeps_its_words() -> None:
    chunks = chunk_markdown("Il gatto dorme sul tetto.")
    assert len(chunks) == 1
    assert chunks[0].text == "Il gatto dorme sul tetto."
    assert chunks[0].headings == ()
    assert chunks[0].ordinal == 0


def test_heading_path_is_prepended_so_a_passage_carries_its_context() -> None:
    text = "# 2026 results\n\n## Italy\n\nRevenue fell 4%.\n"
    chunks = chunk_markdown(text)
    assert len(chunks) == 1
    assert chunks[0].headings == ("2026 results", "Italy")
    assert "Revenue fell 4%." in chunks[0].text
    # The whole path, not just the nearest heading: "Italy" alone would not say
    # which year.
    assert "2026 results" in chunks[0].text
    assert "Italy" in chunks[0].text


def test_a_skipped_heading_level_does_not_lose_the_outer_heading() -> None:
    # h1 straight to h3 is extremely common in extracted documents.
    chunks = chunk_markdown("# Annual report\n\n### Notes\n\nSomething about notes.\n")
    assert chunks[0].headings == ("Annual report", "Notes")


def test_a_sibling_heading_replaces_rather_than_nests() -> None:
    text = "# Top\n\n## One\n\nFirst body.\n\n## Two\n\nSecond body.\n"
    chunks = chunk_markdown(text)
    paths = [chunk.headings for chunk in chunks]
    assert ("Top", "One") in paths
    assert ("Top", "Two") in paths
    # "One" must not still be in scope under "Two".
    for chunk in chunks:
        if "Second body." in chunk.text:
            assert "One" not in chunk.headings


def test_sections_are_not_merged_across_a_heading() -> None:
    # Two short sections: each is under a different path, so folding them
    # together would attach text to the wrong heading.
    text = "## Alpha\n\nShort one.\n\n## Beta\n\nShort two.\n"
    chunks = chunk_markdown(text)
    assert len(chunks) == 2
    assert chunks[0].headings == ("Alpha",)
    assert chunks[1].headings == ("Beta",)


def test_long_text_splits_at_sentence_boundaries_not_mid_sentence() -> None:
    sentence = "Questa frase ha una lunghezza prevedibile e finisce qui."
    text = " ".join([sentence] * 60)
    chunks = chunk_markdown(text, chunk_chars=300, chunk_overlap=0)
    assert len(chunks) > 1
    for chunk in chunks:
        stripped = chunk.text.strip()
        # Every chunk ends on a sentence terminator, which is the property that
        # a fixed-width slicer cannot give.
        assert stripped.endswith("."), stripped[-40:]


def test_a_single_sentence_longer_than_the_limit_is_cut_on_whitespace() -> None:
    # No sentence boundary anywhere, so the fallback has to cut. It must still
    # not cut inside a word.
    words = ["parola"] * 200
    chunks = chunk_markdown(" ".join(words), chunk_chars=200, chunk_overlap=0)
    assert len(chunks) > 1
    for chunk in chunks:
        for word in chunk.text.split():
            assert word == "parola", f"a word was cut in half: {word!r}"


def test_a_run_with_no_whitespace_at_all_is_still_split() -> None:
    # A base64 blob or a language without spaces: cutting mid-token is the only
    # option left, and producing nothing would lose the document.
    chunks = chunk_markdown("x" * 1000, chunk_chars=200, chunk_overlap=0)
    assert len(chunks) > 1
    assert "".join(chunk.text for chunk in chunks) == "x" * 1000


def test_overlap_repeats_text_across_the_boundary() -> None:
    sentence = "Alfa beta gamma delta epsilon zeta eta theta iota kappa."
    text = " ".join([sentence] * 40)
    with_overlap = chunk_markdown(text, chunk_chars=300, chunk_overlap=90)
    without = chunk_markdown(text, chunk_chars=300, chunk_overlap=0)
    # Overlap makes the same text take more chunks, because each one repeats
    # the tail of the last.
    assert len(with_overlap) > len(without)


def test_overlap_is_clamped_to_a_third_of_the_chunk() -> None:
    # Asking for more overlap than chunk size would otherwise loop or produce
    # chunks that are almost entirely duplicate.
    text = " ".join(["Frase breve numero uno."] * 50)
    chunks = chunk_markdown(text, chunk_chars=300, chunk_overlap=100_000)
    assert chunks
    # Termination is the property under test; a runaway would not get here.
    assert len(chunks) < 200


def test_a_tiny_remainder_is_folded_into_the_previous_chunk() -> None:
    sentence = "Una frase di lunghezza media che occupa spazio sufficiente."
    text = " ".join([sentence] * 12) + " Breve."
    chunks = chunk_markdown(text, chunk_chars=300, chunk_overlap=0)
    assert all(
        len(chunk.text.strip()) >= MIN_CHUNK_CHARS or len(chunks) == 1 for chunk in chunks
    ), [len(chunk.text) for chunk in chunks]


def test_ordinals_are_contiguous_from_zero() -> None:
    text = "# A\n\n" + " ".join(["Frase piena di parole."] * 80) + "\n\n# B\n\nAltro testo qui."
    chunks = chunk_markdown(text, chunk_chars=250)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_no_chunk_body_greatly_exceeds_the_limit() -> None:
    text = " ".join(["Parola"] * 400)
    limit = 250
    chunks = chunk_markdown(text, chunk_chars=limit, chunk_overlap=0)
    for chunk in chunks:
        # The heading prefix is added after the limit is applied, and there is
        # no heading here, so the bound should hold exactly.
        assert len(chunk.text) <= limit + MIN_CHUNK_CHARS


def test_an_absurdly_small_chunk_size_is_refused_not_honoured() -> None:
    # Silently accepting 5 characters would index fragments that embed to noise.
    with pytest.raises(ValueError, match="at least"):
        chunk_markdown("some text", chunk_chars=5)


def test_a_decimal_point_is_not_a_sentence_boundary() -> None:
    text = "Il valore e 3.14159 e non cambia. " * 20
    chunks = chunk_markdown(text, chunk_chars=200, chunk_overlap=0)
    for chunk in chunks:
        assert "3.14159" in chunk.text or "3.14159" not in text
        # Nothing should end on the digits of a decimal.
        assert not chunk.text.strip().endswith("3.")
