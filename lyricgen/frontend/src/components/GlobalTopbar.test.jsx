import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import GlobalTopbar from "./GlobalTopbar";

vi.mock("../i18n", () => ({
  useI18n: () => ({
    lang: "es",
    setLang: vi.fn(),
    t: (key) => ({
      "nav.new_batch": "Crear videos",
      "nav.settings": "Configuración",
      "nav.logout": "Cerrar sesión",
      "topbar.open_navigation": "Abrir navegación",
      "topbar.close_navigation": "Cerrar navegación",
      "topbar.search": "Buscar videos y acciones",
      "topbar.no_renders": "Sin renders activos",
      "topbar.render_one": "1 render activo",
      "topbar.renders_many": "{count} renders activos",
      "topbar.open_user_menu": "Abrir menú de usuario",
      "topbar.close_user_menu": "Cerrar menú de usuario",
      "topbar.language": "Idioma",
    })[key] || key,
  }),
}));
vi.mock("./WhatsNew/WhatsNewBell", () => ({ default: () => null }));
vi.mock("./HelpCenter/HelpButton", () => ({ default: () => null }));

afterEach(cleanup);

describe("GlobalTopbar navigation contracts", () => {
  it("uses the safe navigation callback for settings", () => {
    const onNavigate = vi.fn();
    render(<GlobalTopbar user={{ username: "ana", role: "operator" }} onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole("button", { name: "Abrir menú de usuario" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Configuración" }));
    expect(onNavigate).toHaveBeenCalledWith("settings");
  });

  it("keeps the create action named and delegates new-batch semantics", () => {
    const onCreate = vi.fn();
    render(<GlobalTopbar user={{ username: "ana" }} onCreate={onCreate} />);
    const create = screen.getByRole("button", { name: "Crear videos" });
    fireEvent.click(create);
    expect(onCreate).toHaveBeenCalledOnce();
  });
});
