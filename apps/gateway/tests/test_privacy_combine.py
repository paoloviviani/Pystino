"""Pipelines combine (ADR 0113, section 0): scope, the ladder, policies, readers, hops.

No I/O. The properties this file exists for:

* every pipeline in scope runs, and the highest level on the ladder wins;
* policies add up and the strictest mode wins per type (the fold of ADR 0038);
* an administrator's reader and engine hold over a user's;
* the winner is the administrator's on a tie;
* a user's pipeline can raise a level or add a policy and can never lower anything.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

import pytest
from gateway.config import (
    CustomPattern,
    EntityMode,
    EntityPolicy,
    KeywordList,
    RedactionPolicy,
)
from gateway.errors import ServiceUnavailableError
from gateway.models import PrivacyPipeline
from gateway.privacy.combine import Retarget, plan
from gateway.privacy.evaluate import Evaluator, Facts, Undecidable
from gateway.privacy.finds import ContentJudge
from gateway.privacy.pipelines import (
    Pipeline,
    not_in_scope,
    pipeline_from_row,
    summarize,
    validate_pipeline,
)
from gateway.privacy.rules import RuleError
from gateway.privacy.state import PolicyRef, PrivacyState, UserPolicyRef, user_rule_of

USER = uuid.uuid4()
OTHER = uuid.uuid4()
GROUP = uuid.uuid4()
PROVIDER = uuid.uuid4()
MODEL = uuid.uuid4()


def rule(
    name: str, conditions: list[dict[str, Any]] | None = None, **action: Any
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "enabled": True,
        "observe_only": False,
        "conditions": conditions or [],
        "action": action or {"level": "refuse"},
    }


def pipe(
    kind: str,
    name: str,
    *,
    rules: list[dict[str, Any]] | None = None,
    otherwise: dict[str, Any] | None = None,
    who: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
    owner: uuid.UUID | None = None,
    position: int = 0,
    enabled: bool = True,
) -> Pipeline:
    row = PrivacyPipeline(
        id=uuid.uuid4(),
        owner_id=owner,
        kind=kind,
        name=name,
        enabled=enabled,
        position=position,
        who=who or {"mode": "all", "ids": []},
        target=target or {"mode": "all", "providers": [], "models": []},
        rules=rules or [],
        otherwise=otherwise or {"level": "default" if kind == "read" else "pass"},
    )
    return pipeline_from_row(row, order=position)


def facts(kind: str = "embed", **kw: Any) -> Facts:
    base: dict[str, Any] = {
        "kind": kind,
        "user_id": str(USER),
        "billing_group_id": str(GROUP),
        "provider_id": str(PROVIDER),
        "model_id": str(MODEL),
        "filename": "referto.pdf",
        "mime": "application/pdf",
        "filetype": "pdf",
        "judge": judge(),
    }
    base.update(kw)
    return Facts(**base)


#: A text a keyword list finds, and the policy that finds it. Nothing topic-specific ships
#: with the product; a test writes its own list.
SECRET = "The project codename is Falcon."
POLICY_ID = str(uuid.uuid4())
FINDS = [{"key": "finds", "values": [POLICY_ID]}]
NEEDS_SERVICE_ID = str(uuid.uuid4())


def judge(*, down: bool = False) -> ContentJudge:
    """Counts the keyword list's items; ``down`` is a policy that needs the (absent) service."""
    keywords = RedactionPolicy(keywords=[KeywordList(name="CODENAME", words=["Falcon"])])
    names = RedactionPolicy(entities={"PERSON": EntityPolicy(mode=EntityMode.DETECT_ONLY)})
    return ContentJudge(
        policies={POLICY_ID: ("Codenames", keywords), NEEDS_SERVICE_ID: ("Names", names)},
        classifiers={},
        redactor=None,
        default_threshold=0.5,
        backend=None,
    )


