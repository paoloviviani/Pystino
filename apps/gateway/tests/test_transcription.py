"""``POST /v1/audio/transcriptions``: one surface, metered by the minute.

What is pinned is what would otherwise be found on an invoice: that the billed
seconds come from the best source there is (the provider's own figure, then the
file's header, then a size estimate that says it is one), that the quota
reservation is the real duration whenever it can be read, that the upload
reaches the provider as the caller sent it, and that the size cap and the
redaction switch refuse before anything is spent.
"""

from __future__ import annotations

import io
import wave
from decimal import Decimal
from typing import Any

import httpx
import pytest
from conftest import FakeUpstream, Seeded, make_token
from conftest import bearer_auth as bearer
from fastapi import FastAPI
from gateway import audio
from gateway.accounting import TokenCounts, compute_cost
from gateway.config import (
    EntityMode,
    RedactionPolicy,
    RedactionSettings,
    Settings,
)
from gateway.models import (
    ApiSurface,
    GroupModelAccess,
    LimitMetric,
    LimitRule,
    LimitScope,
    ModelDef,
    ModelKind,
    ModelPrice,
    UsageRecord,
    UsageSource,
)
from gateway.pricing import CataloguePrice, import_prices, kinds_by_id, parse_catalogue
from gateway.routers import _metered as metered
from gateway.routers.transcription import redaction_in_force
from joserfc.jwk import RSAKey
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_bill_to import BILL_TO, _second_group


