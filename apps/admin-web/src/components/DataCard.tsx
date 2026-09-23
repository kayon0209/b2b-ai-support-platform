import { Badge } from "./ui";
import { useLang } from "../lib/i18n";

/**
 * Tool receipts rendered as cards (feature list 4A.3/4A.4).
 *
 * An order status is a list of dated nodes, and a paragraph of prose is a
 * worse answer to "where is my order" than a timeline: the customer scans for
 * the stage, and the freshness of the data is the thing that makes the answer
 * trustworthy, so it is shown rather than implied.
 *
 * Receipts arrive as a JSON string on a `tool` turn. Anything that is not
 * recognisable JSON, or JSON we have no card for, falls back to plain text -
 * a parser that throws is worse than no card at all.
 */

interface OrderNode {
  label?: string;
  status?: string;
  at?: string | number | null;
}

interface Receipt {
  tool?: string;
  fetched_at?: string | number | null;
  order_id?: string;
  status?: string;
  nodes?: OrderNode[];
  [key: string]: unknown;
}

function parse(text: string): Receipt | null {
  try {
    const value: unknown = JSON.parse(text);
    return value !== null && typeof value === "object" ? (value as Receipt) : null;
  } catch {
    return null;
  }
}

function freshness(fetchedAt: string | number | null | undefined, now: number): string | null {
  if (fetchedAt === null || fetchedAt === undefined) return null;
  const epoch = typeof fetchedAt === "number" ? fetchedAt : Date.parse(fetchedAt) / 1000;
  if (!Number.isFinite(epoch)) return null;
  const minutes = Math.max(0, Math.round((now - epoch) / 60));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

export function DataCard({ text }: { text: string }) {
  const { t } = useLang();
  const receipt = parse(text);

  if (receipt === null || !Array.isArray(receipt.nodes) || receipt.nodes.length === 0) {
    return <p className="muted">{text}</p>;
  }

  const age = freshness(receipt.fetched_at, Math.floor(Date.now() / 1000));

  return (
    <div className="data-card">
      <div className="data-card-head">
        {receipt.order_id ? <strong>{receipt.order_id}</strong> : null}
        {receipt.status ? <Badge tone="good">{receipt.status}</Badge> : null}
        {age ? <span className="muted">{t("chat.dataFreshness")}: {age}</span> : null}
      </div>
      <ol className="data-card-nodes">
        {receipt.nodes.map((node, index) => (
          <li key={index}>
            <span className="data-card-node-label">{node.label ?? "—"}</span>
            {node.status ? <span className="muted">{node.status}</span> : null}
            {node.at ? <span className="muted">{String(node.at)}</span> : null}
          </li>
        ))}
      </ol>
    </div>
  );
}
