"""The v2 rule model (ADR 0114), as units: validation, evaluation, combination, claims.

Every rule of every pipeline in scope is evaluated (no first match); administrators come
before users; a block refuses first; otherwise the first rule to hide a span decides how, and
``off`` and ``detect_only`` never claim one. A reading rule picks the extractor by order.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from gateway.config import EntityMode, HidingRule, PolicyDefinition
from gateway.privacy import rules as rulelib
from gateway.privacy.combine import plan
from gateway.privacy.evaluate import Evaluator, Facts, Undecidable
from gateway.privacy.finds import FindResult, Item, Verdict
from gateway.privacy.pipelines import Pipeline
from gateway.redaction.http import claim_spans
from llmp_shared import EntitySpan

P1, P2 = str(uuid.uuid4()), str(uuid.uuid4())
USER = uuid.uuid4()


class FakeJudge:
    """Finds what it is told per policy; ``down`` policies cannot answer."""

    def __init__(self, found: dict[str, list[str]], down: Sequence[str] = ()) -> None:
        self.found, self.down = found, set(down)

    def policy_name(self, policy_id: str) -> str | None:
        return {P1: "One", P2: "Two"}.get(policy_id)

    def classifier_name(self, classifier_id: str) -> str | None:
        return None

    async def find(
        self, policy_ids: Sequence[str], texts: Sequence[str], exclude: frozenset[str] = frozenset()
    ) -> FindResult:
        values: dict[str, Item] = {}
        for pid in policy_ids:
            for v in self.found.get(pid, []):
                # "TYPE:value" names the type; a bare value is of type T.
                kind, _, value = v.rpartition(":")
                kind = kind or "T"
                if kind in exclude:
                    continue
                values.setdefault(v, Item("pattern", kind, "pattern", None, value))
        bad = [p for p in policy_ids if p in self.down]
        return FindResult(items=tuple(values.values()), unavailable="down" if bad else None)

    async def classify(self, classifier_id: str, texts: Sequence[str]) -> Verdict:
        return Verdict()


def make_rule(
    name: str = "r",
    *,
    policies: Sequence[str] = (P1,),
    mode: str = "redact",
    conditions: Sequence[rulelib.Condition] = (),
    match: str = "all",
    overrides: dict[str, str] | None = None,
    kind: str = "content",
    action: dict[str, Any] | None = None,
    position: int = 0,
    enabled: bool = True,
) -> rulelib.Rule:
    return rulelib.Rule(
        id=uuid.uuid4(),
        kind=kind,
        name=name,
        enabled=enabled,
        position=position,
        policies=tuple(policies),
        match=match,
        conditions=tuple(conditions),
        mode=mode,
        overrides=overrides or {},
        action=action or ({"level": "default"} if kind == "extract" else {}),
    )


def make_pipeline(
    rules: Sequence[rulelib.Rule], *, user: bool = False, position: int = 0, kind: str = "content"
) -> Pipeline:
    owner = USER if user else None
    return Pipeline(
        id=uuid.uuid4(),
        owner_id=owner,
        kind=kind,
        name="user pipeline" if user else "admin pipeline",
        enabled=True,
        position=position,
        who_mode="users" if user else "all",
        who_ids=frozenset({str(owner)}) if user else frozenset(),
        target_mode="all",
        providers=frozenset(),
        models=frozenset(),
        rules=tuple(rules),
        order=position,
    )


def facts(judge: FakeJudge | None, kind: str = "content", **kw: Any) -> Facts:
    return Facts(kind=kind, judge=judge, texts=("some text",), user_id=str(USER), **kw)


def finds(n: int = 1, negate: bool = False) -> rulelib.Condition:
    return rulelib.Condition("finds", (), negate, n)


# -- validation ------------------------------------------------------------------------


class TestValidation:
    def test_a_content_rule_needs_a_policy_and_a_mode(self) -> None:
        with pytest.raises(rulelib.RuleError, match="at least one policy"):
            rulelib.validate_rule("content", {"name": "r", "mode": "redact"})
        with pytest.raises(rulelib.RuleError, match="what the rule does"):
            rulelib.validate_rule("content", {"name": "r", "policies": [P1]})

    def test_finds_names_no_policy_and_counts_the_rules_own(self) -> None:
        ok = rulelib.validate_rule(
            "content",
            {
                "name": "r",
                "policies": [P1],
                "mode": "block",
                "conditions": [{"key": "finds", "count": 3}],
            },
        )
        assert ok["conditions"][0]["count"] == 3 and ok["conditions"][0]["values"] == []
        with pytest.raises(rulelib.RuleError, match="names none"):
            rulelib.validate_rule(
                "content",
                {
                    "name": "r",
                    "policies": [P1],
                    "mode": "block",
                    "conditions": [{"key": "finds", "values": [P1]}],
                },
            )

    def test_overrides_are_cleaned_and_modes_checked(self) -> None:
        ok = rulelib.validate_rule(
            "content",
            {"name": "r", "policies": [P1], "mode": "anonymise", "overrides": {"it vat": "redact"}},
        )
        assert ok["overrides"] == {"IT_VAT": "redact"}
        with pytest.raises(rulelib.RuleError, match="not a mode"):
            rulelib.validate_rule(
                "content",
                {"name": "r", "policies": [P1], "mode": "anonymise", "overrides": {"A": "nope"}},
            )

    def test_match_is_all_or_any(self) -> None:
        with pytest.raises(rulelib.RuleError, match="all or any"):
            rulelib.validate_rule(
                "content", {"name": "r", "policies": [P1], "mode": "off", "match": "some"}
            )

    def test_classified_is_the_administrators_alone(self) -> None:
        with pytest.raises(rulelib.RuleError, match="only administrators"):
            rulelib.validate_rule(
                "content",
                {
                    "name": "r",
                    "policies": [P1],
                    "mode": "block",
                    "conditions": [{"key": "classified", "values": [P2]}],
                },
                user=True,
            )

    def test_an_extract_rule_has_an_action_and_optional_hiding(self) -> None:
        ok = rulelib.validate_rule("extract", {"name": "r", "action": {"level": "extract_with"}})
        assert (
            ok["action"] == {"level": "extract_with", "reader": "markitdown"}
            and ok["policies"] == []
        )
        with pytest.raises(rulelib.ContentOnReadError):
            rulelib.validate_rule("extract", {"name": "r", "conditions": [{"key": "finds"}]})
        with pytest.raises(rulelib.RuleError, match="blocks with its action"):
            rulelib.validate_rule("extract", {"name": "r", "mode": "block"})
        with pytest.raises(rulelib.RuleError, match="Only an extract rule"):
            rulelib.validate_rule(
                "content",
                {"name": "r", "policies": [P1], "mode": "off", "action": {"level": "block"}},
            )

    def test_the_kind_is_extract_or_content(self) -> None:
        with pytest.raises(rulelib.RuleError):
            rulelib.validate_rule("chat", {"name": "r"})


# -- evaluation ------------------------------------------------------------------------


class TestEvaluation:
    async def test_every_rule_is_judged_there_is_no_first_match(self) -> None:
        a, b = make_rule("a", mode="block"), make_rule("b", position=1)
        ev = await Evaluator().evaluate([a, b], facts(FakeJudge({P1: ["x"]})))
        assert [r.name for r in ev.matched] == ["a", "b"]

    async def test_no_conditions_means_the_policies_find_one_item(self) -> None:
        r = make_rule()
        assert (await Evaluator().evaluate([r], facts(FakeJudge({})))).matched == []
        assert (await Evaluator().evaluate([r], facts(FakeJudge({P1: ["x"]})))).matched == [r]

    async def test_finds_counts_distinct_items_of_the_rules_own_policies_combined(self) -> None:
        r = make_rule(policies=(P1, P2), conditions=(finds(3),))
        judge = FakeJudge({P1: ["a", "b"], P2: ["b", "c"]})  # a, b, c: three distinct
        assert (await Evaluator().evaluate([r], facts(judge))).matched == [r]
        judge = FakeJudge({P1: ["a"], P2: ["a"]})
        assert (await Evaluator().evaluate([r], facts(judge))).matched == []

    async def test_a_block_rule_counts_only_the_types_that_block(self) -> None:
        judge = FakeJudge({P1: ["a"]})  # the fake finds items of type T
        ignored = make_rule(mode="block", overrides={"T": "off"})
        assert (await Evaluator().evaluate([ignored], facts(judge))).matched == []
        blocking = make_rule(mode="block", overrides={"T": "block"})
        assert (await Evaluator().evaluate([blocking], facts(judge))).matched == [blocking]
        # A hiding rule still counts a type overridden to another hiding mode.
        hiding_rule = make_rule(mode="anonymise", overrides={"T": "redact"})
        assert (await Evaluator().evaluate([hiding_rule], facts(judge))).matched == [hiding_rule]

    async def test_a_block_rule_blocks_one_type_and_hides_another(self) -> None:
        rule = make_rule("p", mode="block", overrides={"PERSON": "redact"})
        pipeline = make_pipeline([rule])

        async def run(found: list[str]) -> Any:
            return await plan(Evaluator(), [pipeline], facts(FakeJudge({P1: found})))

        # Only the hidden type: not blocked, hidden by the rule with its mode.
        only = await run(["PERSON:Mario"])
        assert not only.refused
        (hit,) = only.hiding_hits()
        assert hit.rule.name == "p" and dict(hit.rule.overrides) == {"PERSON": "redact"}
        assert hit.rule.mode == "off"
        step = only.runs[0].evaluation.steps[0]
        assert step.result == "matched" and "Hide part" in step.reasons[-1]
        # A blocking type: refused, naming the rule.
        for found in (["IBAN:x"], ["PERSON:Mario", "IBAN:x"]):
            blocked = await run(found)
            assert blocked.refused and blocked.winner is not None
            assert blocked.winner.rule.name == "p" and blocked.hiding_hits() == []
        # Nothing found: nothing.
        none = await run([])
        assert not none.refused and none.hits() == []
        assert none.runs[0].evaluation.steps[0].result == "not_matched"

    async def test_the_hide_part_follows_the_other_conditions_and_an_explicit_finds(self) -> None:
        over = {"PERSON": "redact"}
        judge = FakeJudge({P1: ["PERSON:Mario", "IBAN:x"]})
        # finds >= 2 counts only IBAN: one is short, so no block; the person is still hidden.
        rule = make_rule(mode="block", overrides=over, conditions=(finds(2),))
        ev = await Evaluator().evaluate([rule], facts(judge))
        assert [m.mode for m in ev.matched] == ["off"] and not ev.matched[0].blocks_all
        # The rule's other condition fails: no block part, no hide part.
        pdf = rulelib.Condition("filetype", ("pdf",))
        rule = make_rule(mode="block", overrides=over, conditions=(pdf, finds(1)))
        assert (await Evaluator().evaluate([rule], facts(judge, filetype="txt"))).matched == []

    async def test_the_hide_part_never_fails_open(self) -> None:
        rule = make_rule(mode="block", overrides={"PERSON": "redact"})
        # The count cannot be made: the block part is treated as a match, so it refuses.
        down = FakeJudge({P1: []}, down=[P1])
        assert (await Evaluator().evaluate([rule], facts(down))).matched == [rule]

    async def test_match_any_and_negate(self) -> None:
        ext = {"filetype": "pdf"}
        conds = (
            rulelib.Condition("filetype", ("word",)),
            rulelib.Condition("scan", (), False),
        )
        any_rule = make_rule(kind="extract", policies=(), mode="off", match="any", conditions=conds)
        f = Facts(kind="extract", filetype="word", is_scan=False)
        assert (await Evaluator().evaluate([any_rule], f)).matched == [any_rule]
        all_rule = make_rule(kind="extract", policies=(), mode="off", match="all", conditions=conds)
        assert (await Evaluator().evaluate([all_rule], f)).matched == []
        neg = make_rule(
            kind="extract", policies=(), conditions=(rulelib.Condition("filetype", ("pdf",), True),)
        )
        assert (await Evaluator().evaluate([neg], f)).matched == [neg]
        assert ext

    async def test_a_rule_that_is_off_is_skipped(self) -> None:
        r = make_rule(enabled=False)
        ev = await Evaluator().evaluate([r], facts(FakeJudge({P1: ["x"]})))
        assert ev.matched == [] and ev.steps[0].result == "skipped_off"

    async def test_an_undecidable_condition_never_fails_open(self) -> None:
        judge = FakeJudge({}, down=[P1])
        protective = make_rule(mode="redact", conditions=(finds(),))
        assert (await Evaluator().evaluate([protective], facts(judge))).matched == [protective]
        quiet = make_rule(mode="detect_only", conditions=(finds(),))
        assert (await Evaluator().evaluate([quiet], facts(judge))).matched == []
        negated = make_rule(mode="redact", conditions=(finds(1, negate=True),))
        with pytest.raises(Undecidable):
            await Evaluator().evaluate([negated], facts(judge))


# -- combination -----------------------------------------------------------------------


class TestCombination:
    async def test_a_block_refuses_first_and_names_the_rule(self) -> None:
        hide, block = make_rule("hide", mode="redact"), make_rule("stop", mode="block", position=1)
        combined = await plan(
            Evaluator(), [make_pipeline([hide, block])], facts(FakeJudge({P1: ["x"]}))
        )
        assert combined.refused and combined.winner is not None
        assert combined.winner.rule.name == "stop" and combined.hiding_hits() == []

    async def test_administrators_come_before_users(self) -> None:
        user = make_pipeline([make_rule("user rule")], user=True, position=0)
        admin = make_pipeline([make_rule("admin rule")], position=5)
        # Listed with the user's first: the order the rules apply is not the list order.
        combined = await plan(Evaluator(), [user, admin], facts(FakeJudge({P1: ["x"]})))
        assert [h.rule.name for h in combined.hits()] == ["admin rule", "user rule"]

    async def test_pipelines_run_in_console_order(self) -> None:
        late = make_pipeline([make_rule("late")], position=2)
        early = make_pipeline([make_rule("early")], position=1)
        combined = await plan(Evaluator(), [late, early], facts(FakeJudge({P1: ["x"]})))
        assert [h.rule.name for h in combined.hits()] == ["early", "late"]

    async def test_the_first_matching_extract_rule_picks_the_extractor_admin_first(self) -> None:
        def ext(name: str, reader: str, **kw: Any) -> rulelib.Rule:
            return make_rule(
                name,
                kind="extract",
                policies=(),
                mode="off",
                action={"level": "extract_with", "reader": reader},
                **kw,
            )

        user = make_pipeline([ext("mine", "docling")], user=True, kind="extract")
        admin = make_pipeline(
            [ext("first", "markitdown-ocr"), ext("second", "docling", position=1)], kind="extract"
        )
        combined = await plan(Evaluator(), [user, admin], Facts(kind="extract", user_id=str(USER)))
        assert combined.reader == "markitdown-ocr" and combined.level == "extract_with"
        assert {h.rule.name for h in combined.overridden} == {"second", "mine"}

    async def test_an_extract_block_wins_over_a_reader(self) -> None:
        stop = make_rule("stop", kind="extract", policies=(), mode="off", action={"level": "block"})
        ext = make_rule(
            "with",
            kind="extract",
            policies=(),
            mode="off",
            action={"level": "extract_with"},
            position=1,
        )
        combined = await plan(
            Evaluator(),
            [make_pipeline([ext, stop], kind="extract")],
            Facts(kind="extract", user_id=str(USER)),
        )
        assert combined.refused


# -- claims ----------------------------------------------------------------------------


def hiding(
    name: str,
    defn: dict[str, Any],
    mode: str,
    overrides: dict[str, str] | None = None,
    owner: str = "admin",
) -> HidingRule:
    stamped = PolicyDefinition.model_validate(defn).stamp(
        EntityMode(mode), {k: EntityMode(v) for k, v in (overrides or {}).items()}
    )
    return HidingRule(uuid.uuid4(), name, owner, (stamped,))


TEXT = "mail ada@example.org now"
SPANS = [EntitySpan(start=5, end=20, entity_type="EMAIL_ADDRESS", score=0.9, engine="pattern")]
DEF = {"formats": {"EMAIL_ADDRESS": {}}}


class TestClaims:
    def test_the_first_rule_to_hide_a_span_decides_how(self) -> None:
        claims = claim_spans(
            TEXT, SPANS, [hiding("a", DEF, "anonymise"), hiding("b", DEF, "redact")], 0.0
        )
        assert [(c.rule, c.mode) for c in claims.chosen] == [(0, EntityMode.ANONYMISE)]

    def test_off_and_detect_only_claim_nothing(self) -> None:
        rules = [
            hiding("quiet", DEF, "detect_only"),
            hiding("off", DEF, "off"),
            hiding("hide", DEF, "redact"),
        ]
        claims = claim_spans(TEXT, SPANS, rules, 0.0)
        assert [(c.rule, c.mode) for c in claims.chosen] == [(2, EntityMode.REDACT)]
        assert [index for _, index in claims.observed] == [0]

    def test_an_earlier_rule_with_an_override_that_hides_claims_it(self) -> None:
        rules = [
            hiding("a", DEF, "off", {"EMAIL_ADDRESS": "anonymise_restore"}),
            hiding("b", DEF, "redact"),
        ]
        claims = claim_spans(TEXT, SPANS, rules, 0.0)
        assert [(c.rule, c.mode) for c in claims.chosen] == [(0, EntityMode.ANONYMISE_RESTORE)]

    def test_an_override_that_blocks_is_found_before_anything_is_claimed(self) -> None:
        rules = [hiding("a", DEF, "anonymise"), hiding("b", DEF, "off", {"EMAIL_ADDRESS": "block"})]
        claims = claim_spans(TEXT, SPANS, rules, 0.0)
        assert [index for _, index in claims.blocked] == [1]

    def test_a_later_rule_sees_only_text_not_already_claimed(self) -> None:
        other = EntitySpan(start=10, end=22, entity_type="URL", score=0.9, engine="pattern")
        rules = [hiding("a", DEF, "redact"), hiding("b", {"formats": {"URL": {}}}, "anonymise")]
        claims = claim_spans(TEXT, [*SPANS, other], rules, 0.0)
        # The URL overlaps rule a's claim only in part: its remainder is rule b's.
        assert [(c.rule, TEXT[c.span.start : c.span.end]) for c in claims.chosen] == [
            (0, "ada@example.org"),
            (1, "n"),
        ]

    def test_a_span_that_partly_overlaps_a_claim_keeps_its_remainder(self) -> None:
        text = "Mario Rossi"
        person = EntitySpan(start=0, end=11, entity_type="PERSON", score=0.9, engine="spacy")
        rossi = EntitySpan(start=6, end=11, entity_type="LOCATION", score=0.9, engine="spacy")
        rules = [
            hiding("place", {"entities": {"LOCATION": {}}}, "redact"),
            hiding("who", {"entities": {"PERSON": {}}}, "anonymise"),
        ]
        claims = claim_spans(text, [person, rossi], rules, 0.0)
        assert [(c.rule, text[c.span.start : c.span.end], c.mode) for c in claims.chosen] == [
            (1, "Mario", EntityMode.ANONYMISE),
            (0, "Rossi", EntityMode.REDACT),
        ]


class TestRequestPathLaziness:
    async def test_a_rule_with_no_condition_applies_without_counting_unless_it_blocks(self) -> None:
        judge = FakeJudge({})  # nothing found: the exact trace says "not matched"
        hide, stop = make_rule("hide"), make_rule("stop", mode="block", position=1)
        exact = await Evaluator().evaluate([hide, stop], facts(judge))
        assert exact.matched == []
        lazy = await Evaluator().evaluate([hide, stop], facts(judge, lazy_implicit=True))
        # Hiding finds the same spans itself; only a block has to look before it refuses.
        assert [r.name for r in lazy.matched] == ["hide"]


class TestNoPolicyRules:
    def test_a_rule_with_no_policy_may_only_block_or_record_under_a_condition(self) -> None:
        assert rulelib.validate_rule("content", {"name": "r", "mode": "block"})["policies"] == []
        ok = rulelib.validate_rule(
            "content",
            {
                "name": "r",
                "mode": "detect_only",
                "conditions": [{"key": "classified", "values": [P2]}],
            },
        )
        assert ok["policies"] == []
        with pytest.raises(rulelib.RuleError):
            rulelib.validate_rule("content", {"name": "r", "mode": "detect_only"})
        with pytest.raises(rulelib.RuleError):
            rulelib.validate_rule("content", {"name": "r", "mode": "anonymise"})

    async def test_a_no_policy_block_with_no_condition_blocks_everything_in_scope(self) -> None:
        stop = make_rule("all", policies=(), mode="block")
        combined = await plan(Evaluator(), [make_pipeline([stop])], facts(FakeJudge({})))
        assert combined.refused and combined.winner is not None

    async def test_an_override_set_to_off_is_not_counted(self) -> None:
        r = make_rule(mode="block", overrides={"T": "off"}, conditions=(finds(),))
        ev = await Evaluator().evaluate([r], facts(FakeJudge({P1: ["x"]})))
        assert ev.matched == []