def wav_bytes(seconds: float = 3.0, rate: int = 8000) -> bytes:
    """Silence of an exact length: the duration is knowable from the header."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


async def add_model(
    session: AsyncSession,
    seeded: Seeded,
    *,
    name: str = "whisper",
    per_minute: str | None = "0.60",
    upstream: str = "whisper-large-v3",
    grant: bool = True,
    with_price: bool = True,
) -> ModelDef:
    model = ModelDef(
        name=name,
        upstream_model=upstream,
        provider_id=seeded.provider.id,
        kind=ModelKind.TRANSCRIPTION,
    )
    session.add(model)
    await session.flush()
    if with_price:
        session.add(
            ModelPrice(
                model_id=model.id,
                # Zero token rates prove the charge is not coming from them.
                input_per_mtok=Decimal(0),
                output_per_mtok=Decimal(0),
                per_minute=Decimal(per_minute) if per_minute else None,
                currency="EUR",
            )
        )
    if grant:
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
    await session.commit()
    return model


async def post_audio(
    client: httpx.AsyncClient,
    seeded: Seeded,
    model: str,
    *,
    data: bytes | None = None,
    path: str = "/v1/audio/transcriptions",
    headers: dict[str, str] | None = None,
    filename: str = "clip.wav",
    content_type: str = "audio/wav",
    **fields: Any,
) -> httpx.Response:
    payload = wav_bytes() if data is None else data
    return await client.post(
        path,
        data={"model": model, **fields},
        files={"file": (filename, payload, content_type)},
        headers=headers or seeded.auth,
    )


async def latest_record(session: AsyncSession) -> UsageRecord:
    return (
        (
            await session.execute(
                select(UsageRecord).order_by(UsageRecord.created_at.desc()).limit(1)
            )
        )
        .scalars()
        .one()
    )


class TestMetering:
    async def test_the_providers_reported_duration_is_billed_per_minute(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """12.5 s at 0.06 a minute is 0.0125: seconds, not rounded-up minutes.
        The provider's figure wins over the file's own 3 s."""
        model = await add_model(session, seeded, per_minute="0.06")
        fake_upstream.set_json({"text": "hello", "duration": 12.5})

        response = await post_audio(client, seeded, model.name, response_format="verbose_json")
        assert response.status_code == 200, response.text
        assert response.json()["text"] == "hello"

        record = await latest_record(session)
        assert record.api_surface is ApiSurface.TRANSCRIPTION
        assert record.audio_seconds == Decimal("12.5")
        assert record.audio_seconds_source == "upstream"
        assert record.cost == Decimal("0.0125")
        assert record.usage_source is UsageSource.UPSTREAM_EXACT
        # No tokens on this surface: reporting some would bill a request twice.
        assert record.total_tokens == 0

    async def test_usage_seconds_in_a_json_reply_count_as_reported(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_model(session, seeded, per_minute="0.60")
        fake_upstream.set_json({"text": "hi", "usage": {"type": "duration", "seconds": 30}})
        assert (await post_audio(client, seeded, model.name)).status_code == 200
        record = await latest_record(session)
        assert record.audio_seconds == Decimal("30")
        assert record.audio_seconds_source == "upstream"
        assert record.cost == Decimal("0.30")

    async def test_a_text_reply_is_billed_on_the_files_own_length(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        """`text`, `srt` and `vtt` carry no duration, so the header supplies it."""
        model = await add_model(session, seeded, per_minute="0.60")
        fake_upstream.set_raw(b"hello world", status=200, content_type="text/plain")

        response = await post_audio(client, seeded, model.name, response_format="text")
        assert response.status_code == 200
        assert response.text == "hello world"
        assert response.headers["content-type"].startswith("text/plain")
        record = await latest_record(session)
        assert record.audio_seconds == Decimal("3")
        assert record.audio_seconds_source == "measured"
        assert record.cost == Decimal("0.03")
        assert record.usage_source is UsageSource.ESTIMATED

    async def test_an_unreadable_file_is_estimated_from_its_size_and_says_so(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_model(session, seeded, per_minute="0.60")
        fake_upstream.set_raw(
            b"1\n00:00:00,000 --> 00:00:01,000\nhi\n", status=200, content_type="text/plain"
        )

        response = await post_audio(
            client,
            seeded,
            model.name,
            data=b"\x01" * 8000,
            filename="clip.bin",
            content_type="application/octet-stream",
            response_format="srt",
        )
        assert response.status_code == 200
        record = await latest_record(session)
        # 8000 bytes at the 4000 B/s estimate.
        assert record.audio_seconds == Decimal("2")
        assert record.audio_seconds_source == "estimated"
        assert record.usage_source is UsageSource.ESTIMATED
        assert record.cost == Decimal("0.02")

    async def test_an_unpriced_model_records_zero_cost_but_still_the_seconds(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_model(session, seeded, name="unpriced", with_price=False)
        fake_upstream.set_json({"text": "hi", "duration": 60})
        assert (await post_audio(client, seeded, model.name)).status_code == 200
        record = await latest_record(session)
        assert record.cost == Decimal(0)
        assert record.audio_seconds == Decimal("60")

    async def test_a_refused_upstream_bills_nothing(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_model(session, seeded)
        fake_upstream.set_json({"error": {"message": "bad audio"}}, status=400)
        response = await post_audio(client, seeded, model.name)
        assert response.status_code == 400
        record = await latest_record(session)
        assert record.cost == Decimal(0)
        assert record.audio_seconds is None


class TestForwarding:
    async def test_the_upload_reaches_the_provider_as_sent(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_model(session, seeded, upstream="whisper-large-v3")
        fake_upstream.set_json({"text": "ok", "duration": 3})
        data = wav_bytes()

        response = await client.post(
            "/v1/audio/transcriptions",
            data={
                "model": model.name,
                "language": "it",
                "prompt": "nomi propri",
                "response_format": "verbose_json",
                "temperature": "0.2",
                "timestamp_granularities[]": ["word", "segment"],
            },
            files={"file": ("riunione.wav", data, "audio/wav")},
            headers=seeded.auth,
        )
        assert response.status_code == 200, response.text

        sent = fake_upstream.last_body
        # The upstream's own model name, not ours.
        assert sent["model"] == "whisper-large-v3"
        assert sent["language"] == "it"
        assert sent["prompt"] == "nomi propri"
        assert sent["response_format"] == "verbose_json"
        assert sent["temperature"] == "0.2"
        assert sent["timestamp_granularities[]"] == ["word", "segment"]
        assert fake_upstream.files == [("riunione.wav", data, "audio/wav")]
        assert fake_upstream.urls[-1].endswith("/audio/transcriptions")
        # The upstream's key, never the caller's.
        assert fake_upstream.headers[-1]["authorization"] == "Bearer upstream-key"

    @pytest.mark.parametrize("fmt", ["json", "text", "verbose_json", "srt", "vtt"])
    async def test_every_format_is_forwarded_and_returned(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        fmt: str,
    ) -> None:
        model = await add_model(session, seeded, name=f"fmt-{fmt}")
        if fmt in ("json", "verbose_json"):
            fake_upstream.set_json({"text": "hi", "duration": 3})
        else:
            fake_upstream.set_raw(b"WEBVTT\n\nhi", status=200, content_type="text/vtt")
        response = await post_audio(client, seeded, model.name, response_format=fmt)
        assert response.status_code == 200
        assert fake_upstream.last_body["response_format"] == fmt
        if fmt in ("json", "verbose_json"):
            assert response.json()["text"] == "hi"
        else:
            assert response.text == "WEBVTT\n\nhi"
            assert response.headers["content-type"].startswith("text/vtt")

    async def test_json_is_the_default_format(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_model(session, seeded)
        fake_upstream.set_json({"text": "hi"})
        await post_audio(client, seeded, model.name)
        assert fake_upstream.last_body["response_format"] == "json"

    async def test_an_unknown_format_is_refused_before_anything_is_spent(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_model(session, seeded)
        response = await post_audio(client, seeded, model.name, response_format="docx")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_response_format"
        assert (await session.execute(select(UsageRecord))).first() is None

    async def test_translations_use_their_own_path_and_send_no_language(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_model(session, seeded)
        fake_upstream.set_json({"text": "hello", "duration": 6})
        response = await post_audio(
            client, seeded, model.name, path="/v1/audio/translations", language="it"
        )
        assert response.status_code == 200
        assert fake_upstream.urls[-1].endswith("/audio/translations")
        assert "language" not in fake_upstream.last_body
        record = await latest_record(session)
        assert record.api_surface is ApiSurface.TRANSCRIPTION
        assert record.cost == Decimal("0.06")

    async def test_missing_file_and_model_are_400s(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_model(session, seeded)
        no_file = await client.post(
            "/v1/audio/transcriptions", data={"model": model.name}, headers=seeded.auth
        )
        assert no_file.status_code == 400
        no_model = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", wav_bytes(), "audio/wav")},
            headers=seeded.auth,
        )
        assert no_model.status_code == 400


class TestSizeCap:
    async def test_a_file_over_the_cap_is_413_and_never_reaches_the_provider(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        settings: Settings,
    ) -> None:
        model = await add_model(session, seeded)
        settings.transcription.max_upload_bytes = 1000
        fake_upstream.set_json({"text": "never"})

        response = await post_audio(client, seeded, model.name, data=b"\x00" * 200_000)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"
        assert fake_upstream.bodies == []
        assert (await session.execute(select(UsageRecord))).first() is None

    async def test_the_cap_is_checked_on_the_bytes_when_the_length_is_not_declared(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        settings: Settings,
    ) -> None:
        """A file just over the cap but inside the framing allowance passes the
        header check, so the read itself must refuse it."""
        model = await add_model(session, seeded)
        settings.transcription.max_upload_bytes = 1000
        fake_upstream.set_json({"text": "never"})

        response = await post_audio(client, seeded, model.name, data=b"\x00" * 1500)
        assert response.status_code == 413
        assert fake_upstream.bodies == []

    async def test_a_file_at_the_cap_is_served(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        settings: Settings,
    ) -> None:
        model = await add_model(session, seeded)
        data = wav_bytes(0.05)
        settings.transcription.max_upload_bytes = len(data)
        fake_upstream.set_json({"text": "ok"})
        assert (await post_audio(client, seeded, model.name, data=data)).status_code == 200


class TestRefusals:
    async def test_a_chat_model_is_refused_here(
        self, client: httpx.AsyncClient, seeded: Seeded
    ) -> None:
        response = await post_audio(client, seeded, seeded.model.name)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "wrong_model_kind"
        assert "/v1/chat/completions" in response.text

    async def test_a_transcription_model_is_refused_on_the_chat_route(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_model(session, seeded)
        response = await client.post(
            "/v1/chat/completions",
            json={"model": model.name, "messages": [{"role": "user", "content": "hi"}]},
            headers=seeded.auth,
        )
        assert response.status_code == 400
        assert "/v1/audio/transcriptions" in response.text

    async def test_a_model_nobody_granted_is_invisible(
        self, client: httpx.AsyncClient, seeded: Seeded, session: AsyncSession
    ) -> None:
        model = await add_model(session, seeded, name="ungranted", grant=False)
        response = await post_audio(client, seeded, model.name)
        assert response.status_code == 404

    async def test_an_unknown_model_is_404(self, client: httpx.AsyncClient, seeded: Seeded) -> None:
        assert (await post_audio(client, seeded, "no-such-model")).status_code == 404


class TestQuota:
    async def test_the_reservation_is_the_files_real_duration(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        monkeypatch: Any,
    ) -> None:
        model = await add_model(session, seeded)
        fake_upstream.set_json({"text": "ok", "duration": 3})
        captured: list[Decimal] = []
        original = metered.begin

        async def spy(**kwargs: Any) -> Any:
            captured.append(kwargs["worst_case"].audio_seconds)
            return await original(**kwargs)

        monkeypatch.setattr(metered, "begin", spy)
        assert (await post_audio(client, seeded, model.name, data=wav_bytes(3))).status_code == 200
        assert captured == [Decimal("3")]

    async def test_an_unreadable_file_reserves_a_pessimistic_bound_from_its_size(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        monkeypatch: Any,
    ) -> None:
        model = await add_model(session, seeded)
        fake_upstream.set_raw(b"hi", status=200, content_type="text/plain")
        captured: list[Decimal] = []
        original = metered.begin

        async def spy(**kwargs: Any) -> Any:
            captured.append(kwargs["worst_case"].audio_seconds)
            return await original(**kwargs)

        monkeypatch.setattr(metered, "begin", spy)
        await post_audio(
            client,
            seeded,
            model.name,
            data=b"\x01" * 8000,
            response_format="text",
            filename="x.bin",
            content_type="application/octet-stream",
        )
        # 8000 B at 1000 B/s: longer than the 2 s it is billed at.
        assert captured == [Decimal("8")]

    async def test_settlement_charges_what_the_provider_reported_not_the_reservation(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
        fake_upstream: FakeUpstream,
    ) -> None:
        """Reserved on the 3 s header (0.03, under the 0.05 ceiling), settled on
        the provider's 30 s (0.30): the next request is refused. Only a real
        settle makes the second one fail."""
        model = await add_model(session, seeded, per_minute="0.60")
        async with session_factory() as rules:
            rules.add(
                LimitRule(
                    name="five cents",
                    scope=LimitScope.GROUP,
                    scope_id=seeded.group.id,
                    metric=LimitMetric.COST,
                    window_seconds=3600,
                    limit_value=Decimal("0.05"),
                )
            )
            await rules.commit()
        fake_upstream.set_json({"text": "long", "duration": 30})

        assert (await post_audio(client, seeded, model.name)).status_code == 200
        second = await post_audio(client, seeded, model.name)
        assert second.status_code == 429


class TestBillTo:
    @pytest.mark.asyncio
    async def test_the_header_bills_the_named_group(
        self,
        bearer_app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        signing_key: RSAKey,
        fake_upstream: FakeUpstream,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        other = await _second_group(session_factory, seeded)
        model = await add_model(session, seeded)
        async with session_factory() as db:
            db.add(GroupModelAccess(group_id=other, model_id=model.id))
            await db.commit()
        fake_upstream.set_json({"text": "hi", "duration": 6})

        response = await post_audio(
            client,
            seeded,
            model.name,
            headers={**bearer(make_token(signing_key)), BILL_TO: "finance"},
        )
        assert response.status_code == 200, response.text
        record = await latest_record(session)
        assert record.group_id == other
        assert record.cost == Decimal("0.06")


def put_redaction_in_force(app: Any, settings: Settings) -> None:
    """The redactor's own policy is the effective one (no resolver), anonymising."""
    app.state.redaction = None
    settings.redaction = RedactionSettings(
        engine="http",
        endpoint="http://detector:8080",
        placeholder_key=SecretStr("k"),
        policy=RedactionPolicy(default_mode=EntityMode.ANONYMISE_RESTORE),
    )


