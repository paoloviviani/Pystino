"""Combining the pipelines of one request (ADR 0114, superseding ADR 0113 section 0).

**Every rule of every pipeline in scope is evaluated; there is no first match.** The
rules are taken in one order: administrator pipelines in console order (``position``),
then the user's own, each pipeline's rules top to bottom. Then:

* **Block first.** If any matching rule blocks (a content rule in mode ``block``, a
  reading rule whose action is block), the request is refused and the first such rule is
  named. (A type an override sets to ``block`` is found while hiding: ``redaction.http``.)
* **Hiding** is applied in that order, the first rule to hide a span deciding how
  (``redaction.http.claim_spans``). This module only says *which* rules matched.
* **The reader** (reading): the first matching "read with" rule in the order above, an
  administrator's before a user's. A pipeline that guards a reader a request was
  re-pointed at gets the say on the next hop.

Because administrators come first, a user's pipeline can add protection or block, and can
never loosen what an administrator's did.

After a reroute (a read rule named another reader), the pipelines whose target scope covers
the new reader also run, so a reroute can never escape a pipeline that guards that reader.
The loop is bounded.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from gateway.errors import ServiceUnavailableError
from gateway.privacy import rules as rulelib
from gateway.privacy.evaluate import Evaluation, Evaluator, Facts, Undecidable
from gateway.privacy.pipelines import Pipeline, not_in_scope
from gateway.privacy.rules import KIND_EXTRACT, LEVEL_BLOCK, LEVEL_EXTRACT_WITH, Rule

#: How many times a read may be re-pointed at another reader.
MAX_HOPS = 3


@dataclass(slots=True)
class PipelineRun:
    """One pipeline's result for the request: every rule judged."""

    pipeline: Pipeline
    evaluation: Evaluation
    hop: int = 0

    @property
    def pending(self) -> bool:
        """Some rule waits for a fact found while reading (a scan)."""
        return self.evaluation.pending


@dataclass(frozen=True, slots=True)
class Hit:
    """A rule that matched, and the pipeline it belongs to."""

    pipeline: Pipeline
    rule: Rule
    hop: int = 0

    @property
    def user(self) -> bool:
        return self.pipeline.is_user


@dataclass(slots=True)
class Skipped:
    pipeline: Pipeline
    why: str


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
    #: The rule that decided where the request goes (the first blocking rule, else the
    #: rule whose reader was chosen); None when nothing asked for more than what happens anyway.
    winner: Hit | None = None
    #: Some pipeline waits for a fact found while reading: read first, plan again.
    pending: bool = False
    pending_run: Hit | None = None
    #: The reader of a "read with" level: an OCR model's name.
    reader: str | None = None
    reader_run: Hit | None = None
    #: Readers other rules asked for and that were not used.
    overridden: list[Hit] = field(default_factory=list)
    #: The reroute could not be carried out (a link to a built-in reader, a reader
    #: the caller may not use): fail closed, naming this reason.
    impossible: str | None = None

    @property
    def refused(self) -> bool:
        return self.level == LEVEL_BLOCK

    def in_order(self) -> list[PipelineRun]:
        """Administrators' pipelines first (console order), then users', each in list order."""
        return sorted(
            self.runs,
            key=lambda r: (r.pipeline.is_user, r.pipeline.position, r.pipeline.order),
        )

    def hits(self) -> list[Hit]:
        """Every matching rule, in the order the rules apply."""
        return [
            Hit(run.pipeline, rule, run.hop)
            for run in self.in_order()
            if not run.pending or run.evaluation.matched
            for rule in run.evaluation.matched
        ]

    def hiding_hits(self) -> list[Hit]:
        """The matching rules that hide, or record what they find, in order.

        Not at all for a refused request.
        """
        if self.refused:
            return []
        return [
            h for h in self.hits() if h.rule.policies and (h.rule.hides_text or h.rule.records_only)
        ]

    def policy_ids(self) -> list[str]:
        """The policies the hiding rules apply, each once, in order."""
        out: list[str] = []
        for hit in self.hiding_hits():
            out.extend(p for p in hit.rule.policies if p not in out)
        return out