class TestScope:
    def covers(self, p: Pipeline, **kw: Any) -> str:
        args: dict[str, Any] = {
            "user_id": str(USER),
            "group_id": str(GROUP),
            "key_id": None,
            "provider_id": str(PROVIDER),
            "model_id": str(MODEL),
        }
        args.update(kw)
        return not_in_scope(p, **args)

    def test_all_covers_everyone_everywhere(self) -> None:
        assert self.covers(pipe("embed", "P")) == ""

    def test_who(self) -> None:
        users = pipe("embed", "P", who={"mode": "users", "ids": [str(USER)]})
        assert self.covers(users) == ""
        assert self.covers(users, user_id=str(OTHER)) != ""
        groups = pipe("embed", "P", who={"mode": "groups", "ids": [str(GROUP)]})
        assert self.covers(groups) == "" and self.covers(groups, group_id=None) != ""
        keys = pipe("embed", "P", who={"mode": "keys", "ids": [str(OTHER)]})
        assert self.covers(keys) != "" and self.covers(keys, key_id=str(OTHER)) == ""

    def test_target_only_and_all_except_over_providers_and_models(self) -> None:
        only = {"mode": "only", "providers": [str(PROVIDER)], "models": []}
        assert self.covers(pipe("embed", "P", target=only)) == ""
        assert self.covers(pipe("embed", "P", target=only), provider_id=str(uuid.uuid4())) != ""
        by_model = {"mode": "only", "providers": [], "models": [str(MODEL)]}
        assert self.covers(pipe("embed", "P", target=by_model), provider_id=None) == ""
        left_out = {"mode": "except", "providers": [str(PROVIDER)], "models": []}
        assert self.covers(pipe("embed", "P", target=left_out)) != ""
        assert self.covers(pipe("embed", "P", target=left_out), provider_id=str(OTHER)) == ""

    def test_a_pipeline_that_is_off_covers_nothing(self) -> None:
        assert self.covers(pipe("embed", "P", enabled=False)) != ""

    def test_a_users_pipeline_covers_only_its_owner_whatever_is_stored(self) -> None:
        mine = pipe("embed", "Mine", owner=USER, who={"mode": "all", "ids": []})
        assert mine.who_mode == "users" and mine.who_ids == {str(USER)}
        assert self.covers(mine) == ""
        assert self.covers(mine, user_id=str(OTHER)) != ""

    def test_a_user_pipeline_save_ignores_the_who_in_the_body(self) -> None:
        clean = validate_pipeline(
            "embed",
            "Mine",
            {"mode": "all", "ids": []},
            None,
            [],
            None,
            owner_id=USER,
        )
        assert clean["who"] == {"mode": "users", "ids": [str(USER)]}


class TestScopeValidation:
    @pytest.mark.parametrize(
        ("who", "target", "fragment"),
        [
            ({"mode": "everyone"}, None, "Who must be"),
            ({"mode": "users", "ids": []}, None, "at least one"),
            ({"mode": "users", "ids": ["x"]}, None, "ids"),
            (None, {"mode": "local"}, "Target must be"),
            (None, {"mode": "only"}, "at least one provider or model"),
        ],
    )
    def test_errors(self, who: Any, target: Any, fragment: str) -> None:
        with pytest.raises(RuleError, match=fragment):
            validate_pipeline("embed", "P", who, target, [], None)

    def test_names_must_be_unique_within_a_pipeline(self) -> None:
        with pytest.raises(RuleError, match="Two rules"):
            validate_pipeline("embed", "P", None, None, [rule("A"), rule("a")], None)

    def test_a_row_that_does_not_read_is_refused_when_loaded(self) -> None:
        row = PrivacyPipeline(
            id=uuid.uuid4(),
            owner_id=None,
            kind="embed",
            name="P",
            enabled=True,
            position=0,
            who={"mode": "all"},
            target={},
            rules=[{"id": str(uuid.uuid4()), "name": "R", "action": {"level": "read_with"}}],
            otherwise={"level": "pass"},
        )
        with pytest.raises(ValueError, match="no action"):
            pipeline_from_row(row)


async def run(kind: str, pipelines: list[Pipeline], f: Facts, **kw: Any) -> Any:
    return await plan(Evaluator(), pipelines, f, **kw)