class TestRedactionSwitch:
    """Audio cannot be screened, so the one control is whether to allow it at
    all for a caller under an active rule (docs/redaction.md)."""

    async def test_allowed_by_default_even_under_a_rule(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        settings: Settings,
    ) -> None:
        model = await add_model(session, seeded)
        put_redaction_in_force(app, settings)
        fake_upstream.set_json({"text": "hi", "duration": 3})
        assert settings.transcription.allowed_under_redaction is True
        assert (await post_audio(client, seeded, model.name)).status_code == 200

    async def test_refused_under_a_rule_when_the_operator_says_so(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        settings: Settings,
    ) -> None:
        model = await add_model(session, seeded)
        put_redaction_in_force(app, settings)
        settings.transcription.allowed_under_redaction = False
        fake_upstream.set_json({"text": "never"})

        response = await post_audio(client, seeded, model.name)
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "transcription_not_allowed_under_redaction"
        # Before the reservation: no row, no upstream call.
        assert fake_upstream.bodies == []
        assert (await session.execute(select(UsageRecord))).first() is None

    async def test_the_switch_does_nothing_without_a_rule(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        settings: Settings,
    ) -> None:
        model = await add_model(session, seeded)
        settings.transcription.allowed_under_redaction = False
        fake_upstream.set_json({"text": "hi", "duration": 3})
        assert (await post_audio(client, seeded, model.name)).status_code == 200

    def test_what_counts_as_a_rule_in_force(self) -> None:
        def settings_with(policy: RedactionPolicy, engine: str = "http") -> Settings:
            s = Settings()
            s.redaction = RedactionSettings(
                engine=engine,
                endpoint="http://d:1",
                placeholder_key=SecretStr("k"),
                policy=policy,
            )
            return s

        off = RedactionPolicy()
        assert not redaction_in_force(None, settings_with(off))
        assert redaction_in_force(
            None, settings_with(RedactionPolicy(default_mode=EntityMode.REDACT))
        )
        # Detected and left alone is not a rule that changes anything.
        listed_off = RedactionPolicy.model_validate({"entities": {"URL": {"mode": "off"}}})
        assert not redaction_in_force(None, settings_with(listed_off))
        blocking = RedactionPolicy.model_validate({"entities": {"IBAN_CODE": {"mode": "block"}}})
        assert redaction_in_force(None, settings_with(blocking))
        # A deployment on the noop engine redacts nothing whatever it lists.
        assert not redaction_in_force(
            None, settings_with(RedactionPolicy(default_mode=EntityMode.REDACT), engine="noop")
        )


