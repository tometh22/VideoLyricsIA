import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AlertProvider } from "./AlertProvider";
import JobDetail from "./JobDetail";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key) => key }),
}));

vi.mock("../mediaUrl", () => ({
  getDownloadUrl: vi.fn(),
  useMediaUrl: vi.fn(() => null),
}));

const job = {
  job_id: "83f95d0e2679",
  parent_job_id: "a0a7fd193f2e",
  filename: "variante.mp3",
  song_title: "Variante",
  artist: "Artista",
  status: "done",
  progress: 100,
  approved_by: 2,
  approved_at: "2026-07-26T22:30:21Z",
  delivery_profile: "youtube",
  umg_spec: null,
  files: {
    video_url: "/download/83f95d0e2679/video",
    short_url: "/download/83f95d0e2679/short",
    thumbnail_url: "/download/83f95d0e2679/thumbnail",
  },
  s3_keys: {
    video: "tenant/job/lyric_video.mp4",
    short: "tenant/job/short.mp4",
    thumbnail: "tenant/job/thumbnail.jpg",
  },
};

function response(status, body, headers = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => headers[name] || null },
    json: async () => body,
  };
}

function StatefulJobDetail({ initialJob, onJobUpdate = vi.fn() }) {
  const [currentJob, setCurrentJob] = useState(initialJob);
  return (
    <JobDetail
      job={currentJob}
      onBack={vi.fn()}
      onJobUpdate={(nextJob) => {
        setCurrentJob(nextJob);
        onJobUpdate(nextJob);
      }}
    />
  );
}

