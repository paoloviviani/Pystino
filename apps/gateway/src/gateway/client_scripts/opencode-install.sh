#!/usr/bin/env bash
# Point opencode at a Pystino gateway, without enrolling into anything else.
#
#   curl -fsSL https://llm.example.org/opencode/install.sh | bash
#
# The gateway serves this very file at /opencode/install.sh and fills in the
# origin it was fetched from, so the one-liner needs no argument. A person
# mints a `gwk_...` API key in the console first and pastes it here: key
# minting (POST /api/me/keys) is session-cookie only, so a script holding a
# bearer can never do it.
#
# What it does: asks the gateway which chat models the key may use
# (GET /v1/models), then adds ONE block, provider.pystino, to opencode's global
# config (${XDG_CONFIG_HOME:-~/.config}/opencode/opencode.json). Everything
# else in that file is kept; the previous file is backed up first.
#
# Options (all optional; run with --help):
#   --base-url URL     gateway address (or give it as the first argument)
#   --output PATH      write a project file instead of the global one
#   --model ID         also make pystino/ID opencode's default model
#   --key-in-env       write {env:PYSTINO_API_KEY}, not the key itself
#   --install-opencode install the pinned opencode first
#   --dry-run | --print | --yes | --no-discover
#
# The key is never an argv flag (argv leaks through ps and shell history).
# It comes from PYSTINO_API_KEY or a hidden prompt on the terminal, and goes
# only into the file written (mode 600), or nowhere with --key-in-env.
#
# Needs bash and python3. Everything is wrapped in main() and called on the
# last line, so a download cut short runs nothing at all.
set -euo pipefail

# Pinned opencode, used by --install-opencode only. The source of truth is
# Cerea's agent/packaging/opencode-version (the version galopin is tested
# against); keep this in step when that file moves. The environment variable
# is an escape hatch, not a setting anyone should need.
opencode_pin="${PYSTINO_OPENCODE_VERSION:-1.18.34}"

# The gateway replaces the next line with the validated origin of the request
# that fetched this script. Empty when the file is run from a checkout.
served_origin=''