class TestAudioDuration:
    def test_a_wav_header_gives_the_exact_length(self) -> None:
        assert audio.measure(wav_bytes(2.5)).seconds == Decimal("2.5")

    def test_garbage_has_no_measurable_length(self) -> None:
        assert audio.measure(b"not audio at all") is None

    def test_the_reported_duration_is_read_from_either_place(self) -> None:
        assert audio.reported({"duration": 12.5}) == Decimal("12.5")
        assert audio.reported({"usage": {"type": "duration", "seconds": 7}}) == Decimal("7")
        # Token usage (gpt-4o-transcribe) is not a duration.
        assert audio.reported({"usage": {"type": "tokens", "total_tokens": 99}}) is None
        assert audio.reported({"duration": 0}) is None
        assert audio.reported({"duration": True}) is None
        assert audio.reported(None) is None

    def test_the_estimate_is_labelled_by_the_route_not_here(self) -> None:
        assert audio.estimate(4000) == Decimal("1")
        assert audio.estimate(1000, bytes_per_second=audio.RESERVE_BYTES_PER_SECOND) == Decimal("1")


class TestPerMinuteCost:
    def _price(self, per_minute: str | None) -> ModelPrice:
        return ModelPrice(
            input_per_mtok=Decimal(0),
            output_per_mtok=Decimal(0),
            per_minute=Decimal(per_minute) if per_minute else None,
            currency="EUR",
        )

    def test_seconds_over_sixty_times_the_rate(self) -> None:
        cost = compute_cost(TokenCounts(audio_seconds=Decimal("90")), self._price("0.04"))
        assert cost.audio_cost == Decimal("0.06")
        assert cost.total == Decimal("0.06")

    def test_no_rate_no_charge(self) -> None:
        assert compute_cost(TokenCounts(audio_seconds=Decimal(90)), self._price(None)).total == 0

    def test_scaling_keeps_the_component(self) -> None:
        cost = compute_cost(TokenCounts(audio_seconds=Decimal("60")), self._price("1"))
        scaled = cost.scaled(Decimal("2"), "USD")
        assert scaled.audio_cost == Decimal("2")
        assert scaled.total == Decimal("2")


