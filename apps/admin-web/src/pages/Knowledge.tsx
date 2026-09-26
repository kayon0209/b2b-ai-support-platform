import { useState } from "react";
import { useSearchParams } from "react-router-dom";

import { apiDelete, apiGet, apiPost, apiUpload } from "../lib/api";
import { newIdempotencyKey } from "../lib/idempotency";
import { useAction } from "../lib/useAction";
import { useAsync } from "../lib/useAsync";
import type { Alias, KnowledgeDocument } from "../lib/types";
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
import { usePrompt } from "../components/Prompt";
import { useLang } from "../lib/i18n";

/**
 * Knowledge base console (feature list 8.4).
 *
 * Two tabs, because they answer two different questions about the same corpus:
 * *what is in it* (documents and their ingestion state) and *how it is
 * searched* (the alias table that bridges wording to canonical terms, 3.6).
 *
 * Deliberately no third tab for "gaps" or "corrections": both already have
 * pages (`/gaps`, `/approvals`), and a second copy of a list is how two
 * screens start disagreeing about the same data.
 *
 * The upload form asks for `canonical_uri` because the backend treats it as
 * the document's identity - a re-upload with the same URI is the same document
 * at a new version, not a second document. The field is labelled as such so an
 * operator does not discover that by creating duplicates.
 */

/** Ingestion states, with the tone they are shown in. */
function toneForIngestion(status: string | null): "neutral" | "info" | "warn" | "good" | "bad" {
  switch (status) {
    case "ingested":
    case "ready":
      return "good";
    case "processing":
    case "queued":
      return "info";
    case "failed":
      return "bad";
    case "uploaded":
      return "warn";
    default:
      return "neutral";
  }
}

function toneForVersion(status: string | null): "neutral" | "info" | "warn" | "good" | "bad" {
  switch (status) {
    case "active":
      return "good";
    case "draft":
    case "processing":
      return "info";
    case "superseded":
    case "expired":
      return "neutral";
    case "failed":
      return "bad";
    default:
      return "neutral";
  }
}