main() {
	command -v python3 >/dev/null 2>&1 || {
		echo "error: python3 is required (it reads and writes the JSON)" >&2
		exit 5
	}
	# python3 reads the program from the heredoc, so its stdin is not the
	# terminal: prompts open /dev/tty themselves, which is also what lets a
	# `curl | bash` run ask for the key at all.
	PYSTINO_SERVED_ORIGIN="$served_origin" PYSTINO_OPENCODE_PIN="$opencode_pin" \
		exec python3 - "$@" <<'PYSTINO_PY'
from __future__ import annotations

import argparse
import datetime
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

# --- the model mapping ------------------------------------------------------
# Mirrors galopin's buildOpencodeConfig (Cerea agent/store.go) field for field,
# so a person who later enrols with galopin sees the same entries.

DEFAULT_CONTEXT = 131072
DEFAULT_OUTPUT = 16384
EFFORT_LEVELS = ("low", "medium", "high")
ENV_REF = "{env:PYSTINO_API_KEY}"
PLACEHOLDER_ID = "REPLACE-WITH-MODEL-ID"


def positive_int(value, default):
    # opencode rejects a custom-provider model without BOTH limit keys, so an
    # unknown or nonsense limit becomes a default, never an omission.
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def has(values, wanted):
    if not isinstance(values, list):
        return False
    return any(isinstance(v, str) and v.lower() == wanted for v in values)


def build_models(cards):
    """opencode's models map from GET /v1/models cards.

    Only chat models: opencode is a coding agent, and an embedding or OCR
    tier in its picker is one accidental keypress from a 400. A card with no
    `kind` (an older gateway) counts as chat, as in galopin.
    """
    models = {}
    for card in cards:
        if not isinstance(card, dict):
            continue
        model_id = card.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        kind = card.get("kind")
        if kind not in (None, "", "chat"):
            continue
        name = card.get("display_name")
        entry = {"name": name if isinstance(name, str) and name else model_id}
        entry["limit"] = {
            "context": positive_int(card.get("context_window"), DEFAULT_CONTEXT),
            "output": positive_int(card.get("max_output_tokens"), DEFAULT_OUTPUT),
        }
        # A reasoning model gets opencode "variants": named option sets a
        # prompt can pick, each sent as the request's reasoning_effort.
        if has(card.get("supported_features"), "reasoning"):
            entry["variants"] = {lv: {"reasoningEffort": lv} for lv in EFFORT_LEVELS}
        # `attachment` lets opencode's Read tool put an image into the prompt
        # instead of answering "this model does not support image input".
        # The catalogue, not this script, decides which models see images.
        if has(card.get("input_modalities"), "image"):
            entry["attachment"] = True
            entry["modalities"] = {"input": list(card["input_modalities"])}
        models[model_id] = entry
    return models


def placeholder_models():
    return {
        PLACEHOLDER_ID: {
            "name": "Replace with a model id from GET /v1/models",
            "limit": {"context": DEFAULT_CONTEXT, "output": DEFAULT_OUTPUT},
        }
    }


def provider_block(base_url, api_key_value, models):
    return {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Pystino Gateway",
        "options": {"baseURL": base_url, "apiKey": api_key_value},
        "models": models,
    }


# --- talking to the terminal and the gateway --------------------------------


def say(message=""):
    print(message, file=sys.stderr)


def die(code, message):
    say(message)
    sys.exit(code)


def open_tty():
    # Unbuffered binary: a text-mode "r+" on a terminal raises "not seekable".
    try:
        return open("/dev/tty", "rb+", buffering=0)
    except OSError:
        return None


def ask(prompt, secret=False):
    """A line from the terminal, or None when there is none to ask."""
    tty = open_tty()
    if tty is None:
        return None
    with tty:
        try:
            if secret:
                return getpass.getpass(prompt)  # opens /dev/tty itself, echo off
            tty.write(prompt.encode())
            line = tty.readline()
        except (EOFError, OSError):
            return None
    return line.decode(errors="replace").rstrip("\r\n") if line else None


def normalise_base_url(raw):
    raw = raw.strip().rstrip("/")
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        die(2, "error: the gateway address must be an absolute http(s) URL, got: %s" % raw)
    if parts.username or parts.password or parts.query or parts.fragment:
        die(2, "error: the gateway address must not carry credentials, a query or a fragment")
    if not raw.endswith("/v1"):
        raw += "/v1"
        say("note: using %s (/v1 appended)" % raw)
    return raw


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # urllib would replay the Authorization header at the redirect target,
    # which may be another host. A gateway that redirects /v1/models is
    # misaddressed; say so instead of leaking the key to wherever it points.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def discover(base_url, api_key):
    """-> (http status or None, cards or None, detail). Never raises."""
    request = urllib.request.Request(
        base_url + "/models",
        headers={
            "Authorization": "Bearer " + api_key,
            "Accept": "application/json",
            "User-Agent": "pystino-opencode-setup",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=20) as response:
            body = json.load(response)
    except urllib.error.HTTPError as exc:
        where = exc.headers.get("Location") if exc.code in (301, 302, 303, 307, 308) else None
        return exc.code, None, ("redirected to %s" % where) if where else ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, None, str(getattr(exc, "reason", exc))
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return 200, None, "the answer is not an OpenAI model list"
    return 200, data, ""


# --- the config file --------------------------------------------------------


def config_target(output):
    if output:
        return os.path.abspath(os.path.expanduser(output))
    # opencode follows the XDG base-directory spec on every platform,
    # macOS included, so the global file is ~/.config/opencode/opencode.json.
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = xdg if xdg and os.path.isabs(xdg) else os.path.expanduser("~/.config")
    return os.path.join(base, "opencode", "opencode.json")


def load_existing(path):
    """-> the parsed document, or None when there is no file to merge into.

    opencode also reads opencode.jsonc, and either file may hold comments or
    trailing commas. Rewriting such a file through a JSON parser would drop
    the comments, so anything that is not strict JSON is refused untouched.
    """
    jsonc = os.path.splitext(path)[0] + ".jsonc"
    if not os.path.exists(path):
        if os.path.basename(path) == "opencode.json" and os.path.exists(jsonc):
            die(
                7,
                "error: %s exists and may hold comments, which this script would drop.\n"
                "Nothing was changed. Run again with --print and paste the block it prints\n"
                "into that file, or with --output to write a separate file." % jsonc,
            )
        return None
    with open(path, encoding="utf-8-sig") as handle:
        text = handle.read()
    if not text.strip():
        return {}
    try:
        doc = json.loads(text)
    except ValueError as exc:
        die(
            7,
            "error: %s is not strict JSON (%s).\n"
            "If it holds comments or trailing commas, this script will not rewrite it:\n"
            "nothing was changed. Run again with --print and paste the block it prints\n"
            "into the file, or with --output to write a separate file." % (path, exc),
        )
    if not isinstance(doc, dict):
        die(7, "error: %s is not a JSON object; nothing was changed." % path)
    if "provider" in doc and not isinstance(doc["provider"], dict):
        die(7, "error: \"provider\" in %s is not an object; nothing was changed." % path)
    return doc


def merge(doc, block, default_model):
    """A copy of doc with provider.pystino set, and `model` when asked."""
    merged = dict(doc)
    if "$schema" not in merged:
        merged = {"$schema": "https://opencode.ai/config.json", **merged}
    providers = dict(merged.get("provider") or {})
    providers["pystino"] = block  # replaces in place, or appends
    merged["provider"] = providers
    if default_model:
        merged["model"] = "pystino/" + default_model
    return merged


def describe(path, before, after, block, default_model):
    old_models = set()
    if before and isinstance(before.get("provider"), dict):
        old = before["provider"].get("pystino")
        if isinstance(old, dict) and isinstance(old.get("models"), dict):
            old_models = set(old["models"])
    new_models = set(block["models"])
    lines = []
    if before is None:
        lines.append("new file: %s" % path)
    else:
        others = [k for k in before if k not in ("provider", "$schema")]
        other_providers = [k for k in (before.get("provider") or {}) if k != "pystino"]
        lines.append("existing file: %s" % path)
        lines.append(
            "  kept as it was: %d other setting(s)%s, %d other provider(s)%s"
            % (
                len(others),
                " (%s)" % ", ".join(others) if others else "",
                len(other_providers),
                " (%s)" % ", ".join(other_providers) if other_providers else "",
            )
        )
    if old_models or (before and "pystino" in (before.get("provider") or {})):
        lines.append(
            "  provider.pystino: replaced (%d model(s) before, %d now; +%d, -%d)"
            % (len(old_models), len(new_models), len(new_models - old_models), len(old_models - new_models))
        )
    else:
        lines.append("  provider.pystino: added (%d model(s))" % len(new_models))
    if default_model:
        previous = (before or {}).get("model")
        lines.append(
            "  model: %s -> pystino/%s" % (previous if previous else "unset", default_model)
            if previous != "pystino/" + default_model
            else "  model: already pystino/%s" % default_model
        )
    else:
        lines.append("  model: left as it was")
    for field in ("enabled_providers", "disabled_providers"):
        listed = after.get(field)
        if isinstance(listed, list):
            if field == "enabled_providers" and "pystino" not in listed:
                lines.append("  WARNING: enabled_providers does not list pystino; opencode will ignore it")
            if field == "disabled_providers" and "pystino" in listed:
                lines.append("  WARNING: disabled_providers lists pystino; opencode will ignore it")
    return lines


def write_config(path, doc, secret_inside, existed):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    mode = 0o600
    if existed and not secret_inside:
        mode = os.stat(path).st_mode & 0o777
    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    # Temp file in the same directory, then rename: a crash or a full disk
    # never leaves half a config behind. mkstemp creates it at 0600, so the
    # key is never readable by anyone else, even briefly.
    fd, tmp = tempfile.mkstemp(prefix=".opencode.json.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def back_up(path):
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    target = "%s.bak-%s" % (path, stamp)
    n = 1
    while os.path.exists(target):
        n += 1
        target = "%s.bak-%s-%d" % (path, stamp, n)
    shutil.copy2(path, target)
    os.chmod(target, 0o600)  # the old file may hold a key
    return target


# --- installing opencode ----------------------------------------------------


def install_opencode(version):
    found = shutil.which("opencode")
    if found:
        try:
            have = subprocess.run(
                [found, "--version"], capture_output=True, text=True, timeout=30
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            have = ""
        if have == version:
            say("opencode %s is already installed (%s)" % (version, found))
            return
    say("installing opencode %s with opencode's own installer (https://opencode.ai/install)" % version)
    if not shutil.which("curl"):
        die(5, "error: curl is required for --install-opencode")
    # The installer has no use for the key, so it is not passed down.
    result = subprocess.run(
        ["bash", "-c", 'curl -fsSL https://opencode.ai/install | bash -s -- --version "$1"', "_", version],
        env={k: v for k, v in os.environ.items() if k != "PYSTINO_API_KEY"},
    )
    if result.returncode != 0:
        die(5, "error: opencode's installer failed (exit %d)" % result.returncode)
    if not shutil.which("opencode"):
        say("note: opencode is installed but not on PATH yet; open a new shell, or add ~/.opencode/bin to PATH")


# --- main -------------------------------------------------------------------


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="install.sh",
        description=(
            "Add the Pystino gateway as a provider in opencode's config. Merges into an "
            "existing file (a backup is kept); never mints a key."
        ),
        epilog=(
            "The API key comes from PYSTINO_API_KEY or a hidden prompt, never from argv. "
            "Without --key-in-env it is written into the file (mode 600)."
        ),
    )
    parser.add_argument("address", nargs="?", help="gateway address, e.g. https://llm.example.org")
    parser.add_argument("--base-url", help="the same, as a flag; PYSTINO_BASE_URL also works")
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="write this file (e.g. ./opencode.json for one project) instead of the global config",
    )
    parser.add_argument("--model", metavar="ID", help="also set opencode's default model to pystino/ID")
    parser.add_argument(
        "--key-in-env",
        action="store_true",
        help="write {env:PYSTINO_API_KEY} instead of the key; export it where opencode starts",
    )
    parser.add_argument("--install-opencode", action="store_true", help="install the pinned opencode first")
    parser.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    parser.add_argument(
        "--print", dest="print_block", action="store_true",
        help="print the provider block (with the {env:...} key reference) instead of writing a file",
    )
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation before writing")
    parser.add_argument(
        "--no-discover", action="store_true",
        help="do not call the gateway; write a placeholder model to edit by hand",
    )
    return parser.parse_args(argv)


def main():
    args = parse_args(sys.argv[1:])

    if args.install_opencode:
        install_opencode(os.environ["PYSTINO_OPENCODE_PIN"])

    base = (
        args.base_url
        or args.address
        or os.environ.get("PYSTINO_BASE_URL")
        or os.environ.get("PYSTINO_SERVED_ORIGIN")
        or ask("Gateway address (e.g. https://llm.example.org): ")
    )
    if not base:
        die(2, "error: no gateway address. Pass it as the first argument or with --base-url.")
    base_url = normalise_base_url(base)
    if base_url.startswith("http://") and urllib.parse.urlsplit(base_url).hostname not in (
        "localhost", "127.0.0.1", "::1",
    ):
        say("warning: %s is plain http; the key will cross the network unencrypted" % base_url)

    api_key = os.environ.get("PYSTINO_API_KEY", "") or ask(
        "Paste your gateway API key (minted in the console, starts with gwk_): ", secret=True
    )
    if not api_key:
        die(2, "error: no API key. Set PYSTINO_API_KEY, or run this in a terminal to be asked.")
    api_key = api_key.strip()
    if not api_key.startswith("gwk_"):
        say("warning: the key does not start with gwk_; continuing, the gateway will judge it")

    models = None
    if args.no_discover:
        say("note: --no-discover; writing a placeholder model")
        models = placeholder_models()
    else:
        status, cards, detail = discover(base_url, api_key)
        if status in (401, 403):
            die(3, "error: the gateway refused the key (HTTP %d). Mint a fresh key in the console;\n"
                   "check for a truncated paste." % status)
        if status == 429:
            die(4, "error: the gateway reports the key's billing group over its cap (HTTP 429).\n"
                   "The key works; wait for the next window or raise the cap, then run this again.")
        if status != 200 or cards is None:
            die(6, "error: could not read %s/models (%s)\n"
                   "Check the address; --no-discover writes a placeholder instead."
                % (base_url, ("HTTP %d " % status if status else "") + detail))
        models = build_models(cards)
        say("%d chat model(s) available to this key" % len(models))
        if not models:
            say("warning: this key sees no chat models; writing a placeholder model")
            models = placeholder_models()
        if args.model and args.model not in models:
            die(2, "error: --model %s is not one of: %s" % (args.model, ", ".join(sorted(models))))

    for model_id, entry in sorted(models.items()):
        traits = [t for t, on in (("reasoning", "variants" in entry), ("images", "attachment" in entry)) if on]
        say("  %-32s %7d ctx %6d out  %s" % (
            model_id, entry["limit"]["context"], entry["limit"]["output"], ", ".join(traits)))

    if args.print_block:
        block = provider_block(base_url, ENV_REF, models)
        print(json.dumps({"provider": {"pystino": block}}, indent=2, ensure_ascii=False))
        say("(the key is a reference to $PYSTINO_API_KEY; export it where opencode starts)")
        return

    secret_inside = not args.key_in_env
    block = provider_block(base_url, api_key if secret_inside else ENV_REF, models)

    path = config_target(args.output)
    before = load_existing(path)
    after = merge(before or {}, block, args.model)
    for line in describe(path, before, after, block, args.model):
        say(line)
    say("  key: %s" % (
        "written into the file (mode 600)" if secret_inside
        else "NOT written; the file refers to $PYSTINO_API_KEY, export it before starting opencode"))

    if args.dry_run:
        say("dry run: nothing written")
        return
    if not args.yes:
        answer = ask("Write it? [Y/n] ")
        if answer is not None and answer.strip().lower() not in ("", "y", "yes"):
            die(1, "aborted; nothing written")

    existed = before is not None
    if existed:
        say("backup: %s" % back_up(path))
    write_config(path, after, secret_inside, existed)
    say("wrote %s" % path)
    if not args.model:
        say("next: start opencode and pick a pystino model with /models (or re-run with --model ID)")
    say("undo: restore the backup, or delete the provider.pystino block (see the docs)")
    say("spend is not shown in opencode; the console has it. A later 401/403 means the key was")
    say("revoked; a 429 with retry-after means the billing group hit its cap.")


main()
PYSTINO_PY
}

main "$@"
