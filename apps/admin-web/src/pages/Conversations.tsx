import { useState } from "react";

import { apiGet } from "../lib/api";
import { useAsync } from "../lib/useAsync";
import type { ConversationList, ConversationReplay, ReplayRun, ReplayTurn } from "../lib/types";
import { Badge, Card, EmptyState, ListTotal, PageHeader, Spinner } from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";
import type { DictKey } from "../lib/i18n";
import { dateFromEpochSeconds, int, ms } from "../lib/format";

/**
 * Conversation replay (feature list 8.3).
 *
 * The workbench already shows an exchange, but only per case and only the
 * words. Replay is the screen that answers *why* an answer came out that way,
 * so every decision is rendered against the utterance that triggered it -
 * joined by hash, as `replay.py` documents - and the runs that could not be
 * attributed are still listed, separately and with the reason stated.
 *
 * Two things this page deliberately does not do:
 *
 * - It does not claim a reply came from a run. The platform cannot tie a
 *   reply turn to a run (the output hash is truncated and the draft may differ
 *   from what was sent), so the answer appears in order and the decision panel
 *   sits under its own question. Saying more would be inventing a link.
 * - It does not pretend the text is unredacted. `conversation_turns` stores
 *   the customer's sentence with PII-shaped tokens masked, and never stored
 *   the raw bytes; the banner says so, because an operator who believes they
 *   are reading raw text will draw the wrong conclusion from a `[PHONE]`.
 */

const PAGE_SIZE = 25;

/** Route names come from the orchestrator, not from a display table. */
function toneForRoute(route: string | null | undefined): "neutral" | "info" | "warn" | "good" {
  switch (route) {
    case "business_read":
      return "info";
    case "knowledge_qa":
      return "good";
    case "human_required":
      return "warn";
    default:
      return "neutral";
  }
}

function toneForStatus(status: string | null | undefined): "neutral" | "info" | "warn" | "good" | "bad" {
  switch (status) {
    case "completed":
      return "good";
    case "abstained":
      return "warn";
    case "failed":
      return "bad";
    case "queued":
    case "running":
      return "info";
    // Accepted, never executed, closed by the sweep. Not an error and not
    // work in progress - neutral is the honest tone.
    case "abandoned":
      return "neutral";
    default:
      return "neutral";
  }
}

/**
 * What the conversation-list badge should say.
 *
 * A run that never executed has no abstention reason, and its `route` is the
 * queue's default ("knowledge_qa") rather than anything the question was
 * classified as. Printing the route there labelled an abandoned row
 * `knowledge_qa` and read as traffic that had happened. For those two states
 * the status is the only true label.
 */
function badgeLabelFor(run: {
  route: string;
  status: string;
  abstain_reason: string | null;
}): string {
  if (run.abstain_reason) return run.abstain_reason;
  if (run.status === "queued" || run.status === "abandoned") return run.status;
  return run.route;
}

function toneForRole(role: string): "neutral" | "info" | "warn" | "good" {
  switch (role) {
    case "customer":
      return "info";
    case "agent":
      return "good";
    case "tool":
      return "warn";
    default:
      return "neutral";
  }
}

/** The intent snapshot, ordered the way it reads in a sentence. */
const INTENT_ORDER = [
  "scene",
  "business_line",
  "primary_kind",
  "secondary_kinds",
  "confidence",
  "action",
  "spelling_corrections",
] as const;

