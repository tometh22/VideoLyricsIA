// Cobertura de la revisión "world-class" (07/07): el modal es un TEASER
// (hero + título + una línea de gancho + un CTA), no la ficha técnica —
// los highlightKeys/body ya NO se renderizan acá (viven en el panel).
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import WhatsNewModal from "./WhatsNewModal";
import { useChangelog } from "./useChangelog";

afterEach(cleanup);

vi.mock("../../i18n", () => ({
  useI18n: () => ({ t: (key) => key }),
}));

const mockNavigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => mockNavigate,
}));

vi.mock("./useChangelog", () => ({
  useChangelog: vi.fn(),
}));

const BASE_ENTRY = {
  id: "motor-v2",
  titleKey: "announce.motor2_title",
  taglineKey: "announce.motor2_tagline",
  bodyKey: "announce.motor2_body",
  highlightKeys: ["announce.motor2_hl1", "announce.motor2_hl2"],
  icon: "🎯",
  ctaKey: "announce.motor2_cta",
  ctaTo: "/new",
};

function setup(entryOverrides = {}, { user = { id: 1 } } = {}) {
  const dismissModal = vi.fn();
  const entry = entryOverrides === null ? null : { ...BASE_ENTRY, ...entryOverrides };
  useChangelog.mockReturnValue({ modalEntry: entry, dismissModal });
  const utils = render(<WhatsNewModal user={user} />);
  return { ...utils, dismissModal, entry };
}

describe("WhatsNewModal — teaser, no ficha técnica", () => {
  it("no renderiza nada sin modalEntry", () => {
    const { container } = setup(null);
    expect(container).toBeEmptyDOMElement();
  });

  it("no renderiza nada sin user (aunque haya modalEntry)", () => {
    const { container } = setup({}, { user: null });
    expect(container).toBeEmptyDOMElement();
  });

  it("muestra título, tagline, badge y CTA — pero NO los highlightKeys", () => {
    setup();
    expect(screen.getByText("announce.motor2_title")).toBeInTheDocument();
    expect(screen.getByText("announce.motor2_tagline")).toBeInTheDocument();
    expect(screen.getByText("announce.scenes_badge")).toBeInTheDocument();
    expect(screen.getByText("announce.motor2_cta")).toBeInTheDocument();
    // el detalle vive en el panel, no acá — regresión clave de este diseño
    expect(screen.queryByText("announce.motor2_hl1")).not.toBeInTheDocument();
    expect(screen.queryByText("announce.motor2_hl2")).not.toBeInTheDocument();
    expect(screen.queryByText("announce.motor2_body")).not.toBeInTheDocument();
  });

  it("sin media: arma el hero de gradiente con el ícono de la entrada", () => {
    const { container } = setup({ media: undefined, icon: "🚀" });
    expect(screen.getByText("🚀")).toBeInTheDocument();
    expect(container.querySelector("video")).not.toBeInTheDocument();
    expect(container.querySelector("img")).not.toBeInTheDocument();
  });

  it("con media (.mp4): renderiza el video, no el hero de ícono", () => {
    const { container } = setup({ media: "/escenas_demo.mp4" });
    const video = container.querySelector("video");
    expect(video).toBeInTheDocument();
    expect(video).toHaveAttribute("src", "/escenas_demo.mp4");
  });

  it("con media de imagen: renderiza <img>", () => {
    const { container } = setup({ media: "/demo.png" });
    expect(container.querySelector("img")).toHaveAttribute("src", "/demo.png");
  });

  it("el botón × cierra y marca dismissModal, sin navegar", () => {
    const { dismissModal } = setup();
    fireEvent.click(screen.getByRole("button", { name: "common.cancel" }));
    expect(dismissModal).toHaveBeenCalledTimes(1);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  it("el CTA navega a ctaTo y también dismissea", () => {
    const { dismissModal } = setup({ ctaTo: "/new" });
    fireEvent.click(screen.getByText("announce.motor2_cta"));
    expect(dismissModal).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith("/new");
  });

  it("sin ctaTo: no revienta y no muestra botón de acción (solo × para cerrar)", () => {
    setup({ ctaTo: undefined, ctaKey: undefined });
    expect(screen.queryByText("announce.motor2_cta")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "common.cancel" })).toBeInTheDocument();
  });
});
