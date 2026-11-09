"""Combining the pipelines of one request (ADR 0113, section 0).

**Every pipeline in scope runs, and the results combine.** Inside a pipeline the
first matching rule wins (``evaluate``); across pipelines nothing is first:

* **Where it is handled** is the ladder, and the highest level wins (read:
  default reader < read with < refuse; embed and chat: pass < refuse).
* **What is hidden** adds up: the policies each pipeline chose are folded by
  ``RedactionPolicy.combine`` (the strictest mode per entity type) and a user's
  own policies on top of that. The caller does the fold; this module only says
  *which* policies and *who asked for them*.
* **The reader** is the administrator's choice if any administrator pipeline
  made one, else a user's, else the default. **The engine** likewise.
* **Naming**: the rule that set the winning level, and on a tie the
  administrator's. The trace keeps every pipeline in scope and the rule that
  fired in each.

Because the ladder takes a maximum and policies only add, a user's pipeline can
raise a level or add a policy but can never lower what an administrator's did.

After a reroute (a read rule named another reader), the pipelines whose target
scope covers the new reader also run, so a reroute can never escape a pipeline
that guards that reader. The loop is bounded.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from gateway.errors import ServiceUnavailableError
from gateway.privacy import rules as rulelib
from gateway.privacy.evaluate import Evaluation, Evaluator, Facts, Undecidable
from gateway.privacy.pipelines import Pipeline, not_in_scope
from gateway.privacy.rules import KIND_READ, LEVEL_READ_WITH, LEVEL_REFUSE, Rule

#: How many times a read may be re-pointed at another reader.
MAX_HOPS = 3


@dataclass(slots=True)
class PipelineRun:
    """One pipeline's result for the request: the rule that set its action."""

    pipeline: Pipeline
    evaluation: Evaluation
    #: The rule that fired, or the pipeline's last rule when none did.
    rule: Rule
    is_otherwise: bool
    hop: int = 0
    #: The pipeline waits for a fact found while reading (a scan).
    pending: bool = False

    @property
    def level(self) -> str:
        return self.rule.level

    @property
    def action(self) -> dict[str, object]:
        return dict(self.rule.action)

    def rank(self, kind: str) -> int:
        return rulelib.LADDER[kind].index(self.level)


@dataclass(slots=True)
class Skipped:
    pipeline: Pipeline
    why: str


@dataclass(slots=True)
class PolicyUse:
    """A policy some pipeline chose to apply."""

    policy_id: str
    run: PipelineRun

    @property
    def user(self) -> bool:
        return self.run.pipeline.is_user


@dataclass(slots=True)
class Hop:
    """A reroute: the reader named, and the pipelines that then also ran."""

    reader: str
    added: list[PipelineRun] = field(default_factory=list)


@dataclass(slots=True)
class Combined:
    kind: str
    runs: list[PipelineRun] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    hops: list[Hop] = field(default_factory=list)
    level: str = ""
    #: The run that set the winning level (an administrator's on a tie); None when
    #: nothing asked for more than what happens anyway.
    winner: PipelineRun | None = None
    #: Some pipeline waits for a fact found while reading: read first, plan again.
    pending: bool = False
    pending_run: PipelineRun | None = None
    #: The reader of a "read with" level: ``builtin`` or a model name.
    reader: str | None = None
    reader_run: PipelineRun | None = None
    #: Readers other pipelines asked for and that were not used, with who asked.
    overridden: list[PipelineRun] = field(default_factory=list)
    engine: str | None = None
    engine_run: PipelineRun | None = None
    #: The reroute could not be carried out (a link to a built-in reader, a reader
    #: the caller may not use): fail closed, naming this reason.
    impossible: str | None = None

    @property
    def refused(self) -> bool:
        return self.level == LEVEL_REFUSE

    def in_ladder_order(self) -> list[PipelineRun]:
        """Administrators' pipelines first, then users', each in list order."""
        return sorted(
            self.runs,
            key=lambda r: (r.pipeline.is_user, r.pipeline.position, r.pipeline.order),
        )

    def policy_uses(self) -> list[PolicyUse]:
        """The policies to apply, widest scope first, each named once.

        Not at all for a refused request. Every administrator pipeline's policy
        comes before any user's, then the broadest scope first (the order ADR 0038's
        fold always used), which matters for exactly one field, the allow-list,
        taken from the first policy alone: a user's pipeline must never decide it.
        """
        if self.refused:
            return []
        out: list[PolicyUse] = []
        seen: set[str] = set()
        for run in sorted(
            (r for r in self.runs if not r.pending),
            key=lambda r: (r.pipeline.is_user, r.pipeline.scope_rank, r.pipeline.order),
        ):
            policy = run.rule.action.get("redact_with")
            if policy and str(policy) not in seen:
                seen.add(str(policy))
                out.append(PolicyUse(str(policy), run))
        return out


