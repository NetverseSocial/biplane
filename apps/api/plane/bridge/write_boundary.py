# Copyright (c) 2026 The Biplane Authors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.
"""What must be true before the bridge writes a board row (BIP-67).

Scope A, *Who may move a ticket* (John's ruling, 2026-08-14): the bridge is not
a participant. It holds no role and has no authority of its own. **It may write
only where facts it verified for itself determine the outcome**, and anywhere
else it writes nothing and hands the work back to a person.

THIS MODULE IS THE DECISION, NOT THE DELIVERY. It answers "may this write
happen, and if not, why" and returns a :class:`Refusal` carrying a reason a
human can act on.

WHO ACTUALLY SAYS IT, TODAY: the pull-request reply, and only where a pull
request exists. There is NO notification caller in this release — that slice was
cut, so a refusal on a push is recorded durably and reaches no person at all.
The ``Refusal`` is the boundary object for whatever eventually delivers it;
that slice's mechanism is deliberately not described here — pre-answered
mechanism for absent code is the same false present tense this module exists
to keep out of the write path.

WHY EVERY CALL REFUSES TODAY, AND WHY THAT IS NOT A SWITCH
-----------------------------------------------------------------
There is deliberately **no flag** here. Scope A's own configuration invariant
prefers absent code to a mode flag, for the reason this repository has already
paid for once: a guarantee that rests on a boolean is one edit from being
false while every document still says it is true. The release notes said
"every bridge write ships off" while the source wrote freely, because that
sentence was read from the architecture and not from the code.

So each refusal below is derived from a **named fact that is missing or a rule
that was withdrawn**, never from a setting:

* ``BINDING_UNAVAILABLE`` — Scope A requires that *the ticket names the pull
  request*, and that is the match, not a convenience. No field records it yet,
  so the condition cannot be evaluated; an unevaluable condition is a refusal
  by the rule's own terms.

  **NARROWED CLAIM (Morrow cold read).** An earlier version of this paragraph
  said changing :func:`_ticket_names` alone would suffice when the field lands.
  That is FALSE as the callers stand: `_advance` passes ``pull_number=None``,
  so the boundary is not even told which pull request this is. Making the
  binding evaluable requires BOTH the field AND the structured pull-request
  number reaching this function — carried from the payload, never re-parsed
  out of the human-readable ``context`` string. Recorded rather than left for
  future code to discover by parsing prose.
* ``ADVANCE_NOT_AUTHORISED`` — no rule authorises a board write on a push. A
  push is neither a merge nor an approval, so nothing it carries can determine
  an outcome. Scope A ships **every board/state write** off, completion and
  advance alike; this is the advance half of that and it is not waiting on a
  fact. (Board/state, not "every bridge write" — the pull-request reply is a
  bridge write and it is exactly what stays on.)
* ``REWORK_SUPERSEDED`` — ADR 009's automatic Review → Code & TDD write on a
  changes-requested review is **removed outright, not deferred**: a
  changes-requested review can never satisfy the conditions for a write, so no
  waiting makes it qualify. That is exactly the case where the bridge asks.
"""

from dataclasses import dataclass

#: The ticket does not name the pull request, so nothing binds this event to it.
BINDING_UNAVAILABLE = "ticket-artifact-binding-unavailable"
#: A push determines no outcome; no rule authorises a write from one.
ADVANCE_NOT_AUTHORISED = "advance-not-authorised"
#: ADR 009's automatic rework write was withdrawn, not postponed.
REWORK_SUPERSEDED = "rework-write-superseded"
#: The project is in scope but no ticket with that sequence exists.
TICKET_NOT_FOUND = "ticket-not-found"
#: The event named no ticket at all — an EVENT-level fact, not a per-ticket one.
NO_TICKET_NOMINATED = "no-ticket-nominated"


@dataclass(frozen=True)
class Refusal:
    """Why a board write did not happen, in terms a person can act on.

    ``ticket`` is the key the event named; ``reason`` is a stable code for
    machines; ``detail`` is the sentence a human is shown. Both are carried into
    the durable delivery result so the record survives the process.

    **RECIPIENTS ARE NOT HERE (Morrow).** An earlier version carried a ``notify``
    tuple resolved from the ticket. The notification slice was cut from this
    release, so that resolution had zero runtime consumers while still costing a
    query on every refusal and a soft-delete policy to maintain. Recipient
    resolution BELONGS TO the future delivery slice and is not designed here.
    HOW it resolves recipients is that slice's problem: pre-answering the
    mechanism for code that does not exist is the defect class this module was
    built to keep out.
    """

    ticket: str
    reason: str
    detail: str