class TestTheLadder:
    async def test_the_highest_level_across_pipelines_wins(self) -> None:
        quiet = pipe("embed", "Quiet")
        strict = pipe("embed", "Strict", rules=[rule("Health", FINDS)])
        combined = await run("embed", [quiet, strict], facts(text=SECRET))
        assert combined.level == "refuse" and combined.refused
        assert combined.winner is not None and combined.winner.pipeline is strict
        assert combined.winner.rule.name == "Health"

    async def test_nothing_asking_for_more_is_the_base_level(self) -> None:
        strict = pipe("embed", "Strict", rules=[rule("Health", FINDS)])
        combined = await run("embed", [strict], facts(text="La riunione e lunedi."))
        assert combined.level == "pass" and combined.winner is None

    async def test_on_a_tie_the_administrators_rule_is_named(self) -> None:
        mine = pipe("embed", "Mine", owner=USER, rules=[rule("Mine", FINDS)])
        theirs = pipe("embed", "Theirs", rules=[rule("Theirs", FINDS)], position=5)
        combined = await run("embed", [mine, theirs], facts(text=SECRET))
        assert combined.winner is not None and combined.winner.pipeline is theirs

    async def test_a_user_pipeline_alone_still_raises_the_level(self) -> None:
        mine = pipe("embed", "Mine", owner=USER, rules=[rule("Mine", FINDS)])
        combined = await run("embed", [mine], facts(text=SECRET))
        assert combined.refused and combined.winner is not None and combined.winner.pipeline.is_user

    async def test_a_user_pipeline_can_never_lower_what_an_administrators_did(self) -> None:
        admin = pipe("embed", "Admin", otherwise={"level": "refuse"})
        # The most a user's pipeline may say is a base level with a policy: that is
        # "pass" on the ladder, which does not lower a refusal.
        mine = pipe("embed", "Mine", owner=USER, otherwise={"level": "pass"})
        assert (await run("embed", [admin, mine], facts())).refused

    async def test_the_last_rule_applies_when_nothing_matched(self) -> None:
        p = pipe("embed", "P", rules=[rule("Never", FINDS)], otherwise={"level": "refuse"})
        combined = await run("embed", [p], facts(text="La riunione e lunedi."))
        assert combined.refused and combined.winner is not None
        assert combined.runs[0].is_otherwise

    async def test_observe_only_is_logged_and_the_search_continues(self) -> None:
        watch = rule("Watch", FINDS)
        watch["observe_only"] = True
        p = pipe("embed", "P", rules=[watch, rule("Real", FINDS)])
        combined = await run("embed", [p], facts(text=SECRET))
        assert combined.winner is not None and combined.winner.rule.name == "Real"
        assert [r.name for r in combined.runs[0].evaluation.observed] == ["Watch"]

    async def test_pipelines_out_of_scope_do_not_run_and_say_why(self) -> None:
        elsewhere = pipe(
            "embed",
            "Elsewhere",
            otherwise={"level": "refuse"},
            target={"mode": "only", "providers": [str(OTHER)], "models": []},
        )
        combined = await run("embed", [elsewhere], facts())
        assert combined.level == "pass" and combined.runs == []
        assert combined.skipped[0].why == "the target is not covered"

    async def test_a_pipeline_of_another_kind_is_never_consulted(self) -> None:
        other = pipe("chat", "Chat", otherwise={"level": "refuse"})
        assert (await run("embed", [other], facts())).level == "pass"

    async def test_every_input_is_judged_in_full(self) -> None:
        p = pipe("embed", "P", rules=[rule("Health", FINDS)])
        f = facts(texts=("La riunione e lunedi.", SECRET))
        assert (await run("embed", [p], f)).refused