def run_of(pipeline: Pipeline, evaluation: Evaluation, hop: int = 0) -> PipelineRun:
    if evaluation.pending is not None:
        return PipelineRun(
            pipeline, evaluation, pipeline.otherwise, is_otherwise=True, hop=hop, pending=True
        )
    if evaluation.fired is not None:
        return PipelineRun(pipeline, evaluation, evaluation.fired, False, hop)
    return PipelineRun(pipeline, evaluation, pipeline.otherwise, True, hop)


async def run_in_scope(
    evaluator: Evaluator,
    pipelines: Sequence[Pipeline],
    facts: Facts,
    *,
    seen: set[uuid.UUID],
    hop: int = 0,
) -> tuple[list[PipelineRun], list[Skipped]]:
    """Run each pipeline of the kind that covers *facts* and has not run yet."""
    runs: list[PipelineRun] = []
    skipped: list[Skipped] = []
    for pipeline in pipelines:
        if pipeline.id in seen or pipeline.kind != facts.kind:
            continue
        why = not_in_scope(
            pipeline,
            user_id=facts.user_id,
            group_id=facts.billing_group_id,
            key_id=facts.api_key_id,
            provider_id=facts.provider_id,
            model_id=facts.model_id,
        )
        if why:
            skipped.append(Skipped(pipeline, why))
            continue
        seen.add(pipeline.id)
        try:
            evaluation = await evaluator.evaluate(list(pipeline.rules), facts, pipeline.otherwise)
        except Undecidable as exc:
            raise ServiceUnavailableError(
                f"The privacy rule '{exc.rule.name}' (pipeline '{pipeline.name}') cannot be "
                f"decided now ({exc.why}), so the request is refused rather than let through.",
                code="privacy_undecidable",
            ) from exc
        runs.append(run_of(pipeline, evaluation, hop))
    return runs, skipped


def combine(kind: str, runs: Sequence[PipelineRun]) -> Combined:
    """The level, the winner, the reader and engine, from the runs that have an answer."""
    combined = Combined(kind=kind, runs=list(runs))
    ladder = rulelib.LADDER[kind]
    answered = [r for r in runs if not r.pending]
    pending = [r for r in runs if r.pending]
    if pending:
        combined.pending = True
        combined.pending_run = pending[0]
    best = max((r.rank(kind) for r in answered), default=0)
    combined.level = ladder[best]
    if best > 0:
        tied = [r for r in combined.in_ladder_order() if not r.pending and r.rank(kind) == best]
        combined.winner = tied[0]
    if combined.level == LEVEL_READ_WITH:
        withs = [
            r for r in combined.in_ladder_order() if not r.pending and r.level == LEVEL_READ_WITH
        ]
        # An administrator's choice holds over a user's. Among those that may
        # decide, the run from the latest hop wins: a pipeline that guards the
        # reader the request has been re-pointed at gets the say on where it goes
        # next, so a reroute can never walk past it.
        admin_withs = [r for r in withs if not r.pipeline.is_user]
        deciding = admin_withs or withs
        first = max(deciding, key=lambda r: r.hop) if deciding else withs[0]
        combined.reader = str(first.rule.action.get("reader") or rulelib.BUILTIN_READER)
        combined.reader_run = first
        combined.overridden = [
            r
            for r in withs
            if str(r.rule.action.get("reader") or rulelib.BUILTIN_READER) != combined.reader
        ]
        if combined.reader == rulelib.BUILTIN_READER:
            for run in withs:
                if str(
                    run.rule.action.get("reader") or rulelib.BUILTIN_READER
                ) == rulelib.BUILTIN_READER and run.rule.action.get("engine"):
                    combined.engine = str(run.rule.action["engine"])
                    combined.engine_run = run
                    break
    return combined


