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

  it("muestra trabajo real sin nombrar ni enlazar dónde está publicado", () => {
    renderLanding();

    const reel = document.querySelector("#videos");
    expect(within(reel).getAllByText("Charly García").length).toBeGreaterThan(0);
    expect(document.body.textContent).not.toMatch(/HitsDeSiempre|VEVO/i);
    expect(document.querySelectorAll('a[href*="youtube"]')).toHaveLength(0);
  });

  it("ofrece autogestión a artistas y sellos, y producción gestionada a sellos", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    renderLanding({ onLogin });

    const paths = document.querySelector("#para-quien");
    expect(within(paths).getByText("Genly Platform")).toBeInTheDocument();
    expect(within(paths).getByText("Genly Studio")).toBeInTheDocument();
    expect(within(paths).getByText("Para artistas y sellos")).toBeInTheDocument();
    expect(within(paths).getByRole("heading", { level: 3, name: /lo hacemos nosotros/i })).toBeInTheDocument();
    await user.click(within(paths).getByRole("button", { name: /empezar gratis/i }));
    expect(onLogin).toHaveBeenCalledTimes(1);

    const mode = screen.getByRole("combobox", { name: /cómo querés trabajar/i });
    expect(mode).toHaveValue("managed");
    await user.click(within(paths).getByRole("link", { name: /workspace del equipo/i }));
    expect(mode).toHaveValue("team");
    expect(screen.getByRole("button", { name: /quiero el workspace para mi equipo/i })).toBeInTheDocument();
    await user.click(within(paths).getByRole("link", { name: /pedir mi muestra gratis/i }));
    expect(mode).toHaveValue("managed");
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

    await user.click(screen.getAllByRole("button", { name: /empezar gratis/i })[0]);
    expect(onLogin).toHaveBeenCalledTimes(1);
  });
});
