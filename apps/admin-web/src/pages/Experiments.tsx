import { useState } from "react";
import { apiGet, apiPut } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { ExperimentDefinition, ExperimentResults } from "../lib/types";
import {
  ActionFeedback,
  Badge,
  Card,
  EmptyState,
  ListTotal,
  PageHeader,
  Spinner,
} from "../components/ui";
import { LoadError } from "../components/LoadError";
import { useLang } from "../lib/i18n";

/**
 * A/B experiments: define the arms, read what each one did.
 *
 * The mechanism existed since feature 8.6 and **nothing called it**, so an
 * experiment nobody could run was a comparison nobody could read. This is the
 * screen that makes it usable, and it is deliberately two panels rather than
 * one editor:
 *
 * - **Definitions** are what you set, and they are cheap to change.
 * - **Results** are what happened, and they are read from the runs that recorded
 *   their arm - never re-derived from the weights. Re-deriving would silently
 *   re-bucket every past run the moment a split changed, so a number you looked
 *   at yesterday would change and you would be comparing two populations rather
 *   than two arms.
 *
 * `automation_rate` renders as "—" when an arm has no runs, never as 0%. "Nobody
 * was bucketed here" and "everything in it escalated" are opposite facts, and
 * only the second is a reason to stop the experiment.
 */

interface ArmDraft {
  name: string;
  weight: string;
}

const MAX_ARMS = 8;