@dataclass(slots=True)
class Retarget:
    """Facts for the reader a rule named, or why it cannot be used."""

    facts: Facts | None
    #: The named reader is the one already in charge: no hop.
    same: bool = False
    impossible: str | None = None


RetargetFn = Callable[[str], Awaitable[Retarget]]


async def plan(
    evaluator: Evaluator,
    pipelines: Sequence[Pipeline],
    facts: Facts,
    *,
    retarget: RetargetFn | None = None,
) -> Combined:
    """Run every pipeline in scope and combine; for a read, follow reroutes.

    A "read with" level naming another reader re-points the request at it, and
    the pipelines whose scope covers *that* reader also run (each pipeline runs
    once). Bounded by :data:`MAX_HOPS`; a hop that cannot be made is
    ``Combined.impossible``, which the caller turns into a refusal.
    """
    seen: set[uuid.UUID] = set()
    runs, skipped = await run_in_scope(evaluator, pipelines, facts, seen=seen)
    combined = combine(facts.kind, runs)
    combined.skipped = skipped
    if facts.kind != KIND_READ or retarget is None:
        return combined
    hops: list[Hop] = []
    all_runs = list(runs)
    settled = False
    visited: list[tuple[str, PipelineRun | None]] = []
    for hop in range(1, MAX_HOPS + 1):
        if combined.pending or combined.level != LEVEL_READ_WITH or combined.reader is None:
            settled = True
            break
        if any(name == combined.reader for name, _ in visited):
            # Back to a reader the request already went to: the pipelines that
            # guard it were run with the earlier answer, so walking on would skip them.
            named = ", ".join(f"'{run.rule.name}'" for _, run in visited if run is not None) + (
                f" and '{combined.reader_run.rule.name}'" if combined.reader_run else ""
            )
            combined.impossible = (
                f"the pipelines send the file round in a circle ({named}): reroute cycle."
            )
            settled = True
            break
        moved = await retarget(combined.reader)
        if moved.impossible is not None:
            combined.impossible = moved.impossible
            settled = True
            break
        if moved.same or moved.facts is None:
            settled = True
            break
        facts = moved.facts
        visited.append((combined.reader, combined.reader_run))
        added, more_skipped = await run_in_scope(evaluator, pipelines, facts, seen=seen, hop=hop)
        hops.append(Hop(combined.reader, added))
        skipped = [*skipped, *more_skipped]
        all_runs.extend(added)
        combined = combine(facts.kind, all_runs)
        combined.skipped = skipped
        if not added:
            settled = True
            break
    if (
        not settled
        and combined.impossible is None
        and not combined.pending
        and combined.level == LEVEL_READ_WITH
        and combined.reader is not None
    ):
        # Out of hops with the pipelines still asking for another reader than the
        # one in charge: the request cannot be carried out, so it is refused
        # rather than handled by a reader some pipeline did not choose.
        probe = await retarget(combined.reader)
        if not probe.same:
            combined.impossible = "the pipelines keep re-pointing the file (too many reroutes)."
    if hops and combined.impossible is None and combined.level == LEVEL_READ_WITH:
        # The pipelines that guard the reader the request ended on ran at the last
        # hop; if an administrator's of them asks for another reader, it was not
        # honoured, so the request is refused.
        for run in combined.runs:
            if (
                run.hop == len(hops)
                and not run.pending
                and not run.pipeline.is_user
                and run.level == LEVEL_READ_WITH
                and str(run.rule.action.get("reader") or rulelib.BUILTIN_READER) != combined.reader
            ):
                combined.impossible = (
                    f"'{run.rule.name}' asks for another reader than the one the file ended on."
                )
                break
    combined.hops = hops
    return combined