class TestPolicies:
    async def test_policies_add_up_widest_scope_first_and_each_once(self) -> None:
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        narrow = pipe(
            "chat",
            "Narrow",
            otherwise={"level": "pass", "redact_with": b},
            who={"mode": "users", "ids": [str(USER)]},
        )
        wide = pipe("chat", "Wide", otherwise={"level": "pass", "redact_with": a}, position=1)
        again = pipe("chat", "Again", otherwise={"level": "pass", "redact_with": a}, position=2)
        combined = await run("chat", [narrow, wide, again], facts("chat"))
        assert [u.policy_id for u in combined.policy_uses()] == [a, b]

    async def test_a_refused_request_applies_no_policy(self) -> None:
        a = str(uuid.uuid4())
        p = pipe("chat", "P", otherwise={"level": "pass", "redact_with": a})
        q = pipe("chat", "Q", otherwise={"level": "refuse"})
        assert (await run("chat", [p, q], facts("chat"))).policy_uses() == []

    def state(self) -> tuple[PrivacyState, str, str]:
        state = PrivacyState()
        a, b = uuid.uuid4(), uuid.uuid4()
        state.policies = {
            str(a): PolicyRef(
                a,
                "Default",
                RedactionPolicy(
                    entities={
                        "PERSON": EntityPolicy(mode=EntityMode.ANONYMISE_RESTORE),
                        "IBAN_CODE": EntityPolicy(mode=EntityMode.BLOCK),
                    },
                    allow_list=["acme.test"],
                ),
            ),
            str(b): PolicyRef(
                b,
                "Strict",
                RedactionPolicy(
                    entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)},
                    patterns=[CustomPattern(name="PRJ", regex=r"PRJ-\d+", mode=EntityMode.REDACT)],
                    allow_list=["evil.test"],
                ),
            ),
        }
        return state, str(a), str(b)

    async def test_the_strictest_mode_wins_per_type_and_the_first_allow_list_stands(self) -> None:
        state, a, b = self.state()
        p = pipe("chat", "P1", otherwise={"level": "pass", "redact_with": a})
        q = pipe("chat", "P2", otherwise={"level": "pass", "redact_with": b}, position=1)
        combined = await run("chat", [p, q], facts("chat"))
        effective = state.effective_policy(combined.policy_uses(), RedactionPolicy())
        assert effective.policy.mode_for("PERSON") is EntityMode.REDACT
        assert effective.policy.mode_for("IBAN_CODE") is EntityMode.BLOCK
        assert effective.policy.allow_list == ["acme.test"]
        assert {p.name for p in effective.policy.patterns} == {"PRJ"}

    async def test_no_policy_means_the_environments(self) -> None:
        state, _, _ = self.state()
        env = RedactionPolicy(default_mode=EntityMode.OFF)
        combined = await run("chat", [pipe("chat", "P")], facts("chat"))
        effective = state.effective_policy(combined.policy_uses(), env)
        assert effective.policy is env and effective.rule_id is None

    async def test_a_user_policy_only_tightens_and_never_lowers_an_administrators(self) -> None:
        state, a, _ = self.state()
        weak = uuid.uuid4()
        state.user_policies = {
            str(weak): UserPolicyRef(
                weak,
                USER,
                "Mine",
                (
                    user_rule_of(
                        {"kind": "mode", "name": "IBAN_CODE", "mode": "anonymise"}, uuid.uuid4()
                    ),
                    user_rule_of({"kind": "mode", "name": "PERSON", "mode": "block"}, uuid.uuid4()),
                ),
            )
        }
        admin = pipe("chat", "Admin", otherwise={"level": "pass", "redact_with": a})
        mine = pipe(
            "chat", "Mine", owner=USER, otherwise={"level": "pass", "redact_with": str(weak)}
        )
        combined = await run("chat", [admin, mine], facts("chat"))
        policy = state.effective_policy(combined.policy_uses(), RedactionPolicy()).policy
        # The administrator's block stays whatever the user wrote; their stricter one applies.
        assert policy.mode_for("IBAN_CODE") is EntityMode.BLOCK
        assert policy.mode_for("PERSON") is EntityMode.BLOCK

    async def test_another_users_policy_is_never_applied(self) -> None:
        state, _, _ = self.state()
        theirs = uuid.uuid4()
        state.user_policies = {
            str(theirs): UserPolicyRef(
                theirs,
                OTHER,
                "Theirs",
                (user_rule_of({"kind": "mode", "name": "PERSON", "mode": "block"}, uuid.uuid4()),),
            )
        }
        mine = pipe(
            "chat", "Mine", owner=USER, otherwise={"level": "pass", "redact_with": str(theirs)}
        )
        combined = await run("chat", [mine], facts("chat"))
        # Never applied, and the request is refused (a named 503) rather than run without the
        # policy the pipeline chose.
        from gateway.errors import ServiceUnavailableError

        with pytest.raises(ServiceUnavailableError) as raised:
            state.effective_policy(combined.policy_uses(), RedactionPolicy())
        assert raised.value.code == "privacy_policy_unavailable"