export function Knowledge() {
  const { t } = useLang();
  // The tab lives in the address so a refresh keeps the operator where they
  // were - the same reason the gap queue does it.
  const [searchParams, setSearchParams] = useSearchParams();
  const tab: "documents" | "aliases" =
    searchParams.get("tab") === "aliases" ? "aliases" : "documents";

  function setTab(next: "documents" | "aliases") {
    const params = new URLSearchParams(searchParams);
    if (next === "documents") params.delete("tab");
    else params.set("tab", next);
    setSearchParams(params);
  }

  const action = useAction();
  const prompt = usePrompt();

  const spaces = useAsync<{ items: { id: string; name: string }[]; total: number }>(
    () => apiGet(`/v1/knowledge/spaces`),
    [],
  );
  const documents = useAsync<{ items: KnowledgeDocument[]; total: number }>(
    () => apiGet(`/v1/knowledge/documents?limit=100`),
    [],
  );
  const aliases = useAsync<{ items: Alias[]; total: number }>(
    () => apiGet(`/v1/knowledge/aliases`),
    [],
  );

  return (
    <div className="page">
      <PageHeader
        title={t("knowledge.title")}
        subtitle={t("knowledge.subtitle")}
        actions={
          <div className="segmented">
            <button
              className={`segment${tab === "documents" ? " active" : ""}`}
              onClick={() => setTab("documents")}
            >
              {t("knowledge.tabDocuments")}
            </button>
            <button
              className={`segment${tab === "aliases" ? " active" : ""}`}
              onClick={() => setTab("aliases")}
            >
              {t("knowledge.tabAliases")}
            </button>
          </div>
        }
      />

      <ActionFeedback error={action.error} notice={action.notice} />
      {prompt.element}

      {tab === "documents" ? (
        <>
          <UploadForm
            spaces={spaces.data?.items ?? []}
            spacesFailed={Boolean(spaces.error)}
            onUploaded={() => {
              documents.reload();
            }}
          />

          <LoadError
            error={documents.error}
            status={documents.errorStatus}
            onRetry={documents.reload}
          />
          {documents.loading ? <Spinner label={t("knowledge.loadingDocuments")} /> : null}
          {documents.data && documents.data.items.length === 0 ? (
            <EmptyState message={t("knowledge.emptyDocuments")} />
          ) : null}

          {documents.data && documents.data.items.length > 0 ? (
            <div className="table-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th scope="col">{t("knowledge.headerTitle")}</th>
                    <th scope="col">{t("knowledge.headerClassification")}</th>
                    <th scope="col">{t("knowledge.headerVersion")}</th>
                    <th scope="col">{t("common.status")}</th>
                    <th scope="col">{t("knowledge.headerIngest")}</th>
                  </tr>
                </thead>
                <tbody>
                  {documents.data.items.map((doc) => (
                    <tr key={doc.id}>
                      <td className="cell-strong">{doc.title}</td>
                      <td>
                        <Badge tone="neutral">{doc.classification}</Badge>
                      </td>
                      {/* "—" rather than a blank cell: an empty cell reads as a
                          rendering bug, while a dash reads as "no version
                          yet", which is what it is. */}
                      <td>{doc.version_label ?? "—"}</td>
                      <td>
                        {doc.status ? (
                          <Badge tone={toneForVersion(doc.status)}>{doc.status}</Badge>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td>
                        {doc.ingestion_status ? (
                          <Badge tone={toneForIngestion(doc.ingestion_status)}>
                            {doc.ingestion_status}
                          </Badge>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
          <ListTotal
            shown={documents.data?.items.length ?? 0}
            total={documents.data?.total ?? 0}
          />
        </>
      ) : (
        <AliasTable
          aliases={aliases.data?.items ?? []}
          loading={aliases.loading}
          error={aliases.error}
          errorStatus={aliases.errorStatus}
          onReload={aliases.reload}
          onAdd={async () => {
            const values = await prompt.ask({
              title: t("knowledge.addAliasTitle"),
              confirmLabel: t("knowledge.addAlias"),
              detail: t("knowledge.addAliasDetail"),
              fields: [
                { name: "alias", label: t("knowledge.aliasLabel"), required: true },
                { name: "term", label: t("knowledge.termLabel"), required: true },
              ],
            });
            if (!values?.alias || !values?.term) return;
            const ok = await action.run(
              () =>
                apiPost(
                  "/v1/knowledge/aliases",
                  { alias: values.alias, term: values.term, weight: 1.0 },
                  newIdempotencyKey(),
                ).then(() => undefined),
              t("knowledge.aliasAdded"),
            );
            if (ok) aliases.reload();
          }}
          onDelete={async (alias: string) => {
            const values = await prompt.ask({
              title: t("knowledge.deleteAliasTitle", { alias }),
              confirmLabel: t("knowledge.deleteAlias"),
              fields: [{ name: "confirm", label: t("knowledge.deleteConfirmLabel") }],
            });
            if (!values) return;
            const ok = await action.run(
              () => apiDelete(`/v1/knowledge/aliases/${encodeURIComponent(alias)}`).then(() => undefined),
              t("knowledge.aliasDeleted"),
            );
            if (ok) aliases.reload();
          }}
          busy={action.busy}
        />
      )}
    </div>
  );
}

function UploadForm({
  spaces,
  spacesFailed,
  onUploaded,
}: {
  spaces: { id: string; name: string }[];
  spacesFailed: boolean;
  onUploaded: () => void;
}) {
  const { t } = useLang();
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [uri, setUri] = useState("");
  const [spaceId, setSpaceId] = useState("");
  const [versionLabel, setVersionLabel] = useState("v1");
  const action = useAction();

  // A space is required by the backend; keeping the button disabled until one
  // is picked turns a 400 into an obviously unavailable action.
  const ready = Boolean(file && title.trim() && uri.trim() && (spaceId || spaces.length === 0));

  async function submit() {
    if (!file) return;
    const form = new FormData();
    form.append("space_id", spaceId);
    form.append("title", title);
    form.append("canonical_uri", uri);
    form.append("classification", "internal");
    form.append("version_label", versionLabel || "v1");
    // The field name is `file` on the server; the filename travels inside the
    // FormData rather than in a separate parameter.
    form.append("file", file);

    const ok = await action.run(
      () => apiUpload("/v1/knowledge/documents", form).then(() => undefined),
      t("knowledge.uploaded"),
    );
    if (ok) {
      setFile(null);
      setTitle("");
      setUri("");
      setVersionLabel("v1");
      onUploaded();
    }
  }

  return (
    <Card>
      <div className="toolbar">
        <button className="btn" onClick={() => setOpen((v) => !v)}>
          {open ? t("knowledge.cancelUpload") : t("knowledge.startUpload")}
        </button>
        <span className="muted">{t("knowledge.uploadHint")}</span>
      </div>

      {open ? (
        <>
          <ActionFeedback error={action.error} notice={action.notice} />
          <div className="toolbar">
            <label className="field">
              <span>{t("knowledge.uploadFile")}</span>
              <input
                type="file"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              />
            </label>
            <label className="field">
              <span>{t("knowledge.uploadTitle")}</span>
              <input value={title} onChange={(e) => setTitle(e.target.value)} />
            </label>
            <label className="field">
              <span>{t("knowledge.uploadUri")}</span>
              <input
                value={uri}
                onChange={(e) => setUri(e.target.value)}
                placeholder="doc://policy/refund"
              />
            </label>
            {spaces.length > 0 ? (
              <label className="field">
                <span>{t("knowledge.uploadSpace")}</span>
                <select value={spaceId} onChange={(e) => setSpaceId(e.target.value)}>
                  <option value="">{t("knowledge.pickSpace")}</option>
                  {spaces.map((space) => (
                    <option key={space.id} value={space.id}>
                      {space.name}
                    </option>
                  ))}
                </select>
              </label>
            ) : null}
            <label className="field">
              <span>{t("knowledge.uploadVersion")}</span>
              <input
                value={versionLabel}
                onChange={(e) => setVersionLabel(e.target.value)}
              />
            </label>
            {/* Uploading into a corpus with no space is not possible, and an
                empty picker looks like a bug rather than a missing step. */}
            {spaces.length === 0 ? (
              <span className="muted">
                {spacesFailed ? t("knowledge.spacesFailed") : t("knowledge.noSpaces")}
              </span>
            ) : null}
          </div>
          <div className="toolbar">
            <button className="btn primary" disabled={!ready || action.busy} onClick={submit}>
              {t("knowledge.uploadSubmit")}
            </button>
          </div>
        </>
      ) : null}
    </Card>
  );
}

function AliasTable({
  aliases,
  loading,
  error,
  errorStatus,
  onReload,
  onAdd,
  onDelete,
  busy,
}: {
  aliases: Alias[];
  loading: boolean;
  error: string | null;
  errorStatus?: number | null;
  onReload: () => void;
  onAdd: () => void;
  onDelete: (alias: string) => void;
  busy: boolean;
}) {
  const { t } = useLang();
  return (
    <>
      <div className="toolbar">
        <button className="btn primary" onClick={onAdd} disabled={busy}>
          {t("knowledge.addAlias")}
        </button>
        {/* The table is what search uses to bridge wording to canonical terms
            (3.6); saying so stops it reading as a decorative dictionary. */}
        <span className="muted">{t("knowledge.aliasHint")}</span>
      </div>

      <LoadError error={error} status={errorStatus ?? null} onRetry={onReload} />
      {loading ? <Spinner label={t("knowledge.loadingAliases")} /> : null}
      {!loading && aliases.length === 0 ? (
        <EmptyState message={t("knowledge.emptyAliases")} />
      ) : null}

      {aliases.length > 0 ? (
        <div className="table-scroll">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">{t("knowledge.aliasLabel")}</th>
                <th scope="col">{t("knowledge.termLabel")}</th>
                <th scope="col" className="num">
                  {t("knowledge.weightLabel")}
                </th>
                <th />
              </tr>
            </thead>
            <tbody>
              {aliases.map((row) => (
                <tr key={row.alias}>
                  <td className="cell-strong">{row.alias}</td>
                  <td>{row.term}</td>
                  <td className="num">{row.weight.toFixed(2)}</td>
                  <td className="row-actions">
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() => onDelete(row.alias)}
                    >
                      {t("knowledge.deleteAlias")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      <ListTotal shown={aliases.length} total={aliases.length} />
    </>
  );
}