function DecisionPanel({ run }: { run: ReplayRun }) {
  const { t } = useLang();
  const intent = run.intent ?? {};
  const chips = INTENT_ORDER.filter((key) => intent[key] !== undefined).map((key) => {
    const value = intent[key];
    return {
      key,
      label: t(`conversations.intent.${key}` as DictKey),
      value: Array.isArray(value) ? value.join(", ") : String(value),
    };
  });

  return (
    <div className="replay-decision">
      <div className="replay-decision-head">
        <Badge tone={toneForRoute(run.route)}>{run.route}</Badge>
        <Badge tone={toneForStatus(run.status)}>{run.status}</Badge>
        {run.abstain_reason ? (
          <Badge tone="warn">{run.abstain_reason}</Badge>
        ) : null}
        {run.latency_ms !== null ? (
          <span className="muted replay-small">{ms(run.latency_ms)}</span>
        ) : null}
        {run.model ? <span className="muted replay-small">{run.model}</span> : null}
      </div>
      {chips.length > 0 ? (
        <ul className="replay-chips">
          {chips.map((chip) => (
            <li key={chip.key}>
              <span className="muted">{chip.label}</span>
              <span className="cell-code">{chip.value}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {run.sources.length > 0 ? (
        <>
          <div className="muted replay-small">{t("conversations.sources")}</div>
          <ul className="sources">
            {run.sources.map((source) => (
              <li key={`${source.claim_index}-${source.source_uri}`}>
                {source.source_uri}
                <span className="muted"> · {source.claim_index}</span>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {run.case_id ? (
        <div className="replay-small">
          <span className="muted">{t("conversations.caseLink")}</span>{" "}
          <span className="cell-code">{run.case_id.slice(0, 8)}</span>
        </div>
      ) : null}
      {/* Stated in the panel rather than left to a docstring: the force of
          every conclusion drawn from this panel depends on how the decision
          was tied to the question above it. */}
      <div className="muted replay-small">{t("conversations.matchedBy", { how: run.matched_by })}</div>
    </div>
  );
}

function Timeline({ replay }: { replay: ConversationReplay }) {
  const { t } = useLang();
  if (replay.turns.length === 0) {
    return <EmptyState message={t("conversations.noTurns")} />;
  }
  return (
    <ol className="replay-timeline">
      {replay.turns.map((turn: ReplayTurn, index: number) => (
        <li key={index}>
          <div className="replay-turn">
            <Badge tone={toneForRole(turn.role)}>{turn.role}</Badge>
            <span>{turn.text}</span>
            {turn.at ? (
              <span className="muted replay-small">{dateFromEpochSeconds(turn.at)}</span>
            ) : null}
          </div>
          {turn.decision ? (
            <DecisionPanel
              run={{
                ...turn.decision,
                started_at: turn.at,
                sources:
                  replay.runs.find((r) => r.run_id === turn.decision?.run_id)?.sources ?? [],
              }}
            />
          ) : null}
        </li>
      ))}
    </ol>
  );
}

export function Conversations() {
  const { t } = useLang();
  const [selected, setSelected] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);

  const list = useAsync(
    () => apiGet<ConversationList>(`/v1/conversations?limit=${PAGE_SIZE}&offset=${offset}`),
    [offset],
  );
  const replay = useAsync(
    () =>
      selected
        ? apiGet<ConversationReplay>(`/v1/conversations/${selected}/replay`)
        : Promise.resolve(null),
    [selected],
  );

  // Runs whose input hash matched no turn. Shown separately instead of
  // omitted: each one is a decision the platform took about this customer, and
  // a replay that hides them shows a tidier story than what happened.
  const attributed = new Set(
    (replay.data?.turns ?? []).map((turn) => turn.decision?.run_id).filter(Boolean),
  );
  const unattributed = (replay.data?.runs ?? []).filter((run) => !attributed.has(run.run_id));

  return (
    <div className="page">
      <PageHeader title={t("conversations.title")} subtitle={t("conversations.subtitle")} />

      {list.error ? <LoadError error={list.error} status={list.errorStatus} onRetry={list.reload} /> : null}
      {list.loading ? <Spinner label={t("conversations.loading")} /> : null}
      {list.data && list.data.items.length === 0 ? (
        <EmptyState message={t("conversations.empty")} />
      ) : null}

      <div className="grid-case">
        <Card title={t("conversations.list")}>
          {list.data && list.data.items.length > 0 ? (
            <ul className="case-list">
              {list.data.items.map((conversation) => (
                <li key={conversation.conversation_ref_id}>
                  <button
                    className={`case-row${selected === conversation.conversation_ref_id ? " active" : ""}`}
                    onClick={() => setSelected(conversation.conversation_ref_id)}
                  >
                    <span className="case-subject cell-code">
                      {conversation.conversation_ref_id.slice(0, 8)}
                    </span>
                    <span className="case-meta">
                      <span className="muted replay-small">
                        {t("conversations.turns", { count: int(conversation.turn_count) })}
                      </span>
                      {conversation.latest_run ? (
                        <Badge tone={toneForStatus(conversation.latest_run.status)}>
                          {badgeLabelFor(conversation.latest_run)}
                        </Badge>
                      ) : null}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <ListTotal shown={list.data?.items.length ?? 0} total={list.data?.items.length ?? 0} />
          <div className="replay-pager">
            <button
              className="btn btn-ghost"
              disabled={offset === 0 || list.loading}
              onClick={() => setOffset((current) => Math.max(0, current - PAGE_SIZE))}
            >
              {t("conversations.newer")}
            </button>
            <button
              className="btn btn-ghost"
              disabled={list.loading || (list.data?.items.length ?? 0) < PAGE_SIZE}
              onClick={() => setOffset((current) => current + PAGE_SIZE)}
            >
              {t("conversations.older")}
            </button>
          </div>
          {/* The count of conversations with nothing to replay. Dropping them
              from the list without saying so would read as "this is all there
              is"; they accumulate one per queue call whose worker never
              advanced it. */}
          {list.data && list.data.nothing_to_replay > 0 ? (
            <p className="muted replay-small">
              {t("conversations.awaiting", { count: int(list.data.nothing_to_replay) })}
            </p>
          ) : null}
        </Card>

        <div className="workbench-detail">
          {!selected ? (
            <Card title={t("conversations.replay")}>
              <EmptyState message={t("conversations.pick")} />
            </Card>
          ) : null}

          {replay.error ? (
            <LoadError error={replay.error} status={replay.errorStatus} onRetry={replay.reload} />
          ) : null}
          {replay.loading ? <Spinner label={t("conversations.loadingReplay")} /> : null}

          {replay.data ? (
            <>
              <Card title={t("conversations.replay")}>
                <ul className="kv">
                  <li>
                    <span>{t("conversations.ref")}</span>
                    <span className="cell-code">{replay.data.conversation_ref_id}</span>
                  </li>
                  <li>
                    <span>{t("conversations.turnsLabel")}</span>
                    <span>{int(replay.data.turn_count)}</span>
                  </li>
                  <li>
                    <span>{t("conversations.runsLabel")}</span>
                    <span>{int(replay.data.run_count)}</span>
                  </li>
                  <li>
                    <span>{t("conversations.window")}</span>
                    <span>
                      {replay.data.first_at ? dateFromEpochSeconds(replay.data.first_at) : "—"}
                      {" → "}
                      {replay.data.last_at ? dateFromEpochSeconds(replay.data.last_at) : "—"}
                    </span>
                  </li>
                </ul>
                {/* The one thing an operator must know before reading a single
                    line: this is the customer's wording with three value
                    classes masked, not the raw message. */}
                <p className="muted replay-small">{t("conversations.redactionNote")}</p>
              </Card>

              <Card title={t("conversations.exchange")}>
                <Timeline replay={replay.data} />
              </Card>

              {unattributed.length > 0 ? (
                <Card title={t("conversations.unattributed")}>
                  <p className="muted replay-small">{t("conversations.unattributedWhy")}</p>
                  <ul className="replay-timeline">
                    {unattributed.map((run) => (
                      <li key={run.run_id}>
                        <div className="replay-turn">
                          <Badge tone={toneForRoute(run.route)}>{run.route}</Badge>
                          <span className="cell-code">{run.run_id.slice(0, 8)}</span>
                          {run.started_at ? (
                            <span className="muted replay-small">
                              {dateFromEpochSeconds(run.started_at)}
                            </span>
                          ) : null}
                        </div>
                        <DecisionPanel run={run} />
                      </li>
                    ))}
                  </ul>
                </Card>
              ) : null}

              {replay.data.cases.length > 0 ? (
                <Card title={t("conversations.cases")}>
                  <ul className="kv">
                    {replay.data.cases.map((linked) => (
                      <li key={linked.case_id}>
                        <span className="cell-code">{linked.case_id.slice(0, 8)}</span>
                        <span className="case-meta">
                          <span>{linked.subject}</span>
                          <Badge tone={toneForStatus(linked.status)}>{linked.status}</Badge>
                        </span>
                      </li>
                    ))}
                  </ul>
                </Card>
              ) : null}
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