class TestReaders:
    def with_reader(self, name: str, reader: str, **kw: Any) -> Pipeline:
        return pipe("read", name, rules=[rule(name, [], level="read_with", reader=reader)], **kw)

    async def test_the_administrators_engine_model_holds_over_a_users(self) -> None:
        # The extractor's engines are models, so "the engine" is just the reader.
        admin = self.with_reader("Admin", "docling")
        mine = self.with_reader("Mine", "markitdown-ocr", owner=USER)
        combined = await run("read", [mine, admin], facts("read"))
        assert combined.level == "read_with" and combined.reader == "docling"
        assert combined.reader_run is not None and not combined.reader_run.pipeline.is_user
        assert [r.pipeline.name for r in combined.overridden] == ["Mine"]

    async def test_a_users_reader_applies_when_no_administrator_chose_one(self) -> None:
        admin = pipe("read", "Admin")
        mine = self.with_reader("Mine", "markitdown-ocr", owner=USER)
        combined = await run("read", [admin, mine], facts("read"))
        assert combined.reader == "markitdown-ocr"
        assert combined.reader_run is not None and combined.reader_run.pipeline.is_user

    async def test_the_administrators_reader_overrides_the_users(self) -> None:
        admin = self.with_reader("Admin", "facility")
        mine = self.with_reader("Mine", "markitdown", owner=USER)
        combined = await run("read", [mine, admin], facts("read"))
        assert combined.reader == "facility"
        assert [r.pipeline.name for r in combined.overridden] == ["Mine"]

    async def test_refuse_is_above_read_with(self) -> None:
        admin = self.with_reader("Admin", "markitdown")
        refuse = pipe("read", "Refuse", otherwise={"level": "refuse"})
        combined = await run("read", [admin, refuse], facts("read"))
        assert combined.refused and combined.reader is None

    async def test_the_ladder_starts_at_the_default_reader(self) -> None:
        combined = await run("read", [pipe("read", "P")], facts("read"))
        assert combined.level == "default"


class TestRerouteRunsTheNewReadersPipelines:
    async def test_pipelines_covering_the_new_reader_also_run(self) -> None:
        facility = uuid.uuid4()
        facility_model = uuid.uuid4()
        route = pipe(
            "read",
            "Route",
            rules=[rule("To facility", [], level="read_with", reader="facility")],
            target={"mode": "only", "providers": [str(PROVIDER)], "models": []},
        )
        guard = pipe(
            "read",
            "Guards the facility reader",
            otherwise={"level": "refuse"},
            target={"mode": "only", "providers": [str(facility)], "models": []},
        )

        async def retarget(reader: str) -> Retarget:
            assert reader == "facility"
            return Retarget(facts("read", provider_id=str(facility), model_id=str(facility_model)))

        combined = await run("read", [route, guard], facts("read"), retarget=retarget)
        # A reroute cannot escape a pipeline that guards the reader it lands on.
        assert combined.refused and combined.winner is not None
        assert combined.winner.pipeline is guard and combined.winner.hop == 1
        assert [h.reader for h in combined.hops] == ["facility"]
        assert [r.pipeline.name for r in combined.hops[0].added] == [guard.name]

    async def test_a_reroute_that_is_impossible_is_reported_not_guessed(self) -> None:
        route = pipe("read", "Route", otherwise={"level": "read_with", "reader": "markitdown"})

        async def retarget(reader: str) -> Retarget:
            return Retarget(None, impossible="the document is a link")

        combined = await run("read", [route], facts("read"), retarget=retarget)
        assert combined.impossible == "the document is a link"

    async def test_the_loop_is_bounded(self) -> None:
        route = pipe("read", "Route", otherwise={"level": "read_with", "reader": "x"})
        calls = 0

        async def retarget(reader: str) -> Retarget:
            nonlocal calls
            calls += 1
            return Retarget(facts("read", provider_id=str(uuid.uuid4())))

        await run("read", [route], facts("read"), retarget=retarget)
        assert calls <= 3

    async def test_a_pipeline_waiting_for_a_scan_is_pending_not_guessed(self) -> None:
        p = pipe("read", "Scans", rules=[rule("Scans", [{"key": "scan"}])])
        combined = await run("read", [p], facts("read"))
        assert combined.pending and combined.pending_run is not None
        known = await run("read", [p], facts("read", is_scan=True))
        assert not known.pending and known.refused


