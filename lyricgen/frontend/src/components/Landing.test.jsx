import { render, screen, within } from "@testing-library/react";
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

  it("presenta el producto con datos reales y sin claims antiguos", () => {
    renderLanding();

    expect(screen.getByRole("heading", { level: 1, name: /hacé el lyric video de tu canción/i })).toBeInTheDocument();
    expect(screen.getByText(/15 conceptos visuales/i)).toBeInTheDocument();
    expect(screen.getByText(/hasta cinco archivos MP3 o WAV en el mismo lote/i)).toBeInTheDocument();
    expect(screen.queryByText(/menos de cinco minutos/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/sin riesgo de ContentID/i)).not.toBeInTheDocument();
  });

  it("muestra trabajo publicado que abre la publicación original", () => {
    renderLanding();

    const reel = document.querySelector("#videos");
    const links = within(reel).getAllByRole("link").filter((link) => link.href.includes("youtube.com/watch"));
    expect(links.length).toBeGreaterThanOrEqual(12);
    links.forEach((link) => {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
    });
  });

  it("cambia el estilo de letra mostrado", async () => {
    const user = userEvent.setup();
    renderLanding();

    const pincel = screen.getByRole("button", { name: /pincel/i });
    await user.click(pincel);
    expect(pincel).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByAltText(/estilo pincel/i)).toHaveAttribute("src", "/landing/looks/pincel.jpg");
  });

  it("mantiene la experiencia multilingüe y actualiza el metadata visible", async () => {
    const user = userEvent.setup();
    renderLanding();

    await user.click(screen.getAllByRole("button", { name: "en" })[0]);

    expect(screen.getByRole("heading", { level: 1, name: /make the lyric video for your song/i })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
    expect(document.title).toMatch(/make the lyric video/i);
  });

  it("envía el CTA público al acceso existente sin cambiar rutas", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    renderLanding({ onLogin });

    await user.click(screen.getAllByRole("button", { name: /crear mi lyric video/i })[0]);
    expect(onLogin).toHaveBeenCalledTimes(1);
  });
});
