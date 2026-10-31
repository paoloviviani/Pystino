#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27"]
# ///
"""`/v1/audio/transcriptions` against a running gateway and a real provider.

The unit suite proves the metering against a fake upstream. What only a real
provider can show:

* that the provider accepts the multipart upload the gateway forwards, for the
  response formats it supports;
* **which duration it reports**: whether `verbose_json` carries `duration`
  (source `upstream`), or only the file header can say (source `measured`);
* that the price the catalogue imported, per minute, produces the cost the
  provider invoices.

It posts a few seconds of generated silence (a WAV, so the duration is known
exactly), once per format, and prints each response and the spend the gateway
recorded around it. Silence usually transcribes to an empty string or a stock
phrase, which is fine: the point is the metering, not the words.

    export GATEWAY_URL=https://gateway.example      # default http://127.0.0.1:8000
    export GATEWAY_KEY=...                           # an API key or a bearer token
    export TRANSCRIPTION_MODEL=whisper-large-v3      # a transcription model it may use
    # Optional: print the ledger's own aggregate for the model (audio_seconds,
    # cost) from the admin report. Needs an administrator's session cookie.
    # export PYSTINO_SESSION='<cookie value>'
    ./scripts/test_transcription_live.py [seconds] [formats]

`seconds` defaults to 4; `formats` is a comma list, default
`json,verbose_json,text`. Never run by CI.
"""

from __future__ import annotations

import io
import os
import sys
import wave

import httpx

GATEWAY = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000").rstrip("/")


def silence_wav(seconds: float, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


def spend(client: httpx.Client, headers: dict[str, str]) -> object:
    response = client.get(f"{GATEWAY}/v1/pystino/usage", headers=headers)
    if response.status_code != 200:
        return f"HTTP {response.status_code}"
    return response.json().get("usage")


def main() -> int:
    key = os.environ.get("GATEWAY_KEY")
    model = os.environ.get("TRANSCRIPTION_MODEL")
    if not key or not model:
        print("set GATEWAY_KEY and TRANSCRIPTION_MODEL (and optionally GATEWAY_URL)")
        return 2
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    formats = (sys.argv[2] if len(sys.argv) > 2 else "json,verbose_json,text").split(",")
    headers = {"authorization": f"Bearer {key}"}
    audio = silence_wav(seconds)
    failures = 0

    with httpx.Client(timeout=300) as client:
        print(f"gateway {GATEWAY}, model {model}, {seconds}s of silence ({len(audio)} bytes)\n")
        for fmt in formats:
            before = spend(client, headers)
            response = client.post(
                f"{GATEWAY}/v1/audio/transcriptions",
                headers=headers,
                data={"model": model, "response_format": fmt},
                files={"file": ("silence.wav", audio, "audio/wav")},
            )
            after = spend(client, headers)
            content_type = response.headers.get("content-type")
            print(f"[{fmt}] HTTP {response.status_code}  content-type {content_type}")
            print(f"  body:   {response.text[:400]!r}")
            if fmt == "verbose_json" and response.status_code == 200:
                try:
                    reported = response.json().get("duration")
                except ValueError:
                    reported = None
                print(f"  provider-reported duration: {reported!r}  (we sent {seconds}s)")
            print(f"  spend before: {before}")
            print(f"  spend after:  {after}\n")
            if response.status_code != 200:
                failures += 1

        # Too big: must be refused by the gateway, never reach the provider.
        limit = int(os.environ.get("EXPECT_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
        big = client.post(
            f"{GATEWAY}/v1/audio/transcriptions",
            headers=headers,
            data={"model": model},
            files={"file": ("big.wav", b"\0" * (limit + 1024 * 1024), "audio/wav")},
        )
        ok = big.status_code == 413
        print(f"[over the cap] HTTP {big.status_code} (expected 413)  {'ok' if ok else 'FAIL'}")
        failures += 0 if ok else 1

        if session := os.environ.get("PYSTINO_SESSION"):
            report = client.get(
                f"{GATEWAY}/api/admin/reports/usage",
                params={"group_by": "model", "model": model},
                cookies={"gw_session": session},
            )
            print(f"\nadmin report for {model}: HTTP {report.status_code}")
            if report.status_code == 200:
                for row in report.json().get("rows", []):
                    print(
                        f"  requests={row['requests']} audio_seconds={row.get('audio_seconds')} "
                        f"cost={row['cost']} estimated={row['estimated_requests']}"
                    )

    print("\nCheck by hand: audio_seconds x per-minute price / 60 against the provider's invoice,")
    print(
        "and whether verbose_json reported a duration (source 'upstream') or the header was used."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
