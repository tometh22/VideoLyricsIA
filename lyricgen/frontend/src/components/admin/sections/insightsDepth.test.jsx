/**
 * Profundidad de Insights (2026-06-11): drill de features, ficha de job
 * y filtro de errores por categoría.
 */
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import FeatureDetailPanel from "./insights/FeatureDetailPanel";
import JobDetailPanel from "./insights/JobDetailPanel";
import ProblemsPanel from "./insights/ProblemsPanel";
import AdoptionPanel from "./insights/AdoptionPanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AdoptionPanel drilleable", () => {
  const ADOPTION = {
    total_jobs: 5, jobs_with_params: 5,
    features: { lyrics_animation: { karaoke: 3 }, line_transition: {}, effect: {},
                movement_style: {}, text_case: {}, text_contrast: {}, title_template: {},
                style: {}, delivery_profile: {} },
    font: [], flags: {}, background_source: {},
  };

  it("click en una barra dispara onDrill con feature+valor", () => {
    const onDrill = vi.fn();
    render(<AdoptionPanel adoption={ADOPTION} onDrill={onDrill} />);
    fireEvent.click(screen.getByText("Karaoke"));
    expect(onDrill).toHaveBeenCalledWith("lyrics_animation", "karaoke", "Karaoke");
  });

  it("sin onDrill las barras no son botones", () => {
    render(<AdoptionPanel adoption={ADOPTION} />);
    expect(screen.getByText("Karaoke").closest("button")).toBe(null);
  });
});

describe("FeatureDetailPanel", () => {
  beforeEach(() => {
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve({
        feature: "lyrics_animation", value: "karaoke", total_jobs: 2,
        users: [{ user_id: 7, username: "ana.m", count: 2, last_used: "2026-06-10T10:00:00Z" }],
        jobs: [{ job_id: "j1", user_id: 7, username: "ana.m", artist: "A", song_title: "S",
                 status: "done", created_at: "2026-06-10T10:00:00Z" }],
      }),
    }));
  });

  it("muestra usuarios y videos; click navega", async () => {
    const onUserClick = vi.fn();
    const onJobClick = vi.fn();
    render(
      <FeatureDetailPanel
        drill={{ feature: "lyrics_animation", value: "karaoke", label: "Karaoke" }}
        days={30}
        onClose={() => {}}
        onUserClick={onUserClick}
        onJobClick={onJobClick}
      />
    );
    // "ana.m" aparece en la tabla de usuarios Y en la sub-línea del video
    await waitFor(() => expect(screen.getAllByText("ana.m").length).toBeGreaterThan(0));
    expect(screen.getByText(/2 videos/)).toBeInTheDocument();
    fireEvent.click(screen.getAllByText("ana.m")[0]);
    expect(onUserClick).toHaveBeenCalled();
    fireEvent.click(screen.getByText(/A — S/));
    expect(onJobClick).toHaveBeenCalledWith("j1");
  });
});

describe("JobDetailPanel", () => {
  beforeEach(() => {
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true, status: 200,
      json: () => Promise.resolve({
        job_id: "j1", username: "ana.m", tenant_id: "umg", artist: "Rata Blanca",
        song_title: "Mujer Amante", status: "done", error: null, error_category: null,
        edit_count: 2, parent_job_id: null, delivery_profile: "both",
        created_at: "2026-06-10T10:00:00Z", completed_at: null, approved_at: null,
        choices: { style: "oscuro", background_source: "library_as_is" },
        render_params: { font: "Anton", lyrics_animation: "karaoke" },
        ai_calls: [{ step: "video_bg", tool_name: "veo-3.1", tool_provider: "google_vertex",
                     duration_ms: 8000, cost_usd: 0.8, created_at: "2026-06-10T10:01:00Z" }],
        ai_cost_usd: 0.8,
        events: [{ action: "job.download", detail: { job_id: "j1", file_type: "video" },
                   created_at: "2026-06-10T12:00:00Z" }],
      }),
    }));
  });

  it("muestra la ficha completa con costos por llamada", async () => {
    render(<JobDetailPanel jobId="j1" onClose={() => {}} />);
    await waitFor(() => expect(screen.getByText(/Rata Blanca — Mujer Amante/)).toBeInTheDocument());
    expect(screen.getByText(/font: Anton/)).toBeInTheDocument();
    expect(screen.getByText("veo-3.1")).toBeInTheDocument();
    expect(screen.getByText(/Llamadas IA \(1\)/)).toBeInTheDocument();
    expect(screen.getByText("job.download")).toBeInTheDocument();
    expect(screen.getByText(/2 re-renders/)).toBeInTheDocument();
  });
});

describe("ProblemsPanel filtro por categoría", () => {
  const ERRORS = [
    { job_id: "e1", artist: "A", song_title: "S1", username: "u1", category: "render",
      error: "render boom", created_at: "2026-06-11T10:00:00Z" },
    { job_id: "e2", artist: "B", song_title: "S2", username: "u2", category: "veo",
      error: "veo timeout", created_at: "2026-06-11T11:00:00Z" },
  ];

  it("click en un chip filtra; segundo click destrava", () => {
    render(<ProblemsPanel recentErrors={ERRORS} errorsByCategory={{ render: 1, veo: 1 }} />);
    expect(screen.getByText(/render boom/)).toBeInTheDocument();
    expect(screen.getByText(/veo timeout/)).toBeInTheDocument();
    // El chip usa el label del catálogo: "Render / ffmpeg: 1"
    fireEvent.click(screen.getByText(/Render \/ ffmpeg: 1/));
    expect(screen.getByText(/render boom/)).toBeInTheDocument();
    expect(screen.queryByText(/veo timeout/)).toBe(null);
    fireEvent.click(screen.getByText(/Render \/ ffmpeg: 1/));
    expect(screen.getByText(/veo timeout/)).toBeInTheDocument();
  });
});
