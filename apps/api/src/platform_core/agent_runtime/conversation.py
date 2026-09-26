"""Multi-turn conversation state, context compression and query rewriting.

Why this module exists
----------------------
`docs/agent.md` specifies seven context layers, in priority order:

    1. system safety / tenant policy      2. actor permissions + account
    3. current case and SLA state         4. **recent relevant conversation turns**
    5. authorized retrieved evidence      6. read-only tool results
    7. **older summary memory**

Layers 4 and 7 were never built. Every run received one bare `question: str`,
so the platform was stateless: "How about monthly?" had no antecedent, "What
about enterprise?" retrieved against a query with no topic, and a commitment
made three turns ago was invisible to the answer being written now. In a
support conversation that is the common case, not the edge case — a customer
who says everything in one message is rare.

It also cost money and latency in the other direction: without compression the
only way to add context is to append every prior turn, which is exactly what
`docs/agent.md` forbids ("Do not copy the full conversation into every model
request").

What "memory" means here, and what it deliberately does not
----------------------------------------------------------
There is no vector store and no LLM call in this module. Everything is
deterministic and inspectable, for three reasons:

- **A summary that cannot be reproduced is not auditable.** Every run records
  which turns it kept and which it summarised, so "why did the model see this"
  has an answer that is not a guess. A model-written summary of a support
  conversation is evidence the platform cannot re-derive.
- **Compression must not drop the things that carry obligation.** The doc is
  explicit: never summarise away unresolved commitments, tool outcomes,
  approvals or ownership changes. Those are matched by rule and **pinned** —
  carried verbatim regardless of budget — rather than being left to a
  summariser's judgement about what was salient.
- **Retrieval needs the antecedent, not a paraphrase.** `rewrite_query` exists
  because anaphora ("how about monthly?") is a *retrieval* problem: the vector
  and FTS queries both run against the surface string. A paraphrase would help
  a reader and still miss the index.

Long-term memory: what is kept
------------------------------
`DurableFact` captures what the customer *stated about their own situation*
(plan, product, order or case reference, region). It deliberately does not
capture credentials, free-text PII or anything the customer merely asked about
— see the rejection list in `_NEVER_DURABLE`. A memory that stores a password
because the customer typed one is a liability, not a feature.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from platform_core.agent_runtime.qa_path import (
    _INTERROGATIVE_OPENERS,
    STOPWORDS,
    _content_terms,
    _stem,
)

# --- Budgets ---------------------------------------------------------------
#
# Character budgets, not token budgets: the platform has no tokenizer and a
# wrong token estimate is worse than a slightly conservative character one,
# because it fails by truncating evidence instead of by summarizing.

# How much conversation text may cross the model boundary in one run. Sized
# against `generator.MAX_TOTAL_EVIDENCE_CHARS` (6000): context and evidence
# share one prompt, so giving context the whole budget would starve evidence.
DEFAULT_CONTEXT_BUDGET_CHARS = 1500

# Turns kept verbatim before older ones are summarized. The recent tail is what
# a multi-turn question actually refers to; summarizing it is how anaphora
# breaks.
DEFAULT_RECENT_TURNS = 6

# Hard cap on stored turns. Without it a long-running conversation grows
# without bound and every compaction walks the whole history.
MAX_STORED_TURNS = 50


class TurnRole(StrEnum):
    """Who produced a turn.

    `TOOL` is distinct from `AGENT` because a tool outcome is evidence about
    the world, not something the assistant said — and the doc requires tool
    outcomes to survive compression.
    """

    CUSTOMER = "customer"
    AGENT = "agent"
    SYSTEM = "system"
    TOOL = "tool"


@dataclass(frozen=True)
class Turn:
    """One message in a conversation.

    `text` is expected to already be minimized/redacted by the caller: this
    module stores what it is given and never fetches raw message bodies.
    """

    role: TurnRole
    text: str
    ts: int = 0
    # Optional correlation so a tool outcome can be tied back to the run that
    # produced it. Not used for routing.
    ref: str = ""

    def __post_init__(self) -> None:
        if isinstance(self.role, str) and not isinstance(self.role, TurnRole):
            # Enum columns elsewhere in the platform hold plain strings; accept
            # them here rather than raising on a caller that read a row back.
            object.__setattr__(self, "role", TurnRole(self.role))


# --- Pinning: the lines that must survive compression ----------------------
#
# `docs/agent.md`: "Never summarize away unresolved commitments, tool outcomes,
# approvals or ownership changes." A summariser optimises for topical
# salience, and an unresolved commitment is often the *least* topical line in a
# long conversation — which is precisely when it matters most.

_COMMITMENT = re.compile(
    r"\b(?:i|we)\s+(?:will|shall|am going to|are going to|promise|have scheduled)\b",
    re.IGNORECASE,
)
_APPROVAL = re.compile(
    r"\b(?:approved?|authori[sz]ed|confirmed?|signed off|granted)\b", re.IGNORECASE
)
_OWNERSHIP = re.compile(
    r"\b(?:assigned?|reassigned?|hand(?:ed|ing) (?:off|over)|taking over|"
    r"escalated? (?:to|this)|transferred? to)\b",
    re.IGNORECASE,
)
_UNRESOLVED = re.compile(
    r"\b(?:still (?:waiting|open|pending|not)|no (?:response|reply) yet|"
    r"has not been|hasn't been|outstanding|follow(?:ing)? up)\b",
    re.IGNORECASE,
)
# Tool outcomes are recognised by role as well as by text, so a bare "ok" from
# a TOOL turn is still pinned.
_TOOL_OUTCOME = re.compile(
    r"\b(?:succeeded|failed|verified|unknown|timed out|returned|created|updated)\b",
    re.IGNORECASE,
)

_PIN_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("commitment", _COMMITMENT),
    ("approval", _APPROVAL),
    ("ownership", _OWNERSHIP),
    ("unresolved", _UNRESOLVED),
    ("tool_outcome", _TOOL_OUTCOME),
)


def pin_reason(turn: Turn) -> str:
    """Why a turn must be pinned, or "" when it may be summarized.

    Returning a *reason* rather than a bool is what makes the pin auditable: a
    run's context can say "three lines were pinned, two of them unresolved
    commitments" instead of "some compression happened".
    """
    if turn.role is TurnRole.TOOL:
        return "tool_outcome"
    for reason, pattern in _PIN_RULES:
        if pattern.search(turn.text):
            return reason
    return ""


# --- Durable facts (long-term memory) --------------------------------------

# What the customer stated about their own situation, worth carrying across
# turns. Matched as `label: value` or `label is/are value`.
_DURABLE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "plan",
        re.compile(
            r"\b(?:my|our)\s+plan\s+is\s+([a-z0-9][a-z0-9 _-]{1,30})"
            r"|\b(?:on|under)\s+(?:the\s+)?(annual|monthly|quarterly|yearly)\s+plan\b",
            re.IGNORECASE,
        ),
    ),
    (
        "product",
        re.compile(
            r"\b(?:using|running|on)\s+(?:the\s+)?([a-z][a-z0-9 _-]{2,30}?)\s+"
            r"(?:version|edition|appliance|gateway|agent)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "region",
        re.compile(r"\b(?:in|for)\s+the\s+(emea|apac|amer|us|eu|uk|cn)\s+region\b", re.IGNORECASE),
    ),
    (
        "case_ref",
        re.compile(r"\bcase\s*#?\s*([0-9]{3,12})\b", re.IGNORECASE),
    ),
    (
        "order_ref",
        re.compile(r"\border\s*#?\s*([A-Z0-9-]{4,20})\b", re.IGNORECASE),
    ),
)

# Never persisted, whatever the customer typed. Storing a credential because it
# appeared in a sentence is the failure this list exists to prevent; the
# Tool Gateway already has `SENSITIVE_FIELD_NAMES` for the write path and this
# is its read-path counterpart.
_NEVER_DURABLE = re.compile(
    r"\b(?:password|passphrase|secret|token|api[ _-]?key|credential|"
    r"social security|ssn|credit card|card number|bank account|iban)\b",
    re.IGNORECASE,
)

# PII-shaped free text: a fact with a *label* ("my plan is annual") is
# storable; a sentence containing an email address or phone number is not.
_PII_SHAPED = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+\d[\d\s()-]{7,}|\b\d{3}-\d{2}-\d{4}\b")


@dataclass(frozen=True)
class DurableFact:
    """One thing the customer said about their own situation.

    `source_turn` is the index in the stored turn list, so a fact can be traced
    back to the sentence that produced it. A memory you cannot attribute is a
    rumour, and the platform already has one append-only record of what was
    said (the audit log) — this must never contradict it.
    """

    key: str
    value: str
    source_turn: int
    ts: int = 0


def extract_durable_facts(turns: list[Turn]) -> tuple[DurableFact, ...]:
    """Facts worth carrying across turns, from the customer's own statements.

    Only `CUSTOMER` turns are read. An assistant saying "your plan is annual"
    is a claim the platform has not verified, and treating it as memory would
    let the model's own output become its input on the next turn — the feedback
    loop `AGENTS.md` forbids ("Automatically learning from unreviewed
    conversations").

    A later statement wins over an earlier one for the same key: the customer
    correcting themselves is the normal case, and "user history conflicts with
    current expression" resolves in favour of the current expression.
    """
    found: dict[str, DurableFact] = {}
    for index, turn in enumerate(turns):
        if turn.role is not TurnRole.CUSTOMER:
            continue
        if _NEVER_DURABLE.search(turn.text) or _PII_SHAPED.search(turn.text):
            # Skip the whole turn. A sentence that mixes "my plan is annual"
            # with a card number is not worth a partial parse.
            continue
        for key, pattern in _DURABLE_PATTERNS:
            match = pattern.search(turn.text)
            if match is None:
                continue
            value = next((g for g in match.groups() if g), "")
            value = value.strip().lower()
            if not value:
                continue
            found[key] = DurableFact(key=key, value=value, source_turn=index, ts=turn.ts)
    return tuple(found[key] for key in sorted(found))


# --- Topic shift -----------------------------------------------------------

# How much of the current utterance must be new for it to count as a topic
# shift. Relative, not absolute: an absolute count would fire on every short
# follow-up ("ok thanks"), which is not a shift.
TOPIC_SHIFT_NEW_FRACTION = 0.8
# Below this many content terms there is no topic to have shifted from; the
# utterance is a follow-up or an acknowledgement.
TOPIC_SHIFT_MIN_TERMS = 2


def topic_shifted(prior: list[Turn], question: str) -> bool:
    """True when the current question is about something new.

    Detected because it changes what should be retrieved: on a shift, the
    previous topic's terms must not be carried into the query, or "how about
    shipping?" after a refund conversation retrieves refund passages. Without
    the check, `rewrite_query` would happily glue the old subject onto a new
    question and the platform would answer a question nobody asked.

    Only `CUSTOMER` turns count as prior topic: an assistant's restatement is
    not evidence of what the conversation is about.
    """
    current = _content_terms(question)
    if len(current) < TOPIC_SHIFT_MIN_TERMS:
        return False
    prior_terms: set[str] = set()
    for turn in prior:
        if turn.role is TurnRole.CUSTOMER:
            prior_terms |= _content_terms(turn.text)
    if not prior_terms:
        return False
    new_terms = current - prior_terms
    return len(new_terms) / len(current) >= TOPIC_SHIFT_NEW_FRACTION


# --- Query rewriting -------------------------------------------------------

# Words that look like content but carry no subject. "how about it" must not
# be treated as having a topic.
_REFERENCE_ONLY = frozenset(
    _stem(word)
    for word in (
        "it",
        "that",
        "this",
        "they",
        "them",
        "those",
        "these",
        "one",
        "same",
        "there",
    )
)
MAX_REWRITE_TERMS = 6

# An elliptical fragment: how many tokens and content terms it may have and
# still count as a continuation rather than a standalone question.
#
# "monthly?" and "the enterprise one?" are the shapes this is for — a noun
# phrase typed into a chat box, which is the most common real follow-up. The
# token bound is what separates them from a real short sentence: "Refund the
# last invoice" has four content terms and six tokens, so it is an instruction,
# not a fragment.
MAX_FRAGMENT_TOKENS = 5
MAX_FRAGMENT_TERMS = 2


def is_continuation(question: str) -> bool:
    """True when the question cannot stand alone.

    Two shapes qualify, and both are the ones that break retrieval:

    - **Anaphora**: every content term is a pronoun-like reference ("how about
      it?", "and that?"). There is nothing for the index to match.
    - **Ellipsis**: a short fragment with no verb ("monthly?", "the enterprise
      one?", "and for the enterprise plan?"). This is a noun phrase typed into
      a chat box, and it is the most common real follow-up.

    An interrogative opener disqualifies both: "How do I cancel?" opens with
    `how` and is a complete question, and rewriting it against the previous
    subject would change what was asked.
    """
    terms = _content_terms(question)
    if not terms:
        return True
    if terms <= _REFERENCE_ONLY:
        return True
    tokens = re.findall(r"[a-z']+", question.lower())
    if not tokens or tokens[0] in _INTERROGATIVE_OPENERS:
        return False
    return len(terms) <= MAX_FRAGMENT_TERMS and len(tokens) <= MAX_FRAGMENT_TOKENS


def rewrite_query(question: str, prior: list[Turn]) -> tuple[str, bool]:
    """Resolve an anaphoric or elliptical question against prior turns.

    Returns `(query, rewritten)`. The rewrite is **additive and lexical**: the
    missing subject terms are prepended and the customer's own words are kept
    untouched at the end. Two properties follow, and both are deliberate:

    - Retrieval still sees the customer's surface string, so nothing that
      would have matched before stops matching. A rewrite that replaced the
      question could lose a term the customer actually needed matched.
    - The result is inspectable. An LLM paraphrase would be more fluent and
      impossible to audit; when a rewritten query retrieves badly, the first
      question is "what did we search for", and this answers it.

    Refuses when the topic has shifted: carrying the old subject into a new
    topic is the failure mode that makes a rewrite worse than no rewrite.

    `MAX_REWRITE_TERMS` bounds the prefix because the point is to restore a
    missing subject, not to build a dossier — a dozen inherited terms would
    dominate a short follow-up and retrieve the previous question's evidence.

    Entity protection (plan 1.4): model numbers, fault codes and order
    references ("EC-504", "X200") travel through the prefix **verbatim**.
    `_content_terms` stems them like any word — "X200s" loses its trailing
    "s" and stops matching the index — so entity-shaped tokens are extracted
    separately, unstemmed, and placed ahead of the subject terms. Rewriting
    away the one exact identifier the index can still match is how a rewrite
    makes recall worse than the raw question.
    """
    if not is_continuation(question):
        return question, False
    if topic_shifted(prior, question):
        return question, False

    subject: list[str] = []
    entities: list[str] = []
    for turn in reversed(prior):
        if turn.role is not TurnRole.CUSTOMER:
            continue
        # Nearest prior customer turn first: the antecedent is almost always
        # the immediately preceding question, not the oldest one.
        terms = _content_terms(turn.text) - _REFERENCE_ONLY
        if terms:
            subject.extend(sorted(terms))
        turn_entities = _entity_terms(turn.text)
        for entity in turn_entities:
            if entity not in entities:
                entities.append(entity)
        if len(subject) >= MAX_REWRITE_TERMS and entities:
            break
    if not subject and not entities:
        return question, False
    prefix_parts = (
        entities[:MAX_REWRITE_ENTITIES] + sorted(set(subject) - set(entities))[:MAX_REWRITE_TERMS]
    )
    prefix = " ".join(prefix_parts)
    if not prefix.strip():
        return question, False
    return f"{prefix} {question.strip()}", True


# Entity-shaped tokens: anything carrying a digit or an internal hyphen is an
# identifier, not prose. Stemming these destroys them.
_ENTITY_TOKEN = re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+)+\b|\b[a-z]*\d[a-z0-9]*\b", re.IGNORECASE)
MAX_REWRITE_ENTITIES = 4


def _entity_terms(text: str) -> list[str]:
    """Verbatim identifier tokens from a turn, newest-friendly order."""
    found: list[str] = []
    for match in _ENTITY_TOKEN.finditer(text):
        token = match.group(0)
        if token.lower() in STOPWORDS or len(token) < 3:
            continue
        if token not in found:
            found.append(token)
    return found


# --- Colloquial normalization (plan 1.4, shared with the alias table) ------

_CJK_CHAR = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def normalize_colloquial(
    query: str, aliases: list[tuple[str, str, float]]
) -> tuple[str, list[str]]:
    """Replace tenant-known colloquial surface forms with canonical terms.

    One data source with retrieval's alias table (knowledge_aliases): two
    vocabularies drift, and then the query is normalized into words the
    index has never seen. Replacement is additive-safe — the canonical term
    is appended next to the surface form rather than replacing it, so a
    wrong mapping costs ranking precision, never recall:

        "the wifi thing keeps dropping" -> "... dropping gateway wifi"

    Only whole-token aliases are replaced (a substring "ec" inside
    "ec-504" must not fire); CJK aliases may match as substrings, which is
    the correct granularity for them.
    """
    lowered = query.lower()
    applied: list[str] = []
    result = query
    for alias, term, _weight in aliases:
        if not alias or alias == term:
            continue
        # The query is case-folded, so the alias must be too. Comparing the
        # raw alias against a lowered query meant EVERY alias carrying an
        # uppercase ASCII letter matched nothing - "V割" against "v割 ...",
        # "EQ", "MI", "TGZ" - which is the entire abbreviation vocabulary of
        # a PCB corpus. A dead alias is worse than a missing one: the row
        # exists, the tenant looks configured, and no query is ever rewritten.
        folded = alias.lower()
        is_cjk = _CJK_CHAR.search(alias) is not None
        present = (
            folded in lowered
            if is_cjk
            else re.search(rf"(?<![\w-]){re.escape(folded)}(?![\w-])", lowered) is not None
        )
        if not present or term in lowered:
            continue
        result = f"{result} {term}"
        applied.append(term)
    return result, applied


# --- Clarification ---------------------------------------------------------

# A question with no content terms is not a query: "it?" has nothing to
# retrieve against, whatever the evidence says.
# And a question this short is a nudge rather than a question ("?", "hi",
# "hello?"). Answering it from the corpus is how a platform comes to sound
# certain about nothing.
#
# Two numbers, because one number cannot mean the same thing in two scripts.
# 12 Latin characters is about two words; 12 Chinese characters is a whole
# sentence, so a threshold calibrated on English rejected the questions a
# Chinese customer actually writes - 怎么退款 (4), 交期是多久 (5),
# 发票怎么开 (5) - as "too short" and answered them with an English
# clarification prompt. Measured 2026-09-23 on the customer surface; see
# `language.answers_in_chinese` for why the script is the right axis.
#
# 3 CJK characters, not the 4 that would be the arithmetic counterpart of 12
# Latin ones. Two reasons, and the second is the one that decided it:
#
# 1. A CJK character carries roughly two to three Latin characters of meaning,
#    so 4-6 would be the like-for-like translation of 12. Any of those numbers
#    is defensible on paper.
# 2. This gate is not what stops the platform inventing an answer - the
#    abstention gate downstream is (`decide_abstention` will not answer
#    without evidence). What this gate costs is a round trip: a customer asked
#    to repeat themselves, which the docstring below already calls *the*
#    expensive failure here. So the floor sits at the lowest value that still
#    excludes a bare greeting, which is 3: "在吗" (2) is blocked, while a
#    terse but complete "SO-9001 到哪了" (3) reaches the order lookup it names.
MIN_ANSWERABLE_LATIN_CHARS = 12
MIN_ANSWERABLE_CJK_CHARS = 3


def _is_too_short_to_answer(text: str) -> bool:
    """Whether the message carries too little to ask about, in either script.

    Passes as soon as *either* count is met, so a mixed message ("PCB 交期是
    多久") is measured by the script that carries its meaning.
    """
    cjk = len(_CJK_CHAR.findall(text))
    if cjk >= MIN_ANSWERABLE_CJK_CHARS:
        return False
    latin = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    return latin < MIN_ANSWERABLE_LATIN_CHARS


def needs_clarification(question: str, prior: list[Turn]) -> tuple[bool, str]:
    """Whether to ask the customer to say more, and what to ask.

    The distinction that matters is between *unanswerable* and *underspecified*
    — they look identical to a confidence score and call for opposite actions:

    - underspecified → ask the minimum useful clarification (the customer is
      present, one detail unblocks the answer);
    - unanswerable   → abstain and hand off (no amount of clarification will
      produce authorized evidence).

    This decides only the first. Refusing to decide the second keeps it from
    being used as a softer abstention: a clarification that is really a handoff
    leaves the customer waiting for a reply that is never coming.

    A continuation with usable history is **not** underspecified — "how about
    monthly?" is answerable once the antecedent is resolved, which is what
    `rewrite_query` is for. Asking a customer to repeat what they already said
    is the expensive failure here.
    """
    stripped = question.strip()
    if _is_too_short_to_answer(stripped):
        return True, "QUESTION_TOO_SHORT"
    if not _content_terms(stripped):
        return True, "NO_QUESTION_CONTENT"
    if is_continuation(stripped) and not _has_usable_history(prior):
        # A follow-up with nothing to follow up on. There is no antecedent to
        # resolve, so the only way forward is to ask.
        return True, "FOLLOW_UP_WITHOUT_ANTECEDENT"
    return False, ""


def _has_usable_history(prior: list[Turn]) -> bool:
    return any(
        turn.role is TurnRole.CUSTOMER and _content_terms(turn.text) - _REFERENCE_ONLY
        for turn in prior
    )


# --- Compaction ------------------------------------------------------------


@dataclass(frozen=True)
class CompactedContext:
    """What one run may put in front of the model.

    `pinned` and `summary` are separated rather than merged so a reader (and a
    test) can tell "the model was told this because it was recent" from "the
    model was told this because it is obligatory". Those have different failure
    modes and collapsing them hides which one broke.
    """

    recent: list[Turn]
    summary: str
    pinned: list[str]
    durable_facts: tuple[DurableFact, ...]
    dropped_turns: int
    topic_shift: bool
    budget_chars: int
    used_chars: int

    def render(self) -> str:
        """The conversation block for the prompt, or "" when there is none.

        Order follows `docs/agent.md`'s priority list: obligations first, then
        the durable facts that scope the answer, then the summary, then the
        recent turns last — nearest the question they belong to.
        """
        blocks: list[str] = []
        if self.pinned:
            blocks.append("Standing items (must not be contradicted):\n" + "\n".join(self.pinned))
        if self.durable_facts:
            blocks.append(
                "Known about this customer:\n"
                + "\n".join(f"- {f.key}: {f.value}" for f in self.durable_facts)
            )
        if self.summary:
            blocks.append("Earlier in this conversation:\n" + self.summary)
        if self.recent:
            blocks.append(
                "Recent turns:\n"
                + "\n".join(f"{t.role.value}: {t.text.strip()}" for t in self.recent)
            )
        return "\n\n".join(blocks)

    def as_dict(self) -> dict[str, Any]:
        """Audit-safe snapshot for `AgentRun.model_config`.

        Turn *text* is deliberately excluded: the run already persists an
        `input_hash` and the audit log owns what was said. Storing customer
        text in a second place creates a second thing to redact and a second
        thing to retain.
        """
        return {
            "turns_stored": len(self.recent) + self.dropped_turns,
            "turns_kept": len(self.recent),
            "turns_summarized": self.dropped_turns,
            "pinned_count": len(self.pinned),
            "pinned_reasons": sorted({p.split(":")[0] for p in self.pinned}),
            "durable_fact_keys": [f.key for f in self.durable_facts],
            "topic_shift": self.topic_shift,
            "budget_chars": self.budget_chars,
            "used_chars": self.used_chars,
        }


def _turn_chars(turn: Turn) -> int:
    # `role: ` prefix included so the rendered size is what is measured.
    return len(turn.role.value) + 2 + len(turn.text.strip())


@dataclass
class ConversationMemory:
    """Bounded, ordered conversation state for one conversation.

    Mutable by design — it is per-conversation working state, not a value
    object — but every method is deterministic and side-effect free apart from
    `add`, so a caller can snapshot it into an `AgentRun` cheaply.
    """

    turns: list[Turn] = field(default_factory=list)
    max_turns: int = MAX_STORED_TURNS
    budget_chars: int = DEFAULT_CONTEXT_BUDGET_CHARS
    recent_turns: int = DEFAULT_RECENT_TURNS

    def add(self, turn: Turn) -> int:
        """Append a turn, evicting the oldest beyond `max_turns`.

        Returns the number of evicted turns that carried a pin, because a
        pinned line falling out of the window is an obligation the platform has
        silently dropped. Callers that care (the worker that owns a
        conversation) can surface it; ignoring the count is fine for callers
        whose conversations cannot outlive the window.
        """
        self.turns.append(turn)
        if len(self.turns) <= self.max_turns:
            return 0
        evicted = self.turns[0 : len(self.turns) - self.max_turns]
        del self.turns[0 : len(self.turns) - self.max_turns]
        return sum(1 for turn in evicted if pin_reason(turn))

    def compact(self, *, budget_chars: int | None = None) -> CompactedContext:
        """Compress stored turns into a bounded context block.

        Algorithm, in order — the order is the contract:

        1. **Pin first, unconditionally.** Pinned lines are carried whatever
           they cost. A budget that can drop a commitment is not a budget, it
           is a bug.
        2. **Keep the recent tail verbatim**, newest that fits the remaining
           budget. This is where anaphora resolves, so it must be word-for-word.
        3. **Summarize what is left** into salient lines, never dropping a
           pinned line (already carried in step 1, so the summary omits them
           rather than duplicating them).
        4. **Report the cost.** `used_chars` is what was actually spent; a
           compaction that silently overruns its budget would make the prompt
           size unauditable.

        If pinned lines alone exceed the budget, they still win. The caller
        sees the overrun in `used_chars` and can decide — truncating an
        obligation is not a decision this module will make on its own.
        """
        budget = budget_chars if budget_chars is not None else self.budget_chars
        if not self.turns:
            return CompactedContext(
                recent=[],
                summary="",
                pinned=[],
                durable_facts=(),
                dropped_turns=0,
                topic_shift=False,
                budget_chars=budget,
                used_chars=0,
            )

        pinned: list[str] = []
        pinned_indexes: set[int] = set()
        for index, turn in enumerate(self.turns):
            reason = pin_reason(turn)
            if reason:
                pinned_indexes.add(index)
                pinned.append(f"{reason}: {turn.text.strip()}")

        used = sum(len(p) + 1 for p in pinned)

        # Recent tail, bounded by *both* the budget and `recent_turns`.
        #
        # The turn cap is not redundant with the budget. Given a generous
        # budget the whole conversation would fit, and then "the recent tail"
        # means "everything" — which is exactly the `docs/agent.md` prohibition
        # on copying the full conversation into every request. Keeping a short
        # tail is also what makes the summary meaningful: it is the difference
        # between "recent" and "older", and without a cap there is no older.
        #
        # Walked by index so a pinned turn in the tail is recognised as already
        # carried above — including it again would spend the budget twice on
        # the same sentence.
        kept_indexes: list[int] = []
        for index in range(len(self.turns) - 1, -1, -1):
            if index in pinned_indexes:
                continue
            cost = _turn_chars(self.turns[index])
            if kept_indexes and (len(kept_indexes) >= self.recent_turns or used + cost > budget):
                break
            kept_indexes.append(index)
            used += cost
        kept_indexes.reverse()
        kept = [self.turns[i] for i in kept_indexes]

        kept_set = set(kept_indexes) | pinned_indexes
        summarized = [t for i, t in enumerate(self.turns) if i not in kept_set]
        summary = _summarize(summarized)
        used += len(summary)

        return CompactedContext(
            recent=kept,
            summary=summary,
            pinned=pinned,
            durable_facts=extract_durable_facts(self.turns),
            dropped_turns=len(summarized),
            topic_shift=False,
            budget_chars=budget,
            used_chars=used,
        )

    def snapshot(self, *, question: str = "") -> CompactedContext:
        """Compact and evaluate topic shift for the incoming question.

        Separate from `compact` because topic shift is a property of the *new*
        utterance against the history, not of the history alone — and callers
        almost always want both.
        """
        context = self.compact()
        if question:
            prior = self.turns
            return replace(context, topic_shift=topic_shifted(prior, question))
        return context


def _summarize(turns: list[Turn]) -> str:
    """Deterministic summary of turns that did not fit the recent tail.

    Not a model call, and not a truncation either. It keeps the *first* line of
    each turn up to a small per-turn cap, which is where a support turn states
    its subject ("I was charged twice for the March invoice"). A model
    abstractive summary would read better and could not be reproduced, audited
    or diffed — and a summary that cannot be diffed cannot be regression
    tested, which for a compression stage is disqualifying.

    Pinned turns are absent from the input by construction (`compact` removes
    them), so this never has to decide whether an obligation is important.
    """
    if not turns:
        return ""
    lines: list[str] = []
    for turn in turns:
        first = turn.text.strip().splitlines()[0] if turn.text.strip() else ""
        if not first:
            continue
        if len(first) > _SUMMARY_LINE_CHARS:
            first = first[: _SUMMARY_LINE_CHARS - 1].rstrip() + "…"
        lines.append(f"- {turn.role.value}: {first}")
    return "\n".join(lines)


_SUMMARY_LINE_CHARS = 120


def now_ts() -> int:
    """Injectable clock, so tests do not depend on wall time."""
    return int(time.time())


__all__: list[str] = [
    "DEFAULT_CONTEXT_BUDGET_CHARS",
    "DEFAULT_RECENT_TURNS",
    "MAX_STORED_TURNS",
    "CompactedContext",
    "ConversationMemory",
    "DurableFact",
    "Turn",
    "TurnRole",
    "extract_durable_facts",
    "is_continuation",
    "needs_clarification",
    "pin_reason",
    "rewrite_query",
    "topic_shifted",
]
