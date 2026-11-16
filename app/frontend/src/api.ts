// Unset => local backend. Explicit "" (Docker build) => same-origin /api via nginx.
const API_BASE =
  import.meta.env.VITE_API_URL === undefined
    ? "http://localhost:8000"
    : import.meta.env.VITE_API_URL;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = (await res.json()) as { detail?: string };
      if (body.detail) detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export type ClientInfo = {
  id: number;
  code: string;
  name: string;
  billing_type: string | null;
  cost_plus_markup: number | null;
  backup_note_template: string | null;
};

export type Building = {
  id: number;
  code: string;
  name: string;
  hr_location_code: string | null;
  dispatch_site_id: string | null;
  finance_site_name: string | null;
  department_code: string | null;
  contracted_day_posts: number | null;
  contracted_night_posts: number | null;
  contracted_headcount: number;
  active: boolean;
  client: ClientInfo | null;
};

export type KpiSnapshot = {
  id?: number;
  building_id?: number | null;
  building: string | null;
  period: string | null;
  contracted_headcount: number | null;
  active_headcount: number | null;
  headcount_variance: number | null;
  scheduled_shifts: number | null;
  callout_shifts: number | null;
  callout_rate: number | null;
  fill_rate: number | null;
  overtime_hours: number | null;
  overtime_dollars: number | null;
  revenue: number | null;
  labor_cost: number | null;
  gross_margin: number | null;
  gross_margin_pct: number | null;
  unbilled_coverage_count: number | null;
  unbilled_coverage_dollars: number | null;
};

export type BuildingDetail = Building & {
  employee_count: number;
  kpi: KpiSnapshot | null;
};

export type Citation = {
  source: string;
  ref: string;
  detail: string;
};

export type ChatResponse = {
  answer: string;
  citations: Citation[];
  can_answer: boolean;
};

export type ReviewItem = {
  id: number;
  kind: string | null;
  status: string | null;
  title: string | null;
  reason: string | null;
  confidence: number | null;
  source: string | null;
  source_ref: string | null;
  payload: Record<string, unknown> | null;
  resolution: Record<string, unknown> | null;
  resolved_at: string | null;
  resolved_by: string | null;
  created_at: string | null;
};

export type ArAgingBucket = {
  bucket: string;
  open_amount: number | string;
};

export type PipelineResult = {
  status: string;
  stages?: Record<string, unknown>;
  output_dir?: string;
  error?: string;
};

export const api = {
  listBuildings: (activeOnly = true) =>
    request<{ buildings: Building[] }>(
      `/api/buildings?active_only=${activeOnly}`
    ),

  getBuilding: (id: number) =>
    request<BuildingDetail>(`/api/buildings/${id}`),

  listKpis: (period = "2026-10") =>
    request<{ period: string; kpis: KpiSnapshot[] }>(
      `/api/kpis?period=${encodeURIComponent(period)}`
    ),

  getArAging: (recompute = false) =>
    request<{ as_of: string; buckets: ArAgingBucket[] }>(
      `/api/kpis/ar-aging?recompute=${recompute}`
    ),

  chat: (message: string) =>
    request<ChatResponse>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, question: message }),
    }),

  listReview: (status: string | null = "pending") => {
    const qs =
      status === null
        ? ""
        : `?status=${encodeURIComponent(status)}`;
    return request<{ items: ReviewItem[]; count: number }>(
      `/api/review${qs}`
    );
  },

  resolveReview: (
    id: number,
    body: {
      status: "resolved" | "rejected" | "pending";
      resolved_by?: string;
      resolution?: Record<string, unknown>;
      note?: string;
    }
  ) =>
    request<ReviewItem>(`/api/review/${id}/resolve`, {
      method: "POST",
      body: JSON.stringify({
        resolved_by: "analyst",
        resolution: {},
        ...body,
      }),
    }),

  runPipeline: (background = false) =>
    request<PipelineResult>(`/api/pipeline/run?background=${background}`, {
      method: "POST",
      body: JSON.stringify({}),
    }),

  pipelineStatus: () =>
    request<{
      payroll_dry_run: boolean;
      counts: Record<string, number>;
    }>("/api/pipeline/status"),
};