def _entry(**over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "whisper-large-v3",
        "pricing": {"currency": "EUR", "per_minute": 0.006},
        "input_modalities": ["audio"],
        "output_modalities": ["text"],
    }
    entry.update(over)
    return entry


class TestCatalogueMapping:
    """Cortecs reports `text` out for a speech model exactly as for a chat one,
    so the kind comes from audio-only input (or a tag), never from audio alone."""

    def test_audio_in_text_out_is_transcription(self) -> None:
        prices, unparsable = parse_catalogue({"data": [_entry()]})
        assert unparsable == []
        assert prices[0].kind is ModelKind.TRANSCRIPTION
        assert prices[0].per_minute == Decimal("0.006")

    def test_a_tag_is_enough(self) -> None:
        prices, _ = parse_catalogue(
            {"data": [_entry(input_modalities=["file"], tags=["Transcription"])]}
        )
        assert prices[0].kind is ModelKind.TRANSCRIPTION

    def test_a_chat_model_that_also_hears_audio_stays_chat(self) -> None:
        chat = _entry(
            id="omni",
            pricing={"currency": "EUR", "input_token": 1.0, "output_token": 2.0},
            input_modalities=["text", "audio", "image"],
        )
        prices, _ = parse_catalogue({"data": [chat]})
        assert prices[0].kind is ModelKind.CHAT
        assert prices[0].per_minute is None

    def test_an_audio_model_with_a_rate_in_no_known_unit_is_reported_not_guessed(self) -> None:
        entry = _entry(pricing={"currency": "EUR", "transcription_cost": 0.36})
        prices, unparsable = parse_catalogue({"data": [entry]})
        assert prices == []
        assert unparsable == ["whisper-large-v3"]
        # Discovery still labels it, so it can be adopted with a typed rate.
        assert kinds_by_id({"data": [entry]}) == {"whisper-large-v3": ModelKind.TRANSCRIPTION}

    async def test_an_import_stores_the_minute_rate_and_notices_a_change(
        self, session: AsyncSession, seeded: Seeded
    ) -> None:
        model = await add_model(session, seeded, upstream="whisper-large-v3", per_minute="0.006")
        same = CataloguePrice(
            "whisper-large-v3",
            Decimal(0),
            Decimal(0),
            "EUR",
            per_minute=Decimal("0.006"),
            kind=ModelKind.TRANSCRIPTION,
        )
        report = await import_prices(session, [same], billing_currency="EUR", dry_run=False)
        assert report.unchanged == [model.name]

        changed = CataloguePrice(
            "whisper-large-v3",
            Decimal(0),
            Decimal(0),
            "EUR",
            per_minute=Decimal("0.01"),
            kind=ModelKind.TRANSCRIPTION,
        )
        report = await import_prices(session, [changed], billing_currency="EUR", dry_run=False)
        assert report.created == [model.name]
        rows = (
            (await session.execute(select(ModelPrice).where(ModelPrice.model_id == model.id)))
            .scalars()
            .all()
        )
        newest = max(rows, key=lambda price: price.effective_from)
        assert newest.per_minute == Decimal("0.01")

    async def test_an_import_stores_the_page_rate_too(
        self, session: AsyncSession, seeded: Seeded
    ) -> None:
        """Found while adding the minute rate: the importer built the price row
        without `per_page`, so an OCR model's whole charge imported as nothing."""
        ocr = ModelDef(
            name="reader",
            upstream_model="ocr-1",
            provider_id=seeded.provider.id,
            kind=ModelKind.OCR,
        )
        session.add(ocr)
        await session.commit()
        await import_prices(
            session,
            [
                CataloguePrice(
                    "ocr-1",
                    Decimal(0),
                    Decimal(0),
                    "EUR",
                    per_page=Decimal("0.003"),
                    kind=ModelKind.OCR,
                )
            ],
            billing_currency="EUR",
            dry_run=False,
        )
        row = (
            await session.execute(select(ModelPrice).where(ModelPrice.model_id == ocr.id))
        ).scalar_one()
        assert row.per_page == Decimal("0.003")


