import {
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  api,
  ArAgingBucket,
  Building,
  BuildingDetail,
  ChatResponse,
  Citation,
  KpiSnapshot,
  ReviewItem,
} from "./api";

type TabId = "buildings" | "kpis" | "chat" | "review";

const TABS: { id: TabId; label: string }[] = [
  { id: "buildings", label: "Buildings" },
  { id: "kpis", label: "KPIs" },
  { id: "chat", label: "Chat" },
  { id: "review", label: "Review" },
];

function fmtNum(n: number | null | undefined, digits = 0): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function fmtMoney(n: number | string | null | undefined): string {
  if (n === null || n === undefined || n === "") return "—";
  const v = typeof n === "string" ? Number(n) : n;
  if (Number.isNaN(v)) return String(n);
  return v.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  });
}

function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return `${(Number(n) * 100).toFixed(1)}%`;
}

function fmtConfidence(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return `${(Number(n) * 100).toFixed(0)}%`;
}

function SourceChip({
  source,
  refLabel,
  detail,
}: {
  source: string;
  refLabel?: string | null;
  detail?: string | null;
}) {
  return (
    <span className="source-chip" title={detail || undefined}>
      <span className="source-chip__label">{source}</span>
      {refLabel ? <span className="source-chip__ref">{refLabel}</span> : null}
    </span>
  );
}

