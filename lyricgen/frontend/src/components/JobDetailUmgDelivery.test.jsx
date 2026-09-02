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
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(
      async (url) => {
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
            job={job}
            onBack={vi.fn()}
            onJobUpdate={onJobUpdate}
          />
        </AlertProvider>
      </MemoryRouter>,
    );

    fireEvent.click(screen.getByText("detail.send_umg"));
    expect(screen.getByText("prores.enable_title")).toBeTruthy();

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
    });

    expect(await screen.findByText("Video publicado en umg.genly.pro")).toBeTruthy();
    expect(screen.getByText("detail.in_umg_portal")).toBeTruthy();
    expect(onJobUpdate).toHaveBeenCalledWith(expect.objectContaining({
      prores_ready: true,
    }));
  });
});
