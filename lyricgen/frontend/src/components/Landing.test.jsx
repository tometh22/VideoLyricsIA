import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import Landing from "./Landing";

function renderLanding(props = {}) {
  const defaults = { onStart: vi.fn(), onLogin: vi.fn(), isLoggedIn: false };
  return render(
    <I18nProvider>
      <Landing {...defaults} {...props} />
    </I18nProvider>,
  );
}

describe("Landing", () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem("genly_lang", "es");
  });

  it("presenta el motor y los límites reales sin claims antiguos", () => {
    renderLanding();

    expect(screen.getByRole("heading", { level: 1, name: /tu canción dirige el video/i })).toBeInTheDocument();
    expect(screen.getByText("15")).toBeInTheDocument();
    expect(screen.getByText("conceptos visuales")).toBeInTheDocument();
    expect(screen.getAllByText(/hasta cinco archivos MP3 o WAV/i)).toHaveLength(2);
    expect(screen.queryByText(/menos de cinco minutos/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/sin riesgo de ContentID/i)).not.toBeInTheDocument();
  });

  it("mantiene la experiencia multilingüe y actualiza el metadata visible", async () => {
    const user = userEvent.setup();
    renderLanding();

    await user.click(screen.getAllByRole("button", { name: "en" })[0]);

    expect(screen.getByRole("heading", { level: 1, name: /your song directs the video/i })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
    expect(document.title).toMatch(/generative visual direction/i);
  });

  it("envía el CTA público al acceso existente sin cambiar rutas", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    renderLanding({ onLogin });

    await user.click(screen.getByRole("button", { name: /crear mi lyric video/i }));
    expect(onLogin).toHaveBeenCalledTimes(1);
  });
});
