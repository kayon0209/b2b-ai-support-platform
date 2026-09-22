/**
 * A read-tool receipt rendered as a card (feature list 4A.3/4A.4).
 *
 * An order status is a list of dated nodes, and a paragraph of prose is a worse
 * answer to "where is my order" than a timeline: the customer scans for the
 * stage, and the freshness of the data is what makes the answer trustworthy, so
 * it is shown rather than implied.
 *
 * This is the single card renderer. There used to be two: one in the operator
 * console that parsed the receipt JSON out of the turn text, and the customer
 * page, which had none and printed the JSON as a chat bubble. Two renderers for
 * one payload is how they drift, so this one takes the parsed card the API
 * already produces (`agent_runtime/tool_card.py`) and every surface calls it.
 *
 * Labels are a prop with Chinese defaults rather than an i18n lookup, because
 * the customer page is deliberately not part of the operator console's
 * translation bundle - pulling it in would couple a customer-facing surface to
 * an operator one. A surface with its own bundle passes the strings in.
 */

export type ToolCardNode = {
  label: string;
  state?: string;
  at?: string;
};

export type ToolCardData = {
  kind: string;
  title?: string | null;
  status?: string | null;
  nodes?: ToolCardNode[] | null;
  eta?: string | null;
  quantity?: number | null;
  carrier?: string | null;
  tracking_no?: string | null;
  fetched_at?: number | null;
  provenance?: string | null;
};

type Labels = {
  orderStatus: string;
  shipment: string;
  eta: string;
  quantity: string;
  carrier: string;
  tracking: string;
  updated: string;
  demoData: string;
};

const ZH: Labels = {
  orderStatus: "订单进度",
  shipment: "物流跟踪",
  eta: "预计出货",
  quantity: "数量",
  carrier: "承运商",
  tracking: "运单号",
  updated: "数据更新于",
  demoData: "示例数据",
};

/**
 * Provider states, mapped to what they mean. An unlisted state is shown as the
 * provider stated it rather than hidden: a stage this platform does not know
 * about is still a stage the customer is asking about.
 */
const STATUS_TEXT: Record<string, string> = {
  in_production: "生产中",
  shipped: "已发货",
  in_transit: "运输中",
  delivered: "已签收",
  pending: "待处理",
  cancelled: "已取消",
};

/** Node markers. Only these three are drawn differently; anything else is
 * neutral, which is the honest rendering of a state we cannot interpret. */
const NODE_STATE: Record<string, string> = {
  done: "is-done",
  active: "is-active",
  pending: "is-pending",
};

const STATE_TEXT: Record<string, string> = {
  done: "已完成",
  active: "进行中",
  pending: "待执行",
};

function relative(epochSeconds: number | null | undefined): string | null {
  if (typeof epochSeconds !== "number" || !Number.isFinite(epochSeconds)) return null;
  const minutes = Math.max(0, Math.round((Date.now() / 1000 - epochSeconds) / 60));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.round(hours / 24)} 天前`;
}

/** An instant as the customer reads it. An unparseable value is shown as-is
 * rather than dropped: the row is still informative without a tidy date. */
function stamp(value: string | null | undefined): string | null {
  if (typeof value !== "string" || !value) return null;
  const at = new Date(value);
  if (Number.isNaN(at.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ${pad(
    at.getHours(),
  )}:${pad(at.getMinutes())}`;
}

export function ToolCard({
  card,
  labels,
}: {
  card: ToolCardData;
  labels?: Partial<Labels>;
}) {
  const t = { ...ZH, ...(labels ?? {}) };
  const nodes = Array.isArray(card.nodes) ? card.nodes : [];
  const age = relative(card.fetched_at);
  const heading = card.kind === "shipment" ? t.shipment : t.orderStatus;
  const status = card.status ? (STATUS_TEXT[card.status] ?? card.status) : null;
  const eta = stamp(card.eta);

  return (
    <div className="tool-card">
      <div className="tool-card-head">
        <span className="tool-card-kind">{heading}</span>
        {card.title ? <strong className="tool-card-title">{card.title}</strong> : null}
        {status ? <span className="tool-card-status">{status}</span> : null}
        {/* Provenance before freshness: "how old" is meaningless without
            "from where", and the demo marker must not be missable. */}
        {card.provenance ? <span className="tool-card-demo">{t.demoData}</span> : null}
        {age ? (
          <span className="tool-card-age">
            {t.updated} {age}
          </span>
        ) : null}
      </div>

      {nodes.length > 0 ? (
        <ol className="tool-card-nodes">
          {nodes.map((node, index) => (
            <li
              key={`${node.label}-${index}`}
              className={`tool-card-node ${NODE_STATE[node.state ?? ""] ?? ""}`.trim()}
            >
              <span className="tool-card-dot" aria-hidden="true" />
              <span className="tool-card-node-label">{node.label}</span>
              {node.state ? (
                <span className="tool-card-node-state">
                  {STATE_TEXT[node.state] ?? node.state}
                </span>
              ) : null}
              {stamp(node.at) ? <span className="tool-card-node-at">{stamp(node.at)}</span> : null}
            </li>
          ))}
        </ol>
      ) : null}

      <div className="tool-card-facts">
        {eta ? (
          <span>
            {t.eta} <strong>{eta}</strong>
          </span>
        ) : null}
        {typeof card.quantity === "number" ? (
          <span>
            {t.quantity} <strong>{card.quantity}</strong>
          </span>
        ) : null}
        {card.carrier ? (
          <span>
            {t.carrier} <strong>{card.carrier}</strong>
          </span>
        ) : null}
        {card.tracking_no ? (
          <span>
            {t.tracking} <strong>{card.tracking_no}</strong>
          </span>
        ) : null}
      </div>
    </div>
  );
}