class TestManualPriceAndAdmin:
    async def test_a_transcription_model_needs_a_per_minute_rate_by_hand(self) -> None:
        from gateway.config import Settings
        from gateway.routers.admin import _manual_catalogue_price
        from gateway.schemas import ManualImportPrice

        settings = Settings()
        price, reason = await _manual_catalogue_price(
            ManualImportPrice(input_per_mtok=Decimal(1), output_per_mtok=Decimal(1)),
            ModelKind.TRANSCRIPTION,
            settings,
            None,
        )
        assert price is None
        assert reason is not None and "per minute" in reason

        price, reason = await _manual_catalogue_price(
            ManualImportPrice(per_minute=Decimal("0.006")),
            ModelKind.TRANSCRIPTION,
            settings,
            None,
        )
        assert reason is None and price is not None
        assert price.per_minute == Decimal("0.006")
        assert price.input_per_mtok == 0

    async def test_the_price_form_stores_and_returns_the_minute_rate(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_admin import as_user, make_admin

        model = await add_model(session, seeded, name="priced-by-hand", per_minute=None)
        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            f"/api/admin/models/{model.id}/prices",
            json={"input_per_mtok": "0", "output_per_mtok": "0", "per_minute": "0.006"},
        )
        assert response.status_code == 201, response.text
        assert Decimal(response.json()["per_minute"]) == Decimal("0.006")

    async def test_a_transcription_model_can_be_created_through_the_api(
        self,
        app: FastAPI,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        from test_admin import as_user, make_admin

        as_user(app, await make_admin(session_factory, seeded))
        response = await client.post(
            "/api/admin/models",
            json={
                "name": "stt",
                "upstream_model": "whisper-large-v3",
                "provider_id": str(seeded.provider.id),
                "kind": "transcription",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["kind"] == "transcription"


CRAFTED_MP3 = b"\xff\xfb" * (1_900_000 // 2)


class TestCraftedInput:
    """tinytag's MP3 frame scan is slow on repeating sync bytes (2 MB: ~0.9 s,
    25 MB: ~15 s). It must not hold the event loop, nor run on big files."""

    def test_a_large_crafted_file_is_not_handed_to_tinytag(self) -> None:
        import time

        started = time.perf_counter()
        assert audio.measure(b"\xff\xfb" * 4_000_000) is None  # 8 MB
        assert time.perf_counter() - started < 0.5

    async def test_the_event_loop_stays_responsive_while_a_crafted_file_is_measured(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        import asyncio
        import time

        model = await add_model(session, seeded)
        fake_upstream.set_raw(b"hi", status=200, content_type="text/plain")
        gaps: list[float] = []
        stop = False

        async def ticker() -> None:
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        task = asyncio.create_task(ticker())
        started = time.perf_counter()
        response = await post_audio(
            client,
            seeded,
            model.name,
            data=CRAFTED_MP3,
            filename="x.mp3",
            content_type="audio/mpeg",
            response_format="text",
        )
        elapsed = time.perf_counter() - started
        stop = True
        await task
        assert response.status_code == 200
        assert elapsed < 10
        # A synchronous scan would show up as one gap as long as the scan.
        assert max(gaps) < 0.5, max(gaps)


class TestChunkedUploadIsBounded:
    async def test_an_oversized_chunked_body_is_cut_off_at_the_cap(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        settings: Settings,
    ) -> None:
        """No Content-Length, so only counting the bytes can stop it: the
        generator is abandoned long before it has sent its 5 MB."""
        model = await add_model(session, seeded)
        settings.transcription.max_upload_bytes = 1000
        boundary = "XBOUNDARYX"
        sent = 0

        async def body() -> Any:
            nonlocal sent
            head = (
                f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n'
                f"{model.name}\r\n--{boundary}\r\n"
                'Content-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                "Content-Type: audio/wav\r\n\r\n"
            ).encode()
            sent += len(head)
            yield head
            for _ in range(320):
                chunk = b"\x00" * 16384
                sent += len(chunk)
                yield chunk
            yield f"\r\n--{boundary}--\r\n".encode()

        response = await client.post(
            "/v1/audio/transcriptions",
            content=body(),
            headers={
                **seeded.auth,
                "content-type": f"multipart/form-data; boundary={boundary}",
            },
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"
        # 320 x 16 KB = 5 MB offered; cap + 64 KB framing + a chunk or two read.
        assert sent < 300_000, sent
        assert fake_upstream.bodies == []


class TestHeaderIsNotTrusted:
    async def test_a_header_claiming_one_second_over_megabytes_is_floored_and_labelled(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
        monkeypatch: Any,
    ) -> None:
        model = await add_model(session, seeded, per_minute="0.60")
        fake_upstream.set_raw(b"hi", status=200, content_type="text/plain")
        data = wav_bytes(1.0, rate=16000) + b"\x00" * 5_000_000
        captured: list[Decimal] = []
        original = metered.begin

        async def spy(**kwargs: Any) -> Any:
            captured.append(kwargs["worst_case"].audio_seconds)
            return await original(**kwargs)

        monkeypatch.setattr(metered, "begin", spy)
        response = await post_audio(client, seeded, model.name, data=data, response_format="text")
        assert response.status_code == 200
        record = await latest_record(session)
        floor = Decimal(len(data)) / Decimal(32000)  # 16 kHz 16-bit mono
        assert record.audio_seconds == pytest.approx(floor, abs=Decimal("0.01"))
        assert record.audio_seconds > 100
        assert record.audio_seconds_source == "estimated"
        assert record.usage_source is UsageSource.ESTIMATED
        # The reservation was the floor too, not the claimed second.
        assert captured[0] == record.audio_seconds

    async def test_an_honest_wav_is_still_measured(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_model(session, seeded)
        fake_upstream.set_raw(b"hi", status=200, content_type="text/plain")
        await post_audio(client, seeded, model.name, data=wav_bytes(30), response_format="text")
        record = await latest_record(session)
        assert record.audio_seconds == Decimal("30")
        assert record.audio_seconds_source == "measured"

    async def test_only_an_upstream_duration_is_labelled_exact(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await add_model(session, seeded)
        fake_upstream.set_json({"text": "hi", "duration": 3})
        await post_audio(client, seeded, model.name, response_format="verbose_json")
        assert (await latest_record(session)).usage_source is UsageSource.UPSTREAM_EXACT


class TestTokenPricedTranscription:
    async def _priced(
        self, session: AsyncSession, seeded: Seeded, name: str, **rates: str | None
    ) -> ModelDef:
        model = ModelDef(
            name=name,
            upstream_model="gpt-4o-transcribe",
            provider_id=seeded.provider.id,
            kind=ModelKind.TRANSCRIPTION,
        )
        session.add(model)
        await session.flush()
        session.add(
            ModelPrice(
                model_id=model.id,
                input_per_mtok=Decimal(rates.get("input") or 0),
                output_per_mtok=Decimal(rates.get("output") or 0),
                per_minute=Decimal(rates["minute"]) if rates.get("minute") else None,
                currency="EUR",
            )
        )
        session.add(GroupModelAccess(group_id=seeded.group.id, model_id=model.id))
        await session.commit()
        return model

    async def test_a_model_with_token_rates_and_no_minute_rate_bills_the_tokens(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await self._priced(session, seeded, "tok", input="2", output="8")
        fake_upstream.set_json(
            {
                "text": "hi",
                "usage": {
                    "type": "tokens",
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "total_tokens": 1500,
                },
            }
        )
        assert (await post_audio(client, seeded, model.name)).status_code == 200
        record = await latest_record(session)
        # 1000 x 2/M + 500 x 8/M = 0.006, not free.
        assert record.cost == Decimal("0.006")
        assert record.prompt_tokens == 1000

    async def test_a_price_row_with_neither_rate_is_refused_not_served_free(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await self._priced(session, seeded, "nothing")
        fake_upstream.set_json({"text": "never"})
        response = await post_audio(client, seeded, model.name)
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "no_per_minute_price"
        assert fake_upstream.bodies == []
        assert (await session.execute(select(UsageRecord))).first() is None

    async def test_an_explicit_zero_minute_rate_is_free_by_decision(
        self,
        client: httpx.AsyncClient,
        seeded: Seeded,
        session: AsyncSession,
        fake_upstream: FakeUpstream,
    ) -> None:
        model = await self._priced(session, seeded, "free", minute="0")
        fake_upstream.set_json({"text": "hi", "duration": 60})
        assert (await post_audio(client, seeded, model.name)).status_code == 200
        assert (await latest_record(session)).cost == Decimal(0)


class TestTotalTimeout:
    async def test_a_provider_that_trickles_is_cut_off_at_the_total(self) -> None:
        """httpx's read timeout restarts per chunk; the deadline must not."""
        import asyncio
        import time

        from gateway.config import UpstreamSettings
        from gateway.upstream import OpenAICompatibleUpstream, UpstreamError

        async def handler(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(10)
            return httpx.Response(200, json={"text": "late"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        upstream = OpenAICompatibleUpstream(UpstreamSettings(base_url="http://provider/v1"), client)
        started = time.perf_counter()
        with pytest.raises(UpstreamError, match="longer than"):
            await upstream.transcribe(
                "/audio/transcriptions",
                [("model", "m")],
                filename="a.wav",
                content=b"x",
                content_type="audio/wav",
                timeout_seconds=0.3,
            )
        assert time.perf_counter() - started < 3
        await client.aclose()
