import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { I18nProvider } from "../i18n";
import { AlertProvider } from "./AlertProvider";
import JobDetail from "./JobDetail";
import SceneEditModal from "./SceneEditModal";
import TrialUsageSummary from "./TrialUsageSummary";

vi.mock("../mediaUrl", () => ({ getDownloadUrl: vi.fn(), useMediaUrl: () => null }));
vi.mock("./OnboardingTour", () => ({ JobDetailTour: () => null }));
vi.mock("./HelpCenter/HelpTip", () => ({ default: () => null }));

const scene = { recurrence_key: "puente", section_type: "puente", prompt: "Un barco", status: "generated" };
const job = { job_id: "trial-scene", status: "pending_review", song_title: "Río", artist: "QA", edit_count: 0, edits_remaining: 3, render_params: { enable_scenes: true }, scene_plan: { scenes: [scene], sections: [{ ...scene, start: 0, end: 10 }] } };
const wrap = (component) => render(<MemoryRouter><I18nProvider><AlertProvider>{component}</AlertProvider></I18nProvider></MemoryRouter>);
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("trial scene actions", () => {
  it("explains the separate allowance and submits the edited scene hint", () => {
    const submit = vi.fn();
    wrap(<SceneEditModal scene={scene} onClose={vi.fn()} onSubmit={submit} />);
    expect(screen.getByText(/cupo de regeneraciones de escena, separado/)).toBeInTheDocument();
    expect(screen.queryByText(/0\.90|cuenta como un edit/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "Una vela amarilla" } });
    fireEvent.click(screen.getByRole("button", { name: "Regenerar escena" }));
    expect(submit).toHaveBeenCalledWith({ hint: "Una vela amarilla", movement_style: "" });
  });

  it("keeps general edit allowance unchanged and labels a successful scene reroll correctly", async () => {
    const updated = vi.fn();
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ ok: true }), { status: 200 })));
    function Harness() {
      const [current, setCurrent] = useState(job);
      return <JobDetail job={current} onBack={vi.fn()} onJobUpdate={(next) => { updated(next); setCurrent(next); }} />;
    }
    wrap(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "Editar el prompt de esta escena" }));
    fireEvent.change(screen.getByPlaceholderText(/primer plano de gotas/i), { target: { value: "Una vela amarilla" } });
    fireEvent.click(screen.getByRole("button", { name: "Regenerar escena" }));
    await waitFor(() => expect(updated).toHaveBeenCalledWith(expect.objectContaining({ status: "editing", current_step: "scenes", edit_count: 0, edits_remaining: 3 })));
    expect(screen.getByText(/Regenerando la escena seleccionada/)).toBeInTheDocument();
    expect(screen.queryByText(/tipografía nueva/)).not.toBeInTheDocument();
  });

  it("does not call the final composition stage a typography change", () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 200 })));
    wrap(<JobDetail job={{ ...job, status: "editing", current_step: "video" }} />);
    expect(screen.getByText("Renderizando el video con tus cambios")).toBeInTheDocument();
  });

  it("allows approval at zero trial balance and displays the server's incomplete-scenes error", async () => {
    const message = "Regenerá las escenas fallidas antes de aprobar.";
    vi.stubGlobal("fetch", vi.fn(async (url) => String(url).includes("/approve/")
      ? new Response(JSON.stringify({ detail: { code: "scenes_incomplete", message } }), { status: 409 })
      : new Response("{}", { status: 200 })));
    wrap(<><TrialUsageSummary trial={{ state: "exhausted", credits: 9, reserved: 9, available: 0 }} /><JobDetail job={job} /></>);
    const approve = screen.getByRole("button", { name: "Aprobar" });
    expect(approve).toBeEnabled();
    fireEvent.click(approve);
    await screen.findByText(message);
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
  });

  it("preserves the server message for a non-YouTube scene conflict", async () => {
    const message = "Este video no tiene créditos reservados en el trial actual.";
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: { code: "trial_job_not_reserved", message },
    }), { status: 409 })));
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    wrap(<JobDetail job={job} />);
    fireEvent.click(screen.getByRole("button", { name: "Editar el prompt de esta escena" }));
    fireEvent.change(screen.getByPlaceholderText(/primer plano de gotas/i), { target: { value: "Una vela amarilla" } });
    fireEvent.click(screen.getByRole("button", { name: "Regenerar escena" }));
    await waitFor(() => expect(alert).toHaveBeenCalledWith(message));
  });

  it("shows expiry on reject as text", async () => {
    const message = "Finalizaron las 24 horas del trial.";
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      detail: { code: "trial_expired", message },
    }), { status: 403 })));
    wrap(<JobDetail job={job} />);
    fireEvent.click(screen.getByRole("button", { name: "Rechazar", exact: true }));
    await screen.findByText(message);
    expect(screen.queryByText(/\[object Object\]/)).not.toBeInTheDocument();
  });
});
