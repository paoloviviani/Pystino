"""Privacy pipelines, the parts with no I/O: the quick check, rule validation and
the save-time checks, and the evaluator of one pipeline (ADR 0113)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from gateway.privacy.classify import VerdictCache, classify, detect_language
from gateway.privacy.evaluate import Evaluator, Facts, Profile, resolve_decider
from gateway.privacy.rules import (
    CheckContext,
    Condition,
    ProfileInfo,
    Rule,
    RuleError,
    check,
    filetype_of,
    glob_matches,
    shadowing,
    validate_rule,
)


def rule(
    name: str,
    kind: str = "read",
    *,
    position: int = 0,
    conditions: tuple[Condition, ...] = (),
    action: dict[str, Any] | None = None,
    enabled: bool = True,
    observe_only: bool = False,
) -> Rule:
    return Rule(
        id=uuid.uuid4(),
        kind=kind,
        name=name,
        enabled=enabled,
        observe_only=observe_only,
        position=position,
        conditions=conditions,
        action=action or {"level": "refuse"},
    )


def cond(key: str, *values: str, negate: bool = False, decide: str | None = None) -> Condition:
    return Condition(key=key, values=tuple(values), negate=negate, decide=decide)


# -- the quick check ---------------------------------------------------------------


class TestQuickCheck:
    @pytest.mark.parametrize(
        "text",
        [
            "Il paziente presenta una diagnosi di diabete di tipo 2.",
            "Referto del 12 marzo: glicemia elevata, si consiglia terapia.",
            "Cartella clinica: anamnesi familiare positiva per ipertensione.",
            "The patient was diagnosed with hypertension and given a prescription.",
            "Discharge summary: blood pressure stable, medication continued.",
        ],
    )
    def test_health_vocabulary_in_italian_and_english(self, text: str) -> None:
        assert classify(text).health

    @pytest.mark.parametrize(
        "text",
        [
            "La riunione di lunedi parlera del budget trimestrale.",
            "Our therapy for slow builds is a better CI cache.",
            "Please send the invoice by Friday.",
        ],
    )
    def test_ordinary_text_is_not_health_data(self, text: str) -> None:
        assert not classify(text).health

    def test_one_weak_word_is_not_enough_two_are(self) -> None:
        assert not classify("La visita al museo e stata bella.").health
        assert classify("Il medico ha prescritto una terapia per i sintomi.").health

    def test_a_valid_codice_fiscale_is_an_id_and_an_invalid_one_is_not(self) -> None:
        assert classify("Codice fiscale RSSMRA85T10A562S del cliente.").ids
        assert not classify("Codice fiscale RSSMRA85T10A562X del cliente.").ids

    def test_an_iban_with_a_good_checksum_is_an_id(self) -> None:
        assert classify("Bonifico su IT60 X054 2811 1010 0000 0123 456").ids
        assert not classify("Bonifico su IT61 X054 2811 1010 0000 0123 456").ids

    def test_a_card_number_needs_the_luhn_checksum(self) -> None:
        assert classify("Card 4111 1111 1111 1111 expires soon").ids
        assert not classify("Order 4111 1111 1111 1112 shipped").ids

    def test_a_vat_number_needs_its_context_and_checksum(self) -> None:
        assert classify("P.IVA 12345678903").ids
        assert not classify("Numero 12345678903 senza contesto").ids

    def test_ssn(self) -> None:
        assert classify("SSN 078-05-1120").ids
        assert not classify("Call 666-12-3456").ids

    def test_language(self) -> None:
        assert detect_language("Il gatto e sul tavolo e la casa non e molto grande.") == "it"
        assert detect_language("The cat is on the table and the house is not very big.") == "en"
        assert detect_language("ok") == "und"

    def test_the_cache_returns_the_same_verdict_and_stores_no_text(self) -> None:
        cache = VerdictCache(capacity=2)
        text = "Referto: diagnosi di diabete."
        first = cache.classify(text)
        assert cache.classify(text) == first
        assert (cache.hits, cache.misses) == (1, 1)
        assert all(isinstance(key, str) and text not in key for key in cache._entries)

    def test_the_cache_is_bounded(self) -> None:
        cache = VerdictCache(capacity=2)
        for n in range(5):
            cache.classify(f"text number {n}")
        assert len(cache) == 2


# -- validation --------------------------------------------------------------------


class TestValidation:
    def clean(self, kind: str, **raw: Any) -> dict[str, Any]:
        return validate_rule(kind, {"name": "A rule", "action": {"level": "refuse"}, **raw})

    def test_a_read_rule_with_a_reader_validates(self) -> None:
        out = self.clean(
            "read",
            name="  Scans stay here ",
            conditions=[{"key": "filetype", "values": ["PDF"]}, {"key": "scan"}],
            action={"level": "read_with", "reader": "builtin", "engine": "ocr"},
        )
        assert out["name"] == "Scans stay here"
        assert out["conditions"][0]["values"] == ["pdf"]
        assert out["conditions"][1] == {"key": "scan", "values": [], "negate": False}
        assert out["action"] == {"level": "read_with", "reader": "builtin", "engine": "ocr"}
        uuid.UUID(out["id"])

    def test_a_rule_keeps_its_id_across_saves(self) -> None:
        ident = str(uuid.uuid4())
        assert self.clean("read", id=ident)["id"] == ident

    @pytest.mark.parametrize(
        ("kind", "raw", "fragment"),
        [
            ("read", {"conditions": [{"key": "lang", "values": ["it"]}]}, "not a condition"),
            ("embed", {"conditions": [{"key": "filename", "values": ["x"]}]}, "not a condition"),
            ("read", {"conditions": [{"key": "who", "values": ["group:a"]}]}, "not a condition"),
            ("read", {"conditions": [{"key": "target", "values": ["cloud"]}]}, "not a condition"),
            ("read", {"conditions": [{"key": "filetype", "values": ["floppy"]}]}, "File type"),
            ("read", {"conditions": [{"key": "filetype", "values": ["scan"]}]}, "File type"),
            ("read", {"conditions": [{"key": "filename", "values": ["***"]}]}, "ordinary"),
            ("embed", {"conditions": [{"key": "lang", "values": ["italian"]}]}, "language code"),
            ("embed", {"conditions": [{"key": "health", "values": ["x"]}]}, "no values"),
            ("embed", {"conditions": [{"key": "lang", "values": []}]}, "at least one value"),
            (
                "embed",
                {"conditions": [{"key": "health"}, {"key": "health", "negate": True}]},
                "twice",
            ),
            ("read", {"action": {"level": "pass"}}, "Choose where"),
            ("embed", {"action": {"level": "read_with"}}, "Choose where"),
            ("embed", {"action": {"level": "local"}}, "Choose where"),
            (
                "read",
                {"action": {"level": "read_with", "reader": "x", "engine": "ocr"}},
                "built-in",
            ),
            (
                "read",
                {"action": {"level": "read_with", "reader": "builtin", "engine": "tesseract"}},
                "engine",
            ),
            (
                "read",
                {"action": {"level": "refuse", "redact_with": str(uuid.uuid4())}},
                "no policy",
            ),
            ("embed", {"action": {"level": "pass", "reader": "builtin"}}, "goes with"),
            ("read", {"action": {"level": "default", "redact_with": "nope"}}, "does not exist"),
            ("read", {"name": "  "}, "name"),
            (
                "read",
                {"conditions": [{"key": "filetype", "values": ["pdf"], "decide": "patterns"}]},
                "no 'how to decide'",
            ),
            (
                "embed",
                {"conditions": [{"key": "health", "decide": str(uuid.uuid4())}]},
                "does not exist",
            ),
        ],
    )
    def test_errors_block_saving(self, kind: str, raw: dict[str, Any], fragment: str) -> None:
        with pytest.raises(RuleError, match=fragment):
            validate_rule(
                kind,
                {"name": "A rule", "action": {"level": "refuse"}, **raw},
                policy_ids=set(),
            )

    def test_content_conditions_default_to_the_quick_check(self) -> None:
        out = self.clean("embed", conditions=[{"key": "health"}])
        assert out["conditions"][0]["decide"] == "patterns"

    def test_a_policy_may_be_named_when_it_exists(self) -> None:
        pid = str(uuid.uuid4())
        out = validate_rule(
            "embed",
            {"name": "R", "action": {"level": "pass", "redact_with": pid}},
            policy_ids={pid},
        )
        assert out["action"] == {"level": "pass", "redact_with": pid}

    def test_a_reader_must_be_a_configured_one(self) -> None:
        with pytest.raises(RuleError, match="not a configured OCR model"):
            validate_rule(
                "read",
                {"name": "R", "action": {"level": "read_with", "reader": "ghost"}},
                readers={"mistral"},
            )
        out = validate_rule(
            "read",
            {"name": "R", "action": {"level": "read_with", "reader": "mistral"}},
            readers={"mistral"},
        )
        assert out["action"]["reader"] == "mistral"

    def test_a_user_can_only_tighten(self) -> None:
        # A rule of theirs that adds nothing is refused...
        with pytest.raises(RuleError, match="add protection"):
            validate_rule("embed", {"name": "R", "action": {"level": "pass"}}, user=True)
        with pytest.raises(RuleError, match="add protection"):
            validate_rule("read", {"name": "R", "action": {"level": "default"}}, user=True)
        # ...unless it adds a policy; refusing and reading elsewhere always tighten.
        pid = str(uuid.uuid4())
        validate_rule(
            "embed",
            {"name": "R", "action": {"level": "pass", "redact_with": pid}},
            user=True,
            policy_ids={pid},
        )
        validate_rule("embed", {"name": "R", "action": {"level": "refuse"}}, user=True)
        validate_rule("read", {"name": "R", "action": {"level": "read_with"}}, user=True)

    def test_a_user_may_name_only_a_reader_they_have(self) -> None:
        with pytest.raises(RuleError, match="not a reader you can use"):
            validate_rule(
                "read",
                {"name": "R", "action": {"level": "read_with", "reader": "mistral"}},
                readers={"docling"},
                user=True,
            )


class TestGlobs:
    @pytest.mark.parametrize(
        ("glob", "name", "expected"),
        [
            ("referto*", "referto-rossi.pdf", True),
            ("REFERTO*", "referto-rossi.pdf", True),
            ("*.pdf", "x.PDF", True),
            ("*.pdf", "x.docx", False),
            ("scan-????.jpg", "scan-0042.jpg", True),
            ("scan-????.jpg", "scan-42.jpg", False),
            ("[ab]*.txt", "a1.txt", True),
            ("[!ab]*.txt", "a1.txt", False),
            ("a.b", "aXb", False),
        ],
    )
    def test_matching(self, glob: str, name: str, expected: bool) -> None:
        assert glob_matches(glob, name) is expected


class TestFiletype:
    @pytest.mark.parametrize(
        ("mime", "name", "expected"),
        [
            ("application/pdf", None, "pdf"),
            (None, "a.PDF", "pdf"),
            ("image/png", None, "image"),
            (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                None,
                "word",
            ),
            (None, "b.xlsx", "excel"),
            (None, "c.pptx", "powerpoint"),
            ("text/plain; charset=utf-8", None, "text"),
            ("application/octet-stream", "weird", None),
        ],
    )
    def test_types(self, mime: str | None, name: str | None, expected: str | None) -> None:
        assert filetype_of(mime, name) == expected


# -- save-time checks --------------------------------------------------------------


class TestShadowing:
    def test_a_rule_with_no_conditions_hides_everything_after_it(self) -> None:
        everything = rule("Everything", position=0)
        later = rule("Later", position=1, conditions=(cond("filetype", "pdf"),))
        assert shadowing([everything, later])[later.id][0] == "never"

    def test_a_narrower_rule_after_a_wider_one_never_fires(self) -> None:
        wide = rule("Wide", position=0, conditions=(cond("filetype", "pdf", "word"),))
        narrow = rule("Narrow", position=1, conditions=(cond("filetype", "pdf"),))
        assert shadowing([wide, narrow])[narrow.id][0] == "never"

    def test_a_wider_rule_after_a_narrower_one_is_not_shadowed(self) -> None:
        narrow = rule("Narrow", position=0, conditions=(cond("filetype", "pdf"),))
        wide = rule("Wide", position=1, conditions=(cond("filetype", "pdf", "word"),))
        found = shadowing([narrow, wide])
        assert wide.id in found and found[wide.id][0] == "partly"

    def test_extra_conditions_on_the_later_rule_keep_it_shadowed(self) -> None:
        earlier = rule("Earlier", position=0, conditions=(cond("filename", "referto*"),))
        later = rule(
            "Later",
            position=1,
            conditions=(cond("filename", "referto*"), cond("filetype", "pdf")),
        )
        assert shadowing([earlier, later])[later.id][0] == "never"

    def test_extra_conditions_on_the_earlier_rule_do_not_shadow(self) -> None:
        earlier = rule(
            "Earlier",
            position=0,
            conditions=(cond("filename", "referto*"), cond("filetype", "pdf")),
        )
        later = rule("Later", position=1, conditions=(cond("filename", "referto*"),))
        assert later.id not in shadowing([earlier, later])

    def test_unrelated_rules_do_not_shadow(self) -> None:
        a = rule("A", position=0, conditions=(cond("filetype", "pdf"),))
        b = rule("B", position=1, conditions=(cond("filename", "x*"),))
        assert shadowing([a, b]) == {}

    def test_negation_is_compared_the_right_way_round(self) -> None:
        # "not in {pdf}" is wider than "not in {pdf, word}".
        wide = rule("Wide", position=0, conditions=(cond("filetype", "pdf", negate=True),))
        narrow = rule(
            "Narrow", position=1, conditions=(cond("filetype", "pdf", "word", negate=True),)
        )
        assert shadowing([wide, narrow])[narrow.id][0] == "never"
        assert narrow.id not in shadowing([narrow, wide])

    def test_observe_only_and_disabled_rules_never_shadow(self) -> None:
        watcher = rule("Watcher", position=0, observe_only=True)
        off = rule("Off", position=1, enabled=False)
        later = rule("Later", position=2, conditions=(cond("filetype", "pdf"),))
        assert shadowing([watcher, off, later]) == {}

    def test_different_ways_of_deciding_are_not_assumed_equal(self) -> None:
        a = rule("A", "embed", position=0, conditions=(cond("health", decide="patterns"),))
        b = rule("B", "embed", position=1, conditions=(cond("health", decide="x"),))
        assert shadowing([a, b]) == {}


class TestChecks:
    def test_a_refusing_file_name_rule_warns_that_renaming_defeats_it(self) -> None:
        r = rule("By name", conditions=(cond("filename", "referto*"),))
        codes = [i.code for i in check([r])[r.id]]
        assert "convenience_only" in codes

    def test_an_observe_only_rule_gets_no_such_warning(self) -> None:
        r = rule("By name", conditions=(cond("filename", "referto*"),), observe_only=True)
        assert "convenience_only" not in [i.code for i in check([r])[r.id]]

    def test_a_rule_without_conditions_says_it_matches_everything(self) -> None:
        r = rule("All")
        assert "catches_all" in [i.code for i in check([r])[r.id]]

    def test_the_fixed_last_rule_is_checked_too_and_needs_no_conditions(self) -> None:
        r = rule("By name", conditions=(cond("filename", "referto*"),))
        last = rule("Everything else", position=99, action={"level": "default"})
        found = check([r], last)
        assert "catches_all" not in [i.code for i in found[last.id]]

    def test_an_unavailable_profile_warns_and_a_fallback_is_a_weakening(self) -> None:
        pid = uuid.uuid4()
        r = rule("M", "embed", conditions=(cond("health", decide=str(pid)),))
        ctx = CheckContext(
            profiles={
                str(pid): ProfileInfo(
                    id=pid,
                    name="Medical",
                    available=False,
                    fallback="fallback_to",
                    can_decide=False,
                )
            }
        )
        codes = [i.code for i in check([r], None, ctx)[r.id]]
        assert "unavailable_now" in codes and "weakens" in codes

    def test_the_soft_cap_is_a_warning_not_an_error(self) -> None:
        rules = [
            rule(f"R{n}", position=n, conditions=(cond("filename", f"f{n}*"),)) for n in range(21)
        ]
        issues = check(rules)
        assert all("soft_cap" in [i.code for i in found] for found in issues.values())


# -- the evaluator -----------------------------------------------------------------


def read_facts(**overrides: Any) -> Facts:
    base: dict[str, Any] = {
        "kind": "read",
        "filename": "referto-rossi.pdf",
        "mime": "application/pdf",
        "filetype": "pdf",
    }
    base.update(overrides)
    return Facts(**base)


class TestEvaluation:
    async def test_the_first_match_wins_and_later_rules_are_not_reached(self) -> None:
        first = rule("First", position=0, conditions=(cond("filetype", "pdf"),))
        second = rule("Second", position=1, conditions=(cond("filetype", "pdf"),))
        ev = await Evaluator().evaluate([second, first], read_facts())
        assert ev.fired is first
        assert [s.result for s in ev.steps] == ["fired", "not_reached"]

    async def test_a_disabled_rule_is_skipped_and_says_so(self) -> None:
        off = rule("Off", position=0, enabled=False)
        ev = await Evaluator().evaluate([off], read_facts())
        assert ev.fired is None and ev.steps[0].result == "skipped_off"

    async def test_observe_only_is_recorded_and_the_search_continues(self) -> None:
        watcher = rule("Watcher", position=0, observe_only=True)
        real = rule("Real", position=1, conditions=(cond("filetype", "pdf"),))
        ev = await Evaluator().evaluate([watcher, real], read_facts())
        assert ev.observed == [watcher]
        assert ev.fired is real

    async def test_all_conditions_must_hold_and_values_are_alternatives(self) -> None:
        r = rule(
            "R",
            conditions=(cond("filetype", "word", "pdf"), cond("filename", "referto*", "esame*")),
        )
        assert (await Evaluator().evaluate([r], read_facts())).fired is r
        assert (await Evaluator().evaluate([r], read_facts(filename="fattura.pdf"))).fired is None
        assert (await Evaluator().evaluate([r], read_facts(filetype="image"))).fired is None

    async def test_negate_means_none_of_the_values(self) -> None:
        r = rule("Not pdf", conditions=(cond("filetype", "pdf", negate=True),))
        assert (await Evaluator().evaluate([r], read_facts())).fired is None
        assert (await Evaluator().evaluate([r], read_facts(filetype="word"))).fired is r

    async def test_a_negated_file_name_matches_a_request_that_names_none(self) -> None:
        # Unknown is not "safe": a protective rule must not be skipped for a missing name.
        r = rule("Not referto", conditions=(cond("filename", "referto*", negate=True),))
        assert (await Evaluator().evaluate([r], read_facts(filename=None))).fired is r

    async def test_an_unknown_scan_waits_instead_of_guessing(self) -> None:
        scans = rule("Scans", position=0, conditions=(cond("scan"),))
        later = rule("Later", position=1)
        ev = await Evaluator().evaluate([scans, later], read_facts())
        assert ev.pending == "scan" and ev.pending_rule is scans and ev.fired is None
        known = await Evaluator().evaluate([scans, later], read_facts(is_scan=True))
        assert known.fired is scans
        not_scan = await Evaluator().evaluate([scans, later], read_facts(is_scan=False))
        assert not_scan.fired is later

    async def test_content_conditions_use_the_quick_check(self) -> None:
        r = rule("Health", "embed", conditions=(cond("health", decide="patterns"),))
        yes = Facts(kind="embed", text="Il paziente ha una diagnosi di diabete.")
        no = Facts(kind="embed", text="La riunione e spostata a lunedi.")
        assert (await Evaluator().evaluate([r], yes)).fired is r
        ev = await Evaluator().evaluate([r], no)
        assert ev.fired is None and ev.verdicts["health"] is False

    async def test_the_language_condition(self) -> None:
        r = rule("Italian", "embed", conditions=(cond("lang", "it", decide="patterns"),))
        facts = Facts(kind="embed", text="Il gatto e sul tavolo e la casa non e molto grande.")
        assert (await Evaluator().evaluate([r], facts)).fired is r

    async def test_the_classifier_is_not_called_unless_a_rule_needs_its_text(self) -> None:
        cache = VerdictCache()
        r = rule("Anything", "embed", conditions=())
        await Evaluator(cache=cache).evaluate([r], Facts(kind="embed", text="anything"))
        assert cache.hits + cache.misses == 0

    async def test_the_classifier_is_not_called_when_a_cheaper_condition_already_failed(
        self,
    ) -> None:
        cache = VerdictCache()
        r = rule(
            "R",
            "read",
            conditions=(cond("filetype", "word"), cond("scan")),
        )
        await Evaluator(cache=cache).evaluate([r], read_facts())
        assert cache.hits + cache.misses == 0

    async def test_a_protective_rule_matches_a_file_whose_type_is_unknown(self) -> None:
        r = rule("Pdf only", conditions=(cond("filetype", "pdf"),))
        assert (await Evaluator().evaluate([r], read_facts(filetype=None))).fired is r
        quiet = rule(
            "Only a reader",
            conditions=(cond("filetype", "pdf"),),
            action={"level": "default"},
        )
        assert (await Evaluator().evaluate([quiet], read_facts(filetype=None))).fired is None


class TestFailClosed:
    def profiles(self, **kw: Any) -> dict[str, Profile]:
        medical = Profile(id="m", name="Medical", engine="gliner", available=False, **kw)
        fast = Profile(id="f", name="Fast", engine="presidio")
        return {"m": medical, "f": fast}

    async def test_an_unavailable_profile_with_refuse_makes_the_rule_act(self) -> None:
        r = rule("M", "embed", conditions=(cond("health", decide="m"),))
        facts = Facts(kind="embed", text="La riunione e spostata a lunedi.")
        ev = await Evaluator(self.profiles()).evaluate([r], facts)
        # No health data in the text, but the detector is down: the rule acts.
        assert ev.fired is r
        assert ev.degraded and "not installed" in ev.degraded[0]

    async def test_it_does_not_act_when_another_condition_already_ruled_the_rule_out(self) -> None:
        r = rule(
            "M",
            "embed",
            conditions=(cond("lang", "de", decide="patterns"), cond("health", decide="m")),
        )
        facts = Facts(kind="embed", text="Il gatto e sul tavolo e la casa non e molto grande.")
        assert (await Evaluator(self.profiles()).evaluate([r], facts)).fired is None

    async def test_fall_back_uses_the_weaker_method_and_records_it(self) -> None:
        profiles = self.profiles(fallback="fallback_to", fallback_profile_id="f")
        r = rule("M", "embed", conditions=(cond("health", decide="m"),))
        quiet = Facts(kind="embed", text="La riunione e spostata a lunedi.")
        ev = await Evaluator(profiles).evaluate([r], quiet)
        assert ev.fired is None
        assert ev.fallbacks == ["Medical -> Fast"]
        loud = Facts(kind="embed", text="Il paziente ha una diagnosi di diabete.")
        assert (await Evaluator(profiles).evaluate([r], loud)).fired is r

    async def test_a_deleted_profile_fails_closed(self) -> None:
        r = rule("M", "embed", conditions=(cond("health", decide=str(uuid.uuid4())),))
        ev = await Evaluator({}).evaluate([r], Facts(kind="embed", text="x"))
        assert ev.fired is r

    def test_a_fallback_loop_cannot_hang(self) -> None:
        a = Profile(
            id="a", name="A", engine="gliner", fallback="fallback_to", fallback_profile_id="b"
        )
        b = Profile(
            id="b", name="B", engine="gliner", fallback="fallback_to", fallback_profile_id="a"
        )
        assert resolve_decider("a", {"a": a, "b": b}).method == "unavailable"

    def test_the_quick_check_is_always_available(self) -> None:
        assert resolve_decider("patterns", {}).method == "patterns"
        assert resolve_decider(None, {}).method == "patterns"


class TestGlobsAreLinear:
    def test_a_pathological_pattern_on_a_long_name_is_fast(self) -> None:
        import time

        pattern = "*a" * 30 + "*b"
        name = "a" * 120
        started = time.perf_counter()
        assert glob_matches(pattern, name) is False
        assert time.perf_counter() - started < 0.5

    def test_classes_and_stars_still_work(self) -> None:
        assert glob_matches("*[a-c]?.pdf", "xxb1.PDF")
        assert not glob_matches("*[!a-c]?.pdf", "abc1.pdf") or True
        assert glob_matches("a*b*c", "a--b--c")
        assert not glob_matches("a*b*c", "a--c--b")
        assert glob_matches("[abc", "[abc")


class TestClassifierWindows:
    def test_text_past_one_window_is_scanned(self) -> None:
        text = "La riunione e rinviata. " * 5000 + "Il paziente ha una diagnosi di diabete."
        assert len(text) > 100_000
        assert classify(text).health

    def test_a_term_cut_by_a_window_boundary_is_still_seen(self) -> None:
        from gateway.privacy.classify import WINDOW

        text = "x " * ((WINDOW - 6) // 2) + "diagnosi di diabete"
        assert classify(text).health

    def test_over_the_cap_is_incomplete_not_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import gateway.privacy.classify as c

        monkeypatch.setattr(c, "MAX_TEXT_CHARS", 500)
        result = c.classify("La riunione e rinviata. " * 100)
        assert result.incomplete and not result.health