describe("JobDetail UMG delivery recovery", () => {
  beforeEach(() => {
    localStorage.setItem("genly_token", "admin-token");
    localStorage.setItem("genly_user", JSON.stringify({
      role: "admin",
      features: { prores_export: true },
    }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("configures ProRes, waits for both masters and then publishes", async () => {
    const onJobUpdate = vi.fn();
    const currentReport = {
      status: "COMPLETE", mode: "enforce", decision: "PASS", report_id: "qc-ready",
      source_fingerprint: "source-current", visual_fingerprint: "visual-current",
      delivery_spec: {}, issues: [], approval: { blocked: false, can_approve: true },
    };
    const preflightedJob = {
      ...job,
      delivery_qc: {
        status: "COMPLETE", mode: "enforce", decision: "PASS",
        source_fingerprint: "source-current", visual_fingerprint: "visual-current",
        delivery_spec: {}, issues: [], approval: { blocked: false, can_approve: true },
      },
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (url) => {
        if (url.includes("/delivery-qc/recheck")) return response(200, { ok: true, delivery_qc: currentReport });
        if (url.includes("/enable-prores/")) {
          return response(200, {
            ok: true,
            umg_spec: {
              frame_size: "HD",
              fps: 29.97,
              prores_profile: 3,
            },
          });
        }
        if (url.includes("/admin/deliveries/from-job/")) {
          const publishCalls = fetchMock.mock.calls.filter(
            ([calledUrl]) => calledUrl.includes("/admin/deliveries/from-job/"),
          ).length;
          if (publishCalls === 1) {
            return response(202, {
              status: "preparing_prores",
              retry_after: 1,
              missing: ["umg_master", "umg_short"],
            });
          }
          return response(200, {
            ok: true,
            label: "Renderizado",
            replaced: false,
          });
        }
        if (url.includes("/status/")) {
          return response(200, {
            ...job,
            umg_spec: {
              frame_size: "HD",
              fps: 29.97,
              prores_profile: 3,
            },
            prores_ready: true,
            s3_keys: {
              ...job.s3_keys,
              umg_master: "tenant/job/umg_master.mov",
              umg_short: "tenant/job/umg_short.mov",
            },
          });
        }
        throw new Error(`Unexpected fetch: ${url}`);
      },
    );

    render(
      <MemoryRouter>
        <AlertProvider>
          <JobDetail
            job={preflightedJob}
            onBack={vi.fn()}
            onJobUpdate={onJobUpdate}
          />
        </AlertProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByText("detail.send_umg"));
    expect(screen.getByText(/esta variante lo reemplaza en la misma entrega/)).toBeTruthy();
    fireEvent.click(screen.getByText("umg.portal_argentina"));
    expect(await screen.findByText("prores.enable_title")).toBeTruthy();

    fireEvent.click(screen.getByText("prores.submit"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/enable-prores/83f95d0e2679"),
      expect.objectContaining({ method: "POST" }),
    ));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/status/83f95d0e2679"),
      expect.any(Object),
    ));
    await waitFor(() => {
      const publishCalls = fetchMock.mock.calls.filter(
        ([url]) => url.includes("/admin/deliveries/from-job/83f95d0e2679"),
      );
      expect(publishCalls).toHaveLength(2);
      expect(JSON.parse(publishCalls.at(-1)[1].body)).toEqual({ portal_id: "argentina" });
    });

    expect(await screen.findByText("Video publicado en umg.genly.pro")).toBeTruthy();
    expect(screen.getByText(/detail\.update_umg/)).toBeTruthy();
    expect(onJobUpdate).toHaveBeenCalledWith(expect.objectContaining({
      prores_ready: true,
    }));
  });

  it("runs and shows QC before configuring or preparing ProRes on the first UMG send", async () => {
    const pendingReport = {
      report_id: "qc-current", status: "COMPLETE", mode: "enforce", decision: "REVIEW",
      source_fingerprint: "source-current", visual_fingerprint: "visual-current",
      delivery_spec: {}, approval: { blocked: true, reason: "manual_review_required" },
      issues: [{
        issue_id: "manual-black-bars", code: "UMG_BLACK_BARS",
        status: "OPEN", result_status: "REVIEW", severity: "FAIL",
        manual_verification_required: true, detector: "mandatory_signed_reviewer_checklist",
        summary: "Revisar franjas negras", description: "Mirar el video completo.",
      }],
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url) => {
      if (url.includes("/admin/deliveries/from-job/")) {
        return response(409, {
          detail: {
            code: "delivery_qc_blocked",
            delivery_qc: { blocked: true, reason: "fresh_preflight_required" },
          },
        });
      }
      if (url.includes("/delivery-qc/recheck")) {
        return response(200, { ok: true, delivery_qc: pendingReport });
      }
      if (url.includes("/delivery-qc/review-attestation")) {
        return response(200, { ok: true, delivery_qc: {
          ...pendingReport, report_id: "qc-reviewed",
          approval: { blocked: false, can_approve: true },
          issues: pendingReport.issues.map(issue => ({ ...issue, status: "RESOLVED_MANUAL" })),
        } });
      }
      if (url.includes("/status/")) return response(200, { ...job, delivery_qc: pendingReport });
      throw new Error(`Unexpected fetch: ${url}`);
    });

    render(
      <MemoryRouter>
        <AlertProvider>
          <StatefulJobDetail initialJob={job} />
        </AlertProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByText("detail.send_umg"));
    fireEvent.click(screen.getByText("umg.portal_argentina"));

    expect(await screen.findByText("Preflight de entrega")).toBeInTheDocument();
    expect(screen.getByText("Revisar franjas negras")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => url.includes("/enable-prores/"))).toBe(false);
    expect(screen.queryByRole("button", { name: /Firmar/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirmar revisión del video" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: "Confirmar revisión del video" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/delivery-qc/review-attestation"),
      expect.objectContaining({ method: "POST", body: JSON.stringify({ confirmed: true, expected_report_id: "qc-current" }) }),
    ));
    expect(screen.getByRole("button", { name: "Continuar a publicar" })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => url.includes("/enable-prores/"))).toBe(false);
  });

  it("publishes an already prepared job to Chile", async () => {
    const currentReport = {
      status: "COMPLETE", mode: "enforce", decision: "PASS", report_id: "qc-ready-chile",
      source_fingerprint: "source-current", visual_fingerprint: "visual-current",
      delivery_spec: {}, issues: [], approval: { blocked: false, can_approve: true },
    };
    const preparedJob = {
      ...job,
      umg_spec: { frame_size: "HD", fps: 29.97, prores_profile: 3 },
      s3_keys: {
        ...job.s3_keys,
        umg_master: "tenant/job/umg_master.mov",
        umg_short: "tenant/job/umg_short.mov",
      },
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async (url, options) => {
      if (url.includes("/delivery-qc/recheck")) return response(200, { ok: true, delivery_qc: currentReport });
      if (url.includes("/admin/deliveries/from-job/")) {
        return response(200, { ok: true, label: "Renderizado", replaced: false, portal_id: "chile" });
      }
      throw new Error(`Unexpected fetch: ${url} ${options?.body || ""}`);
    });

    render(
      <MemoryRouter>
        <AlertProvider>
          <JobDetail job={preparedJob} onBack={vi.fn()} onJobUpdate={vi.fn()} />
        </AlertProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByText("detail.send_umg"));
    fireEvent.click(screen.getByText("umg.portal_chile"));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/admin/deliveries/from-job/83f95d0e2679"),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ portal_id: "chile" }),
      }),
    ));
    expect(await screen.findByText("Video publicado en umgchile.genly.pro")).toBeTruthy();
  });
});