class TestWords:
    def test_what_a_pipeline_does_in_short_words(self) -> None:
        pid = str(uuid.uuid4())
        cid = str(uuid.uuid4())
        p = pipe(
            "embed",
            "P",
            rules=[
                rule("Codenames", [{"key": "finds", "values": [POLICY_ID], "count": 3}]),
                rule(
                    "Topic",
                    [{"key": "classified", "values": [cid], "negate": True}],
                    level="pass",
                    redact_with=pid,
                ),
            ],
            otherwise={"level": "pass", "redact_with": pid},
        )
        said = summarize(p, {pid: "Default", POLICY_ID: "Codenames", cid: "Refunds"})
        assert "refuses text where Codenames finds 3 or more items" in said
        assert "redacts with Default for text not classified as Refunds" in said
        assert "redacts with Default" in said

    def test_observe_only_rules_and_no_ops_are_left_out(self) -> None:
        watch = rule("Watch", FINDS)
        watch["observe_only"] = True
        assert summarize(pipe("embed", "P", rules=[watch]), {}) == []

    def test_a_reader_is_named(self) -> None:
        p = pipe(
            "read",
            "P",
            rules=[rule("Scans", [{"key": "scan"}], level="read_with", reader="markitdown")],
        )
        assert summarize(p, {}) == ["reads scans with markitdown"]