export function Experiments() {
  const { t } = useLang();
  const action = useAction();
  const [key, setKey] = useState("");
  const [description, setDescription] = useState("");
  const [enabled, setEnabled] = useState(false);
  const [arms, setArms] = useState<ArmDraft[]>([
    { name: "control", weight: "50" },
    { name: "candidate", weight: "50" },
  ]);

  const data = useAsync<{
    experiments: ExperimentDefinition[];
    results: ExperimentResults[];
    count: number;
  }>(() => apiGet("/v1/quality/experiments"));

  const trimmedKey = key.trim();
  const keyValid = trimmedKey !== "" && /^[A-Za-z0-9._-]+$/.test(trimmedKey);
  const namedArms = arms.filter((a) => a.name.trim() !== "");
  const weightsValid = namedArms.every((a) => Number(a.weight) > 0);
  const canSave = keyValid && namedArms.length > 0 && weightsValid && !action.busy;

  function save(): void {
    if (!canSave) return;
    void action
      .run(
        () =>
          apiPut(
            `/v1/quality/experiments/${encodeURIComponent(trimmedKey)}`,
            {
              description: description.trim(),
              variants: namedArms.map((a) => ({
                name: a.name.trim(),
                weight: Number(a.weight),
              })),
              enabled,
            },
            newIdempotencyKey(),
          ).then(() => undefined),
        t("experiments.saved", { key: trimmedKey }),
      )
      .then((ok) => {
        if (ok) data.reload();
      });
  }

  /** Flip `enabled` without touching the arms - a full PUT, so the arms must be
   * sent back exactly as they are or a toggle would rewrite the split. */
  function toggle(exp: ExperimentDefinition): void {
    void action
      .run(
        () =>
          apiPut(
            `/v1/quality/experiments/${encodeURIComponent(exp.key)}`,
            {
              description: exp.description,
              variants: exp.variants,
              enabled: !exp.enabled,
            },
            newIdempotencyKey(),
          ).then(() => undefined),
        exp.enabled
          ? t("experiments.stopped", { key: exp.key })
          : t("experiments.started", { key: exp.key }),
      )
      .then((ok) => {
        if (ok) data.reload();
      });
  }

  const resultsByKey = new Map((data.data?.results ?? []).map((r) => [r.key, r]));

  return (
    <div className="page">
      <PageHeader title={t("experiments.title")} subtitle={t("experiments.subtitle")} />

      <Card title={t("experiments.defineTitle")}>
        <div className="toolbar">
          <input
            className="text-input"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder={t("experiments.keyPlaceholder")}
            pattern="[A-Za-z0-9._-]+"
            aria-invalid={trimmedKey !== "" && !keyValid}
          />
          <input
            className="text-input"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t("experiments.descPlaceholder")}
          />
        </div>

        <div className="toolbar">
          {arms.map((arm, index) => (
            <span key={index} className="arm-row">
              <input
                className="text-input"
                value={arm.name}
                placeholder={t("experiments.armName")}
                onChange={(e) =>
                  setArms(arms.map((a, i) => (i === index ? { ...a, name: e.target.value } : a)))
                }
              />
              <input
                className="text-input text-input-narrow"
                value={arm.weight}
                inputMode="numeric"
                placeholder={t("experiments.armWeight")}
                onChange={(e) =>
                  setArms(arms.map((a, i) => (i === index ? { ...a, weight: e.target.value } : a)))
                }
              />
              {arms.length > 1 ? (
                <button
                  className="btn"
                  onClick={() => setArms(arms.filter((_, i) => i !== index))}
                  aria-label={t("experiments.removeArm")}
                >
                  −
                </button>
              ) : null}
            </span>
          ))}
          {arms.length < MAX_ARMS ? (
            <button
              className="btn"
              onClick={() => setArms([...arms, { name: "", weight: "1" }])}
            >
              {t("experiments.addArm")}
            </button>
          ) : null}
        </div>

        <div className="toolbar">
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            {t("experiments.startImmediately")}
          </label>
          <button className="btn btn-primary" disabled={!canSave} onClick={save}>
            {t("experiments.save")}
          </button>
        </div>

        {trimmedKey !== "" && !keyValid ? (
          <p className="prompt-error" role="alert">
            {t("experiments.invalidKey")}
          </p>
        ) : null}
        {!weightsValid ? (
          <p className="prompt-error" role="alert">
            {t("experiments.weightsInvalid")}
          </p>
        ) : null}
        <p className="muted">{t("experiments.weightNote")}</p>
      </Card>

      <ActionFeedback error={action.error} notice={action.notice} />
      <LoadError error={data.error} status={data.errorStatus} onRetry={data.reload} />
      {data.loading ? <Spinner label={t("experiments.loading")} /> : null}
      {data.data && data.data.experiments.length === 0 ? (
        <EmptyState message={t("experiments.empty")} />
      ) : null}

      {data.data && data.data.experiments.length > 0 ? (
        <Card title={t("experiments.resultsTitle")}>
          <div className="table-scroll">
            <table className="table">
              <thead>
                <tr>
                  <th scope="col">{t("experiments.headerKey")}</th>
                  <th scope="col">{t("experiments.headerArm")}</th>
                  <th scope="col" className="num">
                    {t("experiments.headerRuns")}
                  </th>
                  <th scope="col" className="num">
                    {t("experiments.headerAutomated")}
                  </th>
                  <th scope="col" className="num">
                    {t("experiments.headerRate")}
                  </th>
                  <th scope="col">{t("experiments.headerState")}</th>
                  <th scope="col" />
                </tr>
              </thead>
              <tbody>
                {data.data.experiments.flatMap((exp) => {
                  const arms = resultsByKey.get(exp.key)?.arms ?? {};
                  const rows = exp.variants.map((v) => ({
                    key: exp.key,
                    arm: v.name,
                    totals: arms[v.name],
                  }));
                  return rows.map((row, index) => (
                    <tr key={`${row.key}:${row.arm}`}>
                      <td className="cell-strong">{index === 0 ? exp.key : ""}</td>
                      <td>{row.arm}</td>
                      <td className="num">{row.totals?.runs ?? 0}</td>
                      <td className="num">{row.totals?.automated ?? 0}</td>
                      <td className="num">
                        {/* "—" not 0%: an arm nobody entered is not an arm that
                            failed, and only one of those is a reason to stop. */}
                        {row.totals?.automation_rate === null ||
                        row.totals?.automation_rate === undefined
                          ? "—"
                          : `${Math.round(row.totals.automation_rate * 100)}%`}
                      </td>
                      <td>
                        {index === 0 ? (
                          <Badge tone={exp.enabled ? "good" : "neutral"}>
                            {exp.enabled ? t("common.enabled") : t("common.disabled")}
                          </Badge>
                        ) : null}
                      </td>
                      <td className="row-actions">
                        {index === 0 ? (
                          <button className="btn" disabled={action.busy} onClick={() => toggle(exp)}>
                            {exp.enabled ? t("experiments.stop") : t("experiments.start")}
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  ));
                })}
              </tbody>
            </table>
          </div>
        </Card>
      ) : null}
      <ListTotal
        shown={data.data?.experiments.length ?? 0}
        total={data.data?.count ?? 0}
      />
    </div>
  );
}