def combine(kind: str, runs: Sequence[PipelineRun]) -> Combined:
    """Where the request goes, and by which rule, from the runs that have an answer."""
    combined = Combined(kind=kind, runs=list(runs))
    hits = combined.hits()
    blockers = [h for h in hits if h.rule.level == LEVEL_BLOCK]
    combined.level = rulelib.base_level(kind)
    if blockers:
        combined.level, combined.winner = LEVEL_BLOCK, blockers[0]
        return combined
    pending = [r for r in combined.in_order() if r.pending and r.evaluation.pending_rule]
    if pending:
        combined.pending = True
        rule = pending[0].evaluation.pending_rule
        assert rule is not None
        combined.pending_run = Hit(pending[0].pipeline, rule, pending[0].hop)
    withs = [h for h in hits if h.rule.level == LEVEL_EXTRACT_WITH]
    if kind == KIND_EXTRACT and withs:
        combined.level = LEVEL_EXTRACT_WITH
        # An administrator's choice holds over a user's. Among those that may decide, the
        # run from the latest hop wins: a pipeline that guards the reader the request has
        # been re-pointed at gets the say on where it goes next, so a reroute can never
        # walk past it. Within a hop the first rule in order wins.
        admin_withs = [h for h in withs if not h.user]
        deciding = admin_withs or withs
        latest = max(h.hop for h in deciding)
        first = next(h for h in deciding if h.hop == latest)
        combined.reader = str(first.rule.action.get("reader") or rulelib.DEFAULT_READER)
        combined.reader_run = first
        combined.winner = first
        combined.overridden = [
            h
            for h in withs
            if str(h.rule.action.get("reader") or rulelib.DEFAULT_READER) != combined.reader
        ]
    return combined


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
            model_type=facts.model_type,
        )
        if why:
            skipped.append(Skipped(pipeline, why))
            continue
        seen.add(pipeline.id)
        try:
            evaluation = await evaluator.evaluate(list(pipeline.rules), facts)
        except Undecidable as exc:
            raise ServiceUnavailableError(
                f"The privacy rule '{exc.rule.name}' (pipeline '{pipeline.name}') cannot be "
                f"decided now ({exc.why}), so the request is refused rather than let through.",
                code="privacy_undecidable",
            ) from exc
        runs.append(PipelineRun(pipeline, evaluation, hop))
    return runs, skipped


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
    if facts.kind != KIND_EXTRACT or retarget is None:
        return combined
    hops: list[Hop] = []
    all_runs = list(runs)
    settled = False
    visited: list[tuple[str, Hit | None]] = []
    for hop in range(1, MAX_HOPS + 1):
        if combined.pending or combined.level != LEVEL_EXTRACT_WITH or combined.reader is None:
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
        and combined.level == LEVEL_EXTRACT_WITH
        and combined.reader is not None
    ):
        # Out of hops with the pipelines still asking for another reader than the
        # one in charge: the request cannot be carried out, so it is refused
        # rather than handled by a reader some pipeline did not choose.
        probe = await retarget(combined.reader)
        if not probe.same:
            combined.impossible = "the pipelines keep re-pointing the file (too many reroutes)."
    if hops and combined.impossible is None and combined.level == LEVEL_EXTRACT_WITH:
        # The pipelines that guard the reader the request ended on ran at the last
        # hop; if an administrator's of them asks for another reader, it was not
        # honoured, so the request is refused.
        for hit in combined.hits():
            if (
                hit.hop == len(hops)
                and not hit.user
                and hit.rule.level == LEVEL_EXTRACT_WITH
                and str(hit.rule.action.get("reader") or rulelib.DEFAULT_READER) != combined.reader
            ):
                combined.impossible = (
                    f"'{hit.rule.name}' asks for another reader than the one the file ended on."
                )
                break
    combined.hops = hops
    return combined