def decide_completion(*, ticket: str, repo: str, context: str):
    """May the bridge complete ``ticket``? No, and there is nothing to evaluate.

    **THIS IS UNCONDITIONAL AND THE SIGNATURE SAYS SO (Morrow).** An earlier
    version modelled all three future facts — the ticket↔artifact binding, the
    approving reviews, the verified merger — and advertised a ``None`` return
    for the authorised path. None of them can be supplied in this release and no
    mutation exists to reach, so that was a partial second implementation of the
    next slice: exactly the shape that left the retired review authority sitting
    beside the new rule.

    So it returns the one factually true refusal, and the evaluated decision
    arrives only when its fields, forge reads, identity join and write caller
    land together. The requirements stay normative in Scope A, which is where a
    requirement with no code belongs.
    """
    return Refusal(
        ticket=ticket,
        reason=BINDING_UNAVAILABLE,
        detail=(
            f"{ticket} was named on a merge, but nothing binds it to the work that "
            f"merged ({context} in {repo}). A directive in a body or commit message "
            "selects a ticket; it does not authorise a change to it."
        ),
    )


#: The CLOSED set of event kinds this boundary renders for. DELIBERATELY NOT
#: the endpoint's HANDLED_EVENTS: those are wire event names, and the boundary
#: cares about a different distinction — a merged pull request is `merged_pr`
#: here, because what makes the sentence true is that the work MERGED, not
#: which webhook carried it.
BOUNDARY_EVENTS = ("push", "merged_pr")


def decide_advance(*, ticket: str, context: str, repo: str, event: str):
    """May the bridge advance ``ticket``? No — permanently, on every event.

    ``event`` is passed EXPLICITLY, never inferred from the keyword class
    (Morrow: proposed effect != event kind). A merged PR carrying `Refs` is an
    advance-class proposal on a MERGE — telling its author "a push determines
    nothing" in a public comment is factually false about their event. The
    refusal is the same permanent one; the sentence must be true of the event
    it answers.
    """
    if event not in BOUNDARY_EVENTS:
        # NO DEFAULT, AND NO SILENT ELSE (Morrow). `event` used to default to
        # "push" and every unrecognised value fell into the push branch — so a
        # caller that forgot the argument, or passed a wire event name, got a
        # PUBLIC COMMENT telling the author "a push determines nothing" about an
        # event that was not a push. A false sentence, rendered confidently, on
        # the exact axis this boundary exists to be truthful about.
        raise ValueError(
            f"event {event!r} is not one of {BOUNDARY_EVENTS}; the refusal sentence "
            "must be true of the event it answers, so there is nothing safe to fall "
            "back to"
        )
    if event == "merged_pr":
        detail = (
            f"The pull request did merge, but `Refs` proposes an ADVANCE and no rule "
            f"authorises the bridge to advance {ticket} on any event ({context} in {repo}). "
            "Only a completion determined by verified facts will ever move a ticket from "
            "a merge; someone holding a role moves it meanwhile."
        )
    else:
        detail = (
            f"A push determines nothing about {ticket}: it is neither a merge nor an "
            f"approval, so no verified fact decides the outcome ({context} in {repo}). "
            "Someone holding a role on this ticket moves it in whichever direction the "
            "work actually went."
        )
    return Refusal(ticket=ticket, reason=ADVANCE_NOT_AUTHORISED, detail=detail)


def decide_rework(*, ticket: str, repo: str, pull_number):
    """May a changes-requested review send ``ticket`` back? No — withdrawn."""
    return Refusal(
        ticket=ticket,
        reason=REWORK_SUPERSEDED,
        detail=(
            f"A review requesting changes on #{pull_number} in {repo} is neither a merge "
            f"nor an approval, so it cannot authorise moving {ticket}. This is exactly "
            "the case where the bridge asks: the reviewer or the ticket's owner moves it."
        ),
    )


def decide_missing_ticket(*, ticket: str, repo: str, context: str):
    """The project is in scope but the ticket does not exist."""
    return Refusal(
        ticket=ticket,
        reason=TICKET_NOT_FOUND,
        detail=(
            f"{ticket} does not exist in this project, so nothing was moved "
            f"({context} in {repo}). Check the number — a directive that names "
            "no ticket cannot be acted on."
        ),
    )


def describe_no_nomination(*, repo: str, context: str) -> dict:
    """The event named no ticket at all.

    Scope A lists *"a missing or unmatched ticket"* as an ask-case, and zero
    nomination is its limiting form: a merged pull request or a changes-requested
    review carrying no directive produced no boundary call, no record, and so
    nothing for either half of the asking to say (Morrow cold read).

    **This deliberately is NOT a :class:`Refusal`.** A Refusal is per-ticket
    and there is no ticket here. Forcing it into that shape would
    mean inventing a ticket key, and an invented identifier in a durable record
    is worse than an honest absence.
    It is an EVENT-level diagnostic and the shape says so.

    The board notification stays absent for the same reason. The actor is
    present on the pull request, though, so the reply has somewhere to go — and
    "this merged, and it named no ticket" is exactly what that person needs.
    """
    return {
        "reason": NO_TICKET_NOMINATED,
        "detail": (
            f"No ticket was named ({context} in {repo}). Nothing was moved, and "
            "nothing could be: a directive has to name a ticket for the bridge "
            "to have anything to check."
        ),
    }
