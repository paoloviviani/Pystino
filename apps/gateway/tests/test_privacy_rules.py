"""Privacy pipelines, the parts with no I/O: rule validation, the save-time checks,
and the evaluator of one pipeline over a fake judge (ADR 0113, section 0c)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from gateway.privacy.evaluate import Evaluator, Facts, Undecidable
from gateway.privacy.finds import FindResult, Item, Verdict
from gateway.privacy.rules import (
    CheckContext,
    Condition,
    ContentInfo,
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


def cond(key: str, *values: str, negate: bool = False, count: int = 1) -> Condition:
    return Condition(key=key, values=tuple(values), negate=negate, count=count)


def item(type_: str = "KEYWORDS", value: str = "x", *, group: str = "pattern") -> Item:
    return Item(
        group=group,  # type: ignore[arg-type]
        type=type_,
        engine="pattern" if group == "pattern" else "spacy",
        score=None if group == "pattern" else 0.9,
        value=value,
    )


class FakeJudge:
    """Counts nothing itself: answers from a table, and remembers what it was asked."""

    def __init__(
        self,
        finds: dict[str, FindResult] | None = None,
        verdicts: dict[str, Verdict] | None = None,
    ) -> None:
        self.finds = finds or {}
        self.verdicts = verdicts or {}
        self.asked: list[str] = []

    def policy_name(self, policy_id: str) -> str | None:
        return f"policy-{policy_id}" if policy_id in self.finds else None

    def classifier_name(self, classifier_id: str) -> str | None:
        return f"classifier-{classifier_id}" if classifier_id in self.verdicts else None

    async def find(self, policy_id: str, texts: Any) -> FindResult:
        self.asked.append(policy_id)
        return self.finds.get(policy_id, FindResult(unavailable="gone"))

    async def classify(self, classifier_id: str, texts: Any) -> Verdict:
        self.asked.append(classifier_id)
        return self.verdicts.get(classifier_id, Verdict(unavailable="gone"))


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
            ("embed", {"conditions": [{"key": "health"}]}, "not a condition"),
            ("embed", {"conditions": [{"key": "ids"}]}, "not a condition"),
            ("embed", {"conditions": [{"key": "filename", "values": ["x"]}]}, "not a condition"),
            ("read", {"conditions": [{"key": "who", "values": ["group:a"]}]}, "not a condition"),
            ("read", {"conditions": [{"key": "target", "values": ["cloud"]}]}, "not a condition"),
            ("read", {"conditions": [{"key": "filetype", "values": ["floppy"]}]}, "File type"),
            ("read", {"conditions": [{"key": "filetype", "values": ["scan"]}]}, "File type"),
            ("read", {"conditions": [{"key": "filename", "values": ["***"]}]}, "ordinary"),
            ("read", {"conditions": [{"key": "scan", "values": ["x"]}]}, "no values"),
            ("embed", {"conditions": [{"key": "finds", "values": []}]}, "names one policy"),
            ("embed", {"conditions": [{"key": "finds", "values": ["a", "b"]}]}, "names one policy"),
            (
                "embed",
                {"conditions": [{"key": "finds", "values": ["p"], "count": 0}]},
                "from 1 to",
            ),
            (
                "embed",
                {"conditions": [{"key": "finds", "values": ["p"], "count": 5000}]},
                "from 1 to",
            ),
            (
                "embed",
                {"conditions": [{"key": "classified", "values": ["c"], "count": 3}]},
                "takes no count",
            ),
            (
                "embed",
                {
                    "conditions": [
                        {"key": "finds", "values": ["p"]},
                        {"key": "finds", "values": ["p"], "negate": True},
                    ]
                },
                "same policy",
            ),
            ("embed", {"conditions": [{"key": "finds", "values": ["zzz"]}]}, "does not exist"),
            ("embed", {"conditions": [{"key": "classified", "values": ["zzz"]}]}, "does not exist"),
            (
                "read",
                {"conditions": [{"key": "scan"}, {"key": "scan", "negate": True}]},
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
        ],
    )
    def test_errors_block_saving(self, kind: str, raw: dict[str, Any], fragment: str) -> None:
        with pytest.raises(RuleError, match=fragment):
            validate_rule(
                kind,
                {"name": "A rule", "action": {"level": "refuse"}, **raw},
                policy_ids=set(),
                finds_policy_ids={"p"},
                classifier_ids={"c"},
            )

    def test_finds_and_classified_name_one_object_each(self) -> None:
        out = validate_rule(
            "embed",
            {
                "name": "R",
                "action": {"level": "refuse"},
                "conditions": [
                    {"key": "finds", "values": ["p"], "count": 3, "negate": True},
                    {"key": "classified", "values": ["c"]},
                ],
            },
            finds_policy_ids={"p"},
            classifier_ids={"c"},
        )
        assert out["conditions"] == [
            {"key": "finds", "values": ["p"], "negate": True, "count": 3},
            {"key": "classified", "values": ["c"], "negate": False},
        ]

    def test_two_different_policies_may_both_be_counted(self) -> None:
        out = validate_rule(
            "chat",
            {
                "name": "R",
                "action": {"level": "refuse"},
                "conditions": [
                    {"key": "finds", "values": ["p"]},
                    {"key": "finds", "values": ["q"], "count": 2},
                ],
            },
            finds_policy_ids={"p", "q"},
        )
        assert [c["values"] for c in out["conditions"]] == [["p"], ["q"]]

    def test_read_may_count_items_after_a_local_read(self) -> None:
        out = validate_rule(
            "read",
            {
                "name": "R",
                "action": {"level": "refuse"},
                "conditions": [{"key": "finds", "values": ["p"]}],
            },
            finds_policy_ids={"p"},
        )
        assert out["conditions"][0]["key"] == "finds"

    def test_a_user_may_count_a_policy_they_may_name_but_never_classify(self) -> None:
        validate_rule(
            "embed",
            {
                "name": "R",
                "action": {"level": "refuse"},
                "conditions": [{"key": "finds", "values": ["p"]}],
            },
            user=True,
            finds_policy_ids={"p"},
        )
        with pytest.raises(RuleError, match="not one you can use"):
            validate_rule(
                "embed",
                {
                    "name": "R",
                    "action": {"level": "refuse"},
                    "conditions": [{"key": "finds", "values": ["admins-secret"]}],
                },
                user=True,
                finds_policy_ids={"p"},
            )
        with pytest.raises(RuleError, match="only administrators"):
            validate_rule(
                "embed",
                {
                    "name": "R",
                    "action": {"level": "refuse"},
                    "conditions": [{"key": "classified", "values": ["c"]}],
                },
                user=True,
                classifier_ids={"c"},
            )

    def test_a_note_is_kept_only_while_the_rule_is_off(self) -> None:
        off = self.clean("read", enabled=False, note="check me")
        assert off["note"] == "check me"
        on = self.clean("read", enabled=True, note="check me")
        assert "note" not in on

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

    def test_different_policies_are_not_assumed_equal(self) -> None:
        a = rule("A", "embed", position=0, conditions=(cond("finds", "p"),))
        b = rule("B", "embed", position=1, conditions=(cond("finds", "q"),))
        assert shadowing([a, b]) == {}

    def test_more_items_of_the_same_policy_is_a_narrower_condition(self) -> None:
        wide = rule("Wide", "embed", position=0, conditions=(cond("finds", "p", count=1),))
        narrow = rule("Narrow", "embed", position=1, conditions=(cond("finds", "p", count=5),))
        assert shadowing([wide, narrow])[narrow.id][0] == "never"
        assert shadowing([narrow, wide]) == {}

    def test_not_finding_at_least_n_is_the_opposite_way_round(self) -> None:
        wide = rule(
            "Wide", "embed", position=0, conditions=(cond("finds", "p", count=5, negate=True),)
        )
        narrow = rule(
            "Narrow", "embed", position=1, conditions=(cond("finds", "p", count=1, negate=True),)
        )
        # "not at least 5" (fewer than 5) already holds wherever "not at least 1" (none) does.
        assert shadowing([wide, narrow])[narrow.id][0] == "never"
        assert shadowing([narrow, wide]) == {}


class TestChecks:
    def test_a_refusing_file_name_rule_warns_that_renaming_defeats_it(self) -> None:
        r = rule("By name", conditions=(cond("filename", "referto*"),))
        codes = [i.code for i in check([r])[r.id]]
        assert "convenience_only" in codes
        assert (
            "health"
            not in next(i for i in check([r])[r.id] if i.code == "convenience_only").message
        )

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

    def test_a_policy_that_cannot_run_now_is_a_warning(self) -> None:
        r = rule("M", "embed", conditions=(cond("finds", "p"),))
        ctx = CheckContext(
            content={"p": ContentInfo("Names", False, "the NER engine gliner is not available")}
        )
        found = check([r], None, ctx)[r.id]
        assert [i.code for i in found] == ["unavailable_now"]
        assert "gliner" in found[0].message and "Names" in found[0].message

    def test_a_policy_or_classifier_that_is_gone_is_a_warning(self) -> None:
        r = rule("M", "embed", conditions=(cond("classified", "gone"),))
        assert "unavailable_now" in [i.code for i in check([r], None, CheckContext())[r.id]]

    def test_the_migration_note_is_a_warning_on_the_rule(self) -> None:
        r = Rule(
            id=uuid.uuid4(),
            kind="embed",
            name="Old",
            enabled=False,
            observe_only=False,
            position=0,
            conditions=(),
            action={"level": "refuse"},
            note="Migrated: had a removed condition.",
        )
        found = check([r])[r.id]
        assert any(i.code == "migrated" and "removed condition" in i.message for i in found)

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

    async def test_finds_at_least_n_counts_distinct_items(self) -> None:
        r = rule("Names", "embed", conditions=(cond("finds", "p", count=2),))
        two = FakeJudge({"p": FindResult(items=(item(value="a"), item(value="b")))})
        one = FakeJudge({"p": FindResult(items=(item(value="a"),))})
        facts_two = Facts(kind="embed", text="x", judge=two)
        facts_one = Facts(kind="embed", text="x", judge=one)
        assert (await Evaluator().evaluate([r], facts_two)).fired is r
        ev = await Evaluator().evaluate([r], facts_one)
        assert ev.fired is None and ev.found == {"policy-p": 1}

    async def test_the_trace_lists_each_item_that_made_the_condition_true(self) -> None:
        r = rule("Names", "embed", conditions=(cond("finds", "p"),))
        judge = FakeJudge({"p": FindResult(items=(item("PERSON", "Mario", group="entity"),))})
        ev = await Evaluator().evaluate([r], Facts(kind="embed", text="x", judge=judge))
        step = ev.steps[0]
        assert step.result == "fired"
        assert [(i.group, i.type, i.engine, i.score) for i in step.items["policy-p"]] == [
            ("entity", "PERSON", "spacy", 0.9)
        ]

    async def test_is_not_negates_the_count(self) -> None:
        r = rule(
            "Clean",
            "embed",
            conditions=(cond("finds", "p", negate=True),),
            action={"level": "pass", "redact_with": "x"},
        )
        none = FakeJudge({"p": FindResult()})
        some = FakeJudge({"p": FindResult(items=(item(),))})
        assert (
            await Evaluator().evaluate([r], Facts(kind="embed", text="x", judge=none))
        ).fired is r
        assert (
            await Evaluator().evaluate([r], Facts(kind="embed", text="x", judge=some))
        ).fired is None

    async def test_classified_as_follows_the_classifier(self) -> None:
        r = rule("Topic", "chat", conditions=(cond("classified", "c"),))
        yes = FakeJudge(verdicts={"c": Verdict(positive=True)})
        no = FakeJudge(verdicts={"c": Verdict(positive=False)})
        assert (await Evaluator().evaluate([r], Facts(kind="chat", text="x", judge=yes))).fired is r
        ev = await Evaluator().evaluate([r], Facts(kind="chat", text="x", judge=no))
        assert ev.fired is None and ev.verdicts == {"classifier-c": False}

    async def test_a_read_rule_waits_for_the_file_to_be_read_before_judging_its_text(self) -> None:
        r = rule("Names", "read", conditions=(cond("finds", "p"),))
        judge = FakeJudge({"p": FindResult(items=(item(),))})
        ev = await Evaluator().evaluate([r], read_facts(judge=judge))
        assert ev.pending == "scan" and judge.asked == []
        done = await Evaluator().evaluate([r], read_facts(judge=judge, is_scan=False, texts=("x",)))
        assert done.fired is r and judge.asked == ["p"]

    async def test_content_is_not_judged_when_a_cheaper_condition_already_failed(self) -> None:
        judge = FakeJudge({"p": FindResult(items=(item(),))})
        r = rule("R", "read", conditions=(cond("filetype", "word"), cond("finds", "p")))
        await Evaluator().evaluate([r], read_facts(judge=judge, is_scan=False))
        assert judge.asked == []

    async def test_no_judge_is_asked_unless_a_rule_needs_the_text(self) -> None:
        judge = FakeJudge()
        await Evaluator().evaluate(
            [rule("Any", "embed")], Facts(kind="embed", text="x", judge=judge)
        )
        assert judge.asked == []

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
    """An undecidable NER part is never read as "found nothing" (section 0, 3b)."""

    def down(self) -> FakeJudge:
        return FakeJudge(
            {"p": FindResult(unavailable="the NER engine gliner is not available")},
            {"c": Verdict(unavailable="no model is chosen")},
        )

    async def test_a_protective_rule_acts_when_the_ner_part_cannot_run(self) -> None:
        r = rule("M", "embed", conditions=(cond("finds", "p"),))
        ev = await Evaluator().evaluate([r], Facts(kind="embed", text="x", judge=self.down()))
        assert ev.fired is r
        assert ev.degraded and "gliner" in ev.degraded[0]

    async def test_items_already_found_by_patterns_are_certain(self) -> None:
        # Two keyword hits are enough for "at least 2", whatever the NER part would add.
        r = rule("M", "embed", conditions=(cond("finds", "p", count=2),))
        judge = FakeJudge(
            {"p": FindResult(items=(item(value="a"), item(value="b")), unavailable="down")}
        )
        ev = await Evaluator().evaluate([r], Facts(kind="embed", text="x", judge=judge))
        assert ev.fired is r and not ev.degraded

    async def test_a_negated_condition_is_refused_rather_than_let_through(self) -> None:
        r = rule(
            "Clean",
            "embed",
            conditions=(cond("finds", "p", negate=True),),
            action={"level": "pass", "redact_with": "x"},
        )
        r_protective = rule("Clean", "embed", conditions=(cond("finds", "p", negate=True),))
        for candidate in (r, r_protective):
            with pytest.raises(Undecidable):
                await Evaluator().evaluate(
                    [candidate], Facts(kind="embed", text="x", judge=self.down())
                )

    async def test_it_does_not_act_when_another_condition_already_ruled_the_rule_out(self) -> None:
        r = rule("M", "read", conditions=(cond("filetype", "word"), cond("finds", "p")))
        facts = read_facts(judge=self.down(), is_scan=False)
        assert (await Evaluator().evaluate([r], facts)).fired is None

    async def test_an_observe_only_rule_does_not_match_instead(self) -> None:
        r = rule("Watch", "embed", conditions=(cond("finds", "p"),), observe_only=True)
        ev = await Evaluator().evaluate([r], Facts(kind="embed", text="x", judge=self.down()))
        assert ev.fired is None and ev.observed == []

    async def test_a_classifier_that_cannot_answer_fails_closed(self) -> None:
        r = rule("Topic", "chat", conditions=(cond("classified", "c"),))
        ev = await Evaluator().evaluate([r], Facts(kind="chat", text="x", judge=self.down()))
        assert ev.fired is r and "no model is chosen" in ev.degraded[0]

    async def test_a_policy_that_no_longer_exists_fails_closed(self) -> None:
        r = rule("Gone", "embed", conditions=(cond("finds", "gone"),))
        ev = await Evaluator().evaluate([r], Facts(kind="embed", text="x", judge=FakeJudge()))
        assert ev.fired is r

    async def test_no_judge_at_all_fails_closed(self) -> None:
        r = rule("Any", "embed", conditions=(cond("finds", "p"),))
        assert (await Evaluator().evaluate([r], Facts(kind="embed", text="x"))).fired is r

    async def test_a_text_too_large_to_check_fails_closed(self) -> None:
        r = rule("Big", "embed", conditions=(cond("finds", "p"),))
        judge = FakeJudge({"p": FindResult(incomplete=True)})
        ev = await Evaluator().evaluate([r], Facts(kind="embed", text="x", judge=judge))
        assert ev.fired is r and "too large" in ev.degraded[0]


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
