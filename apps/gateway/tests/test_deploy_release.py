"""Digest pinning of the release manifest (ADR 0087)."""

from __future__ import annotations

from pathlib import Path

from gateway.deploy import envfile, release

D = "sha256:" + "a" * 64


def test_every_image_reference_is_pinned_and_the_tag_kept(tmp_path: Path) -> None:
    path = tmp_path / "release.env"
    path.write_text(
        "PYSTINO_REGISTRY=ghcr.io/x\nPYSTINO_VERSION=1.0.0\nCEREA_REGISTRY=ghcr.io/x\n"
        "CEREA_VERSION=1.3.2\nPOSTGRES_IMAGE=pgvector/pgvector:pg18\n"
    )
    seen: list[str] = []

    def resolver(ref: str) -> str:
        seen.append(ref)
        return D

    changes = release.pin_file(path, resolver)
    assert changes == {
        "CEREA_IMAGE": f"ghcr.io/x/cerea:1.3.2@{D}",
        "POSTGRES_IMAGE": f"pgvector/pgvector:pg18@{D}",
    }
    assert seen == ["ghcr.io/x/cerea:1.3.2", "pgvector/pgvector:pg18"]
    assert release.unpinned(envfile.read(path)) == []
    # Re-pinning resolves the tag again (it may have moved) and is a no-op if not.
    assert release.pin_file(path, resolver) == {}


def test_check_names_what_is_unpinned() -> None:
    assert release.unpinned({"POSTGRES_IMAGE": "pg:18", "VALKEY_IMAGE": f"v:8@{D}"}) == [
        "POSTGRES_IMAGE"
    ]


def test_release_pin_cli_points_at_deploy_release_env_by_default() -> None:
    # cli.py no longer has stackfiles.stack_dir() to ask; it computes the
    # repository root itself. This is the tripwire for that arithmetic.
    from gateway.deploy.cli import ROOT

    assert (ROOT / "deploy" / "release.env").is_file()