class TestUndecidableConditionsNeverFailOpen:
    """An undecidable condition matches only where that can only add protection."""

    NAMES: ClassVar[list[dict[str, Any]]] = [{"key": "finds", "values": [NEEDS_SERVICE_ID]}]

    @pytest.mark.parametrize("kind", ["embed", "chat"])
    async def test_a_negated_condition_with_the_service_down_is_refused_not_passed(
        self, kind: str
    ) -> None:
        # "finds no names -> pass", then "everything else -> refuse": assuming the
        # negated condition holds would let the text straight through.
        p = pipe(
            kind,
            "P",
            rules=[
                rule(
                    "No names, pass",
                    [{"key": "finds", "values": [NEEDS_SERVICE_ID], "negate": True}],
                    level="pass",
                    redact_with=str(uuid.uuid4()),
                )
            ],
            otherwise={"level": "refuse"},
        )
        with pytest.raises(ServiceUnavailableError) as raised:
            await plan(Evaluator(), [p], facts(kind, text=SECRET))
        assert "No names, pass" in raised.value.message and "'P'" in raised.value.message
        assert raised.value.code == "privacy_undecidable"

    @pytest.mark.parametrize("kind", ["embed", "chat"])
    async def test_incomplete_input_on_a_negated_condition_is_refused(
        self, kind: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("gateway.privacy.finds.MAX_REQUEST_CHARS", 10)
        p = pipe(
            kind,
            "P",
            rules=[
                rule(
                    "No codename, pass",
                    [{"key": "finds", "values": [POLICY_ID], "negate": True}],
                    level="pass",
                )
            ],
            otherwise={"level": "refuse"},
        )
        with pytest.raises(ServiceUnavailableError):
            await plan(Evaluator(), [p], facts(kind, text="nothing to see here " * 3))

    async def test_a_rule_that_adds_nothing_is_refused_too(self) -> None:
        p = pipe("embed", "P", rules=[rule("Names, pass", self.NAMES, level="pass")])
        with pytest.raises(ServiceUnavailableError):
            await plan(Evaluator(), [p], facts("embed", text=SECRET))

    @pytest.mark.parametrize("kind", ["embed", "chat"])
    async def test_a_protective_rule_on_a_plain_condition_still_acts(self, kind: str) -> None:
        p = pipe(kind, "P", rules=[rule("Names", self.NAMES)])
        combined = await plan(Evaluator(), [p], facts(kind, text="The meeting is on Monday."))
        assert combined.refused

    async def test_the_patterns_part_never_fails(self) -> None:
        # The keyword list is the gateway's own: it answers with no service at all.
        p = pipe("embed", "P", rules=[rule("Codenames", FINDS)])
        assert (await plan(Evaluator(), [p], facts("embed", text=SECRET))).refused
        clean = await plan(Evaluator(), [p], facts("embed", text="The meeting is on Monday."))
        assert not clean.refused

    async def test_an_observe_only_rule_does_not_match_and_does_not_refuse(self) -> None:
        watch = rule("Watch", [{"key": "finds", "values": [NEEDS_SERVICE_ID], "negate": True}])
        watch["observe_only"] = True
        p = pipe("embed", "P", rules=[watch])
        combined = await plan(Evaluator(), [p], facts("embed", text=SECRET))
        assert combined.level == "pass" and combined.runs[0].evaluation.observed == []

    def test_the_evaluator_raises_undecidable_with_the_rule(self) -> None:
        assert issubclass(Undecidable, Exception)


class TestAUserCannotLoosenThroughAnAdministratorsPolicy:
    def state(self) -> tuple[PrivacyState, str, str]:
        state = PrivacyState()
        a, b = uuid.uuid4(), uuid.uuid4()
        state.policies = {
            str(a): PolicyRef(
                a,
                "Keys",
                RedactionPolicy(entities={"EMAIL_ADDRESS": EntityPolicy(mode=EntityMode.REDACT)}),
            ),
            str(b): PolicyRef(
                b,
                "Picked",
                RedactionPolicy(
                    entities={"IBAN_CODE": EntityPolicy(mode=EntityMode.REDACT)},
                    allow_list=["Mario Rossi"],
                    default_mode=EntityMode.OFF,
                ),
            ),
        }
        return state, str(a), str(b)

    async def test_the_allow_list_stays_the_administrators_whoever_ranks_first(self) -> None:
        state, a, b = self.state()
        # An administrator's pipeline scoped to a key: the widest-rank-last in the old order,
        # and the user's own ranked before it. The administrator's must decide the allow-list.
        key = uuid.uuid4()
        admin = pipe(
            "chat",
            "Admin key",
            otherwise={"level": "pass", "redact_with": a},
            who={"mode": "keys", "ids": [str(key)]},
        )
        mine = pipe("chat", "Mine", owner=USER, otherwise={"level": "pass", "redact_with": b})
        combined = await plan(Evaluator(), [mine, admin], facts("chat", api_key_id=str(key)))
        effective = state.effective_policy(combined.policy_uses(), RedactionPolicy())
        assert effective.policy.allow_list == []  # not ["Mario Rossi"]
        assert effective.policy.mode_for("EMAIL_ADDRESS") is EntityMode.REDACT
        assert effective.policy.mode_for("IBAN_CODE") is EntityMode.REDACT

    async def test_picking_an_administrators_policy_does_not_replace_the_environments(self) -> None:
        state, _, b = self.state()
        env = RedactionPolicy(entities={"PERSON": EntityPolicy(mode=EntityMode.REDACT)})
        mine = pipe("chat", "Mine", owner=USER, otherwise={"level": "pass", "redact_with": b})
        combined = await plan(Evaluator(), [mine], facts("chat"))
        effective = state.effective_policy(combined.policy_uses(), env)
        assert effective.policy.mode_for("PERSON") is EntityMode.REDACT  # not off
        assert effective.policy.mode_for("IBAN_CODE") is EntityMode.REDACT
        assert effective.policy.allow_list == []

    async def test_a_picked_default_mode_is_a_floor(self) -> None:
        state = PrivacyState()
        pid = uuid.uuid4()
        state.policies = {
            str(pid): PolicyRef(pid, "All", RedactionPolicy(default_mode=EntityMode.REDACT))
        }
        mine = pipe(
            "chat", "Mine", owner=USER, otherwise={"level": "pass", "redact_with": str(pid)}
        )
        combined = await plan(Evaluator(), [mine], facts("chat"))
        effective = state.effective_policy(combined.policy_uses(), RedactionPolicy())
        assert effective.policy.mode_for("ANYTHING") is EntityMode.REDACT


class TestRerouteHops:
    def pipelines(self, chain: list[str]) -> tuple[list[Pipeline], dict[str, str]]:
        """P0 reads on any target with chain[0]; the pipeline guarding chain[i] says chain[i+1]."""
        providers = {name: str(uuid.uuid4()) for name in chain}
        first = pipe("read", "P0", otherwise={"level": "read_with", "reader": chain[0]})
        out = [first]
        for index, name in enumerate(chain[:-1]):
            out.append(
                pipe(
                    "read",
                    f"Guard {name}",
                    otherwise={"level": "read_with", "reader": chain[index + 1]},
                    target={"mode": "only", "providers": [providers[name]], "models": []},
                    position=index + 1,
                )
            )
        return out, providers

    def retarget_for(self, providers: dict[str, str], impossible: set[str] = frozenset()) -> Any:  # type: ignore[assignment]
        current = {"reader": None}

        async def retarget(reader: str) -> Retarget:
            if reader in impossible:
                return Retarget(None, impossible=f"{reader} is not available")
            if current["reader"] == reader:
                return Retarget(None, same=True)
            current["reader"] = reader
            return Retarget(facts("read", provider_id=providers[reader]))

        return retarget

    async def test_a_pipeline_that_guards_the_current_reader_decides_the_next_hop(self) -> None:
        pipes, providers = self.pipelines(["R", "R2"])
        combined = await plan(
            Evaluator(), pipes, facts("read"), retarget=self.retarget_for(providers)
        )
        assert combined.reader == "R2" and combined.impossible is None
        assert [h.reader for h in combined.hops] == ["R", "R2"]

    async def test_if_that_cannot_be_honoured_it_fails_closed(self) -> None:
        pipes, providers = self.pipelines(["R", "R2"])
        combined = await plan(
            Evaluator(), pipes, facts("read"), retarget=self.retarget_for(providers, {"R2"})
        )
        assert combined.impossible == "R2 is not available"

    async def test_a_users_later_hop_cannot_override_an_administrators_reader(self) -> None:
        pipes, providers = self.pipelines(["R", "R2"])
        # The guard pipeline is a user's: the administrator's earlier choice holds.
        guard = pipes[1]
        user_guard = pipe(
            "read",
            "My guard",
            owner=USER,
            otherwise={"level": "read_with", "reader": "R2"},
            target={"mode": "only", "providers": [providers["R"]], "models": []},
        )
        del guard
        combined = await plan(
            Evaluator(),
            [pipes[0], user_guard],
            facts("read"),
            retarget=self.retarget_for(providers),
        )
        assert combined.reader == "R"

    async def test_running_out_of_hops_is_a_refusal_not_a_quiet_stop(self) -> None:
        chain = ["R1", "R2", "R3", "R4", "R5", "R6"]
        pipes, providers = self.pipelines(chain)
        combined = await plan(
            Evaluator(), pipes, facts("read"), retarget=self.retarget_for(providers)
        )
        assert combined.impossible is not None and "too many reroutes" in combined.impossible

    async def test_a_cycle_of_administrator_reroutes_is_refused(self) -> None:
        providers = {n: str(uuid.uuid4()) for n in ("M", "R1", "R2")}

        def guard(name: str, on: str, to: str, position: int) -> Pipeline:
            return pipe(
                "read",
                name,
                otherwise={"level": "read_with", "reader": to},
                target={"mode": "only", "providers": [providers[on]], "models": []},
                position=position,
            )

        pipes = [
            guard("P1", "M", "R1", 0),
            guard("P2", "R1", "R2", 1),
            guard("P3", "R2", "R1", 2),
        ]
        current = {"reader": None}

        async def retarget(reader: str) -> Retarget:
            if current["reader"] == reader:
                return Retarget(None, same=True)
            current["reader"] = reader
            return Retarget(facts("read", provider_id=providers[reader]))

        combined = await plan(
            Evaluator(), pipes, facts("read", provider_id=providers["M"]), retarget=retarget
        )
        assert combined.impossible is not None and "reroute cycle" in combined.impossible