function Citations({ items }: { items: Citation[] }) {
  if (!items.length) return null;
  return (
    <div className="citations">
      <span className="citations__title">Sources</span>
      <ul>
        {items.map((c, i) => (
          <li key={`${c.source}-${c.ref}-${i}`}>
            <SourceChip source={c.source} refLabel={c.ref} detail={c.detail} />
            {c.detail ? <span className="citations__detail">{c.detail}</span> : null}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Hero({ onEnter }: { onEnter: (tab: TabId) => void }) {
  return (
    <header className="hero" id="top">
      <div className="hero__atmosphere" aria-hidden="true" />
      <div className="hero__content">
        <p className="brand">ModernEdge</p>
        <h1 className="hero__headline">Building ops and payroll, in one place</h1>
        <p className="hero__support">
          Staffing, call-outs, coverage, and finance KPIs with links back to
          source records.
        </p>
        <div className="hero__cta">
          <button
            type="button"
            className="btn btn--primary"
            onClick={() => onEnter("buildings")}
          >
            Buildings
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => onEnter("kpis")}
          >
            KPIs
          </button>
        </div>
      </div>
    </header>
  );
}

function BuildingsPanel() {
  const [buildings, setBuildings] = useState<Building[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<BuildingDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await api.listBuildings(true);
        if (!cancelled) {
          setBuildings(data.buildings);
          if (data.buildings.length && selectedId === null) {
            setSelectedId(data.buildings[0].id);
          }
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (selectedId === null) return;
    let cancelled = false;
    (async () => {
      setDetailLoading(true);
      try {
        const d = await api.getBuilding(selectedId);
        if (!cancelled) setDetail(d);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setDetailLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return buildings;
    return buildings.filter(
      (b) =>
        b.name.toLowerCase().includes(q) ||
        b.code.toLowerCase().includes(q) ||
        (b.client?.name || "").toLowerCase().includes(q)
    );
  }, [buildings, filter]);

  const shifts = useMemo(() => {
    if (!detail) return [];
    const day = detail.contracted_day_posts ?? 0;
    const night = detail.contracted_night_posts ?? 0;
    const active = detail.kpi?.active_headcount ?? detail.employee_count;
    const contracted =
      detail.kpi?.contracted_headcount ?? detail.contracted_headcount;
    const dayShare = day + night > 0 ? day / (day + night) : 0.5;
    const estimatedDayActive = Math.round(active * dayShare);
    const estimatedNightActive = Math.max(0, active - estimatedDayActive);
    return [
      {
        shift: "DAY",
        contracted: day,
        assigned: estimatedDayActive,
        covering: detail.kpi?.fill_rate ?? null,
        punchedHint: "Derived from active headcount × day post share",
      },
      {
        shift: "NIGHT",
        contracted: night,
        assigned: estimatedNightActive,
        covering: detail.kpi?.fill_rate ?? null,
        punchedHint: "Derived from active headcount × night post share",
      },
    ].map((row) => ({
      ...row,
      variance: row.assigned - row.contracted,
      contractedTotal: contracted,
    }));
  }, [detail]);

  return (
    <div className="panel panel--slide">
      <div className="panel__header">
        <div>
          <h2>Building view</h2>
          <p>
            Contracted posts by shift vs active headcount, with source lineage
            to buildings and kpi_snapshots.
          </p>
        </div>
        <label className="field field--inline">
          <span className="sr-only">Filter buildings</span>
          <input
            type="search"
            placeholder="Filter by name, code, client…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </label>
      </div>

      {error ? <p className="banner banner--error">{error}</p> : null}
      {loading ? <p className="muted">Loading buildings…</p> : null}

      <div className="building-layout">
        <aside className="building-list" aria-label="Buildings">
          {filtered.map((b) => (
            <button
              key={b.id}
              type="button"
              className={
                b.id === selectedId
                  ? "building-list__item is-active"
                  : "building-list__item"
              }
              onClick={() => setSelectedId(b.id)}
            >
              <span className="building-list__name">{b.name}</span>
              <span className="building-list__meta">
                {b.code} · {b.contracted_headcount} posts
              </span>
            </button>
          ))}
          {!loading && !filtered.length ? (
            <p className="muted">No buildings match.</p>
          ) : null}
        </aside>

        <div className="building-detail">
          {detailLoading && !detail ? (
            <p className="muted">Loading detail…</p>
          ) : null}
          {detail ? (
            <>
              <div className="building-detail__title-row">
                <div>
                  <h3>{detail.name}</h3>
                  <p className="muted">
                    {detail.client?.name ?? "No client"} · HR{" "}
                    {detail.hr_location_code ?? "—"} · Dispatch{" "}
                    {detail.dispatch_site_id ?? "—"}
                  </p>
                </div>
                <div className="source-row">
                  <SourceChip
                    source="buildings"
                    refLabel={detail.code}
                    detail={`id=${detail.id}`}
                  />
                  {detail.kpi ? (
                    <SourceChip
                      source="kpi_snapshots"
                      refLabel={detail.kpi.period}
                      detail={detail.kpi.building ?? undefined}
                    />
                  ) : null}
                </div>
              </div>

              <div className="shift-grid" role="table" aria-label="Shift staffing">
                <div className="shift-grid__head" role="row">
                  <span>Shift</span>
                  <span>Contracted</span>
                  <span>Assigned / active</span>
                  <span>vs contract</span>
                  <span>Fill / covering</span>
                </div>
                {shifts.map((s) => (
                  <div className="shift-grid__row" role="row" key={s.shift}>
                    <span className="shift-badge">{s.shift}</span>
                    <span>{fmtNum(s.contracted)}</span>
                    <span title={s.punchedHint}>{fmtNum(s.assigned)}</span>
                    <span
                      className={
                        s.variance < 0
                          ? "delta delta--neg"
                          : s.variance > 0
                            ? "delta delta--pos"
                            : "delta"
                      }
                    >
                      {s.variance > 0 ? "+" : ""}
                      {fmtNum(s.variance)}
                    </span>
                    <span>{fmtPct(s.covering)}</span>
                  </div>
                ))}
              </div>

              <dl className="stat-strip">
                <div>
                  <dt>Employees linked</dt>
                  <dd>
                    {fmtNum(detail.employee_count)}
                    <SourceChip source="employees" refLabel={`building_id=${detail.id}`} />
                  </dd>
                </div>
                <div>
                  <dt>Headcount variance</dt>
                  <dd>
                    {fmtNum(detail.kpi?.headcount_variance)}
                    <SourceChip source="kpi_snapshots" refLabel="headcount_variance" />
                  </dd>
                </div>
                <div>
                  <dt>Call-out shifts</dt>
                  <dd>
                    {fmtNum(detail.kpi?.callout_shifts)}
                    <SourceChip source="kpi_snapshots" refLabel="callout_shifts" />
                  </dd>
                </div>
                <div>
                  <dt>Unbilled coverage</dt>
                  <dd>
                    {fmtNum(detail.kpi?.unbilled_coverage_count)} /{" "}
                    {fmtMoney(detail.kpi?.unbilled_coverage_dollars)}
                    <SourceChip source="kpi_snapshots" refLabel="unbilled_coverage" />
                  </dd>
                </div>
              </dl>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}

type KpiFocus =
  | "headcount_variance"
  | "callout_rate"
  | "fill_rate"
  | "overtime"
  | "revenue"
  | "gm"
  | "ar"
  | "unbilled";

function KpisPanel() {
  const [period] = useState("2026-10");
  const [kpis, setKpis] = useState<KpiSnapshot[]>([]);
  const [ar, setAr] = useState<ArAgingBucket[]>([]);
  const [asOf, setAsOf] = useState<string | null>(null);
  const [focus, setFocus] = useState<KpiFocus>("headcount_variance");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [kpiRes, arRes] = await Promise.all([
          api.listKpis(period),
          api.getArAging(false),
        ]);
        if (!cancelled) {
          setKpis(kpiRes.kpis);
          setAr(arRes.buckets);
          setAsOf(arRes.as_of);
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [period]);

  const totals = useMemo(() => {
    const sum = (pick: (k: KpiSnapshot) => number | null | undefined) =>
      kpis.reduce((acc, k) => acc + (Number(pick(k)) || 0), 0);
    const avg = (pick: (k: KpiSnapshot) => number | null | undefined) => {
      const vals = kpis
        .map(pick)
        .filter((v): v is number => v !== null && v !== undefined);
      if (!vals.length) return null;
      return vals.reduce((a, b) => a + Number(b), 0) / vals.length;
    };
    return {
      headcount_variance: sum((k) => k.headcount_variance),
      callout_rate: avg((k) => k.callout_rate),
      fill_rate: avg((k) => k.fill_rate),
      overtime_hours: sum((k) => k.overtime_hours),
      overtime_dollars: sum((k) => k.overtime_dollars),
      revenue: sum((k) => k.revenue),
      gross_margin: sum((k) => k.gross_margin),
      unbilled_count: sum((k) => k.unbilled_coverage_count),
      unbilled_dollars: sum((k) => k.unbilled_coverage_dollars),
      ar_total: ar.reduce((a, b) => a + Number(b.open_amount || 0), 0),
    };
  }, [kpis, ar]);

  const tiles: {
    id: KpiFocus;
    label: string;
    value: string;
    source: string;
    ref: string;
  }[] = [
    {
      id: "headcount_variance",
      label: "Headcount variance",
      value: fmtNum(totals.headcount_variance),
      source: "kpi_snapshots",
      ref: "headcount_variance",
    },
    {
      id: "callout_rate",
      label: "Call-out rate",
      value: fmtPct(totals.callout_rate),
      source: "kpi_snapshots",
      ref: "callout_rate",
    },
    {
      id: "fill_rate",
      label: "Fill rate",
      value: fmtPct(totals.fill_rate),
      source: "kpi_snapshots",
      ref: "fill_rate",
    },
    {
      id: "overtime",
      label: "OT hours / $",
      value: `${fmtNum(totals.overtime_hours, 1)} / ${fmtMoney(totals.overtime_dollars)}`,
      source: "kpi_snapshots",
      ref: "overtime_hours, overtime_dollars",
    },
    {
      id: "revenue",
      label: "Revenue",
      value: fmtMoney(totals.revenue),
      source: "kpi_snapshots",
      ref: "revenue",
    },
    {
      id: "gm",
      label: "Gross margin",
      value: fmtMoney(totals.gross_margin),
      source: "kpi_snapshots",
      ref: "gross_margin",
    },
    {
      id: "ar",
      label: "AR aging",
      value: fmtMoney(totals.ar_total),
      source: "finance_ar_open",
      ref: asOf ? `as_of ${asOf}` : "buckets",
    },
    {
      id: "unbilled",
      label: "Unbilled coverage",
      value: `${fmtNum(totals.unbilled_count)} · ${fmtMoney(totals.unbilled_dollars)}`,
      source: "kpi_snapshots",
      ref: "unbilled_coverage",
    },
  ];

  return (
    <div className="panel panel--slide">
      <div className="panel__header">
        <div>
          <h2>KPI panel</h2>
          <p>
            Period {period}. Select a figure to inspect source records by
            building.
          </p>
        </div>
      </div>

      {error ? <p className="banner banner--error">{error}</p> : null}
      {loading ? <p className="muted">Loading KPIs…</p> : null}

      <div className="kpi-tiles" role="list">
        {tiles.map((t) => (
          <button
            key={t.id}
            type="button"
            role="listitem"
            className={
              focus === t.id ? "kpi-tile is-active" : "kpi-tile"
            }
            onClick={() => setFocus(t.id)}
          >
            <span className="kpi-tile__label">{t.label}</span>
            <span className="kpi-tile__value">{t.value}</span>
            <SourceChip source={t.source} refLabel={t.ref} />
          </button>
        ))}
      </div>

      <div className="kpi-detail">
        {focus === "ar" ? (
          <>
            <h3>AR aging buckets</h3>
            <p className="muted">
              Open balances as of {asOf ?? "—"}. Source: finance_ar_open.
            </p>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Bucket</th>
                  <th>Open amount</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {ar.map((b) => (
                  <tr key={b.bucket}>
                    <td>{b.bucket}</td>
                    <td>{fmtMoney(b.open_amount)}</td>
                    <td>
                      <SourceChip
                        source="finance_ar_open"
                        refLabel={b.bucket}
                        detail={asOf ?? undefined}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </>
        ) : (
          <>
            <h3>Source records — {tiles.find((t) => t.id === focus)?.label}</h3>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Building</th>
                  <th>Value</th>
                  <th>Source</th>
                </tr>
              </thead>
              <tbody>
                {kpis.map((k, i) => {
                  let value = "—";
                  let refLabel = String(focus);
                  switch (focus) {
                    case "headcount_variance":
                      value = fmtNum(k.headcount_variance);
                      refLabel = "headcount_variance";
                      break;
                    case "callout_rate":
                      value = fmtPct(k.callout_rate);
                      refLabel = "callout_rate";
                      break;
                    case "fill_rate":
                      value = fmtPct(k.fill_rate);
                      refLabel = "fill_rate";
                      break;
                    case "overtime":
                      value = `${fmtNum(k.overtime_hours, 1)} / ${fmtMoney(k.overtime_dollars)}`;
                      refLabel = "overtime";
                      break;
                    case "revenue":
                      value = fmtMoney(k.revenue);
                      refLabel = "revenue";
                      break;
                    case "gm":
                      value = `${fmtMoney(k.gross_margin)} (${fmtPct(k.gross_margin_pct)})`;
                      refLabel = "gross_margin";
                      break;
                    case "unbilled":
                      value = `${fmtNum(k.unbilled_coverage_count)} · ${fmtMoney(k.unbilled_coverage_dollars)}`;
                      refLabel = "unbilled_coverage";
                      break;
                    default:
                      break;
                  }
                  return (
                    <tr key={`${k.building}-${i}`}>
                      <td>{k.building ?? "—"}</td>
                      <td>{value}</td>
                      <td>
                        <SourceChip
                          source="kpi_snapshots"
                          refLabel={`${k.building || "row"} · ${refLabel}`}
                          detail={k.period ?? undefined}
                        />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  );
}

function ChatPanel() {
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [history, setHistory] = useState<
    { q: string; a: ChatResponse }[]
  >([]);

  const suggestions = [
    "What is the headcount variance at Meridian Plaza?",
    "Show call-out rates by building",
    "How many pending review items?",
    "What is AR aging?",
  ];

  const ask = useCallback(async (text: string) => {
    const q = text.trim();
    if (!q) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.chat(q);
      setHistory((h) => [{ q, a: res }, ...h]);
      setMessage("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void ask(message);
  };

  return (
    <div className="panel panel--slide">
      <div className="panel__header">
        <div>
          <h2>Chat</h2>
          <p>Questions against the loaded data, with source citations.</p>
        </div>
      </div>

      <form className="chat-form" onSubmit={onSubmit}>
        <label className="field">
          <span className="sr-only">Question</span>
          <textarea
            rows={3}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            placeholder="e.g. What is fill rate at Meridian One?"
            disabled={busy}
          />
        </label>
        <div className="chat-form__actions">
          <button type="submit" className="btn btn--primary" disabled={busy}>
            {busy ? "Asking…" : "Ask"}
          </button>
          <div className="chip-row">
            {suggestions.map((s) => (
              <button
                key={s}
                type="button"
                className="chip"
                disabled={busy}
                onClick={() => void ask(s)}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      </form>

      {error ? <p className="banner banner--error">{error}</p> : null}

      <div className="chat-history">
        {history.map((turn, i) => (
          <article key={`${turn.q}-${i}`} className="chat-turn">
            <p className="chat-turn__q">{turn.q}</p>
            <p
              className={
                turn.a.can_answer
                  ? "chat-turn__a"
                  : "chat-turn__a chat-turn__a--muted"
              }
            >
              {turn.a.answer}
            </p>
            <Citations items={turn.a.citations} />
          </article>
        ))}
        {!history.length ? (
          <p className="muted">No questions yet — try a suggestion above.</p>
        ) : null}
      </div>
    </div>
  );
}

function ReviewPanel() {
  const [items, setItems] = useState<ReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actingId, setActingId] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState("pending");

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listReview(statusFilter);
      setItems(data.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  const act = async (
    id: number,
    status: "resolved" | "rejected" | "pending",
    note: string
  ) => {
    setActingId(id);
    setError(null);
    try {
      await api.resolveReview(id, {
        status,
        note,
        resolution: { action: status === "resolved" ? "approve" : "hold" },
      });
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setActingId(null);
    }
  };

  return (
    <div className="panel panel--slide">
      <div className="panel__header">
        <div>
          <h2>Review</h2>
          <p>Call-outs held for review before payroll.</p>
        </div>
        <label className="field field--inline">
          <span className="sr-only">Status filter</span>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="pending">Pending</option>
            <option value="resolved">Resolved</option>
            <option value="rejected">Rejected</option>
          </select>
        </label>
      </div>

      {error ? <p className="banner banner--error">{error}</p> : null}
      {loading ? <p className="muted">Loading review items…</p> : null}

      <div className="review-list">
        {items.map((item) => (
          <article key={item.id} className="review-card">
            <div className="review-card__top">
              <h3>{item.title || `Item #${item.id}`}</h3>
              <span className="badge">{item.status}</span>
            </div>
            <p className="review-card__reason">{item.reason || "No reason provided."}</p>
            <dl className="review-card__meta">
              <div>
                <dt>Kind</dt>
                <dd>{item.kind ?? "—"}</dd>
              </div>
              <div>
                <dt>Confidence</dt>
                <dd
                  className={
                    item.confidence !== null && item.confidence < 0.7
                      ? "delta delta--neg"
                      : undefined
                  }
                >
                  {fmtConfidence(item.confidence)}
                </dd>
              </div>
              <div>
                <dt>Source</dt>
                <dd>
                  <SourceChip
                    source={item.source || "review_items"}
                    refLabel={item.source_ref}
                  />
                </dd>
              </div>
            </dl>
            {item.status === "pending" ? (
              <div className="review-card__actions">
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={actingId === item.id}
                  onClick={() =>
                    void act(item.id, "resolved", "Approved from review queue UI")
                  }
                >
                  Approve
                </button>
                <button
                  type="button"
                  className="btn btn--ghost"
                  disabled={actingId === item.id}
                  onClick={() =>
                    void act(item.id, "pending", "Held for further review")
                  }
                >
                  Hold
                </button>
                <button
                  type="button"
                  className="btn btn--subtle"
                  disabled={actingId === item.id}
                  onClick={() =>
                    void act(item.id, "rejected", "Rejected from review queue UI")
                  }
                >
                  Reject
                </button>
              </div>
            ) : (
              <p className="muted">
                Resolved by {item.resolved_by ?? "—"}
                {item.resolved_at ? ` · ${item.resolved_at}` : ""}
              </p>
            )}
          </article>
        ))}
        {!loading && !items.length ? (
          <p className="muted">No items in this queue.</p>
        ) : null}
      </div>
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<TabId>("buildings");
  const [pipelineMsg, setPipelineMsg] = useState<string | null>(null);
  const [pipelineBusy, setPipelineBusy] = useState(false);

  const enter = (next: TabId) => {
    setTab(next);
    requestAnimationFrame(() => {
      document.getElementById("workspace")?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    });
  };

  const runPipeline = async () => {
    setPipelineBusy(true);
    setPipelineMsg(null);
    try {
      const res = await api.runPipeline(false);
      setPipelineMsg(
        res.status === "ok"
          ? `Pipeline ${res.status}`
          : `Pipeline ${res.status}${res.error ? `: ${res.error}` : ""}`
      );
    } catch (e) {
      setPipelineMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setPipelineBusy(false);
    }
  };

  return (
    <div className="app">
      <Hero onEnter={enter} />

      <nav className="app-nav" aria-label="Primary">
        <a className="brand brand--nav" href="#top">
          ModernEdge
        </a>
        <div className="app-nav__tabs" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              className={tab === t.id ? "tab is-active" : "tab"}
              onClick={() => enter(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          className="btn btn--subtle btn--compact"
          disabled={pipelineBusy}
          onClick={() => void runPipeline()}
        >
          {pipelineBusy ? "Running…" : "Run pipeline"}
        </button>
      </nav>

      {pipelineMsg ? (
        <p className="banner banner--info pipeline-banner">{pipelineMsg}</p>
      ) : null}

      <main id="workspace" className="workspace" role="tabpanel">
        {tab === "buildings" ? <BuildingsPanel /> : null}
        {tab === "kpis" ? <KpisPanel /> : null}
        {tab === "chat" ? <ChatPanel /> : null}
        {tab === "review" ? <ReviewPanel /> : null}
      </main>

      <footer className="app-footer">
        <span>ModernEdge assessment</span>
        <span className="muted">Figures link to source records</span>
      </footer>
    </div>
  );
}
