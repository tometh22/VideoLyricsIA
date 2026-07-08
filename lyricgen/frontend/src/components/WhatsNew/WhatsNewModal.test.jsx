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
  visual: "transcription",
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

describe("WhatsNewModal", () => {
  it("no renderiza nada sin modalEntry", () => {
    const { container } = setup(null);
    expect(container).toBeEmptyDOMElement();
  });

  it("no renderiza nada sin user", () => {
    const { container } = setup({}, { user: null });
    expect(container).toBeEmptyDOMElement();
  });

  it("muestra visual, título, tagline y CTA, pero no highlights ni body", () => {
    setup();
    expect(screen.getByText("release.visual.audio")).toBeInTheDocument();
    expect(screen.getByText("announce.motor2_title")).toBeInTheDocument();
    expect(screen.getByText("announce.motor2_tagline")).toBeInTheDocument();
    expect(screen.getByText("announce.motor2_cta")).toBeInTheDocument();
    expect(screen.queryByText("announce.motor2_hl1")).not.toBeInTheDocument();
    expect(screen.queryByText("announce.motor2_hl2")).not.toBeInTheDocument();
    expect(screen.queryByText("announce.motor2_body")).not.toBeInTheDocument();
  });

  it("renderiza el visual de Abc cuando la entrada es textcase", () => {
    setup({ visual: "textcase" });
    expect(screen.getByText("release.visual.typography")).toBeInTheDocument();
    expect(screen.getByText("Abc")).toBeInTheDocument();
  });

  it("renderiza video si visual=media y la entrada trae mp4", () => {
    const { container } = setup({ visual: "media", media: "/escenas_demo.mp4" });
    const video = container.querySelector("video");
    expect(video).toBeInTheDocument();
    expect(video).toHaveAttribute("src", "/escenas_demo.mp4");
  });

  it("el botón de cierre marca dismissModal sin navegar", () => {
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

  it("sin ctaTo no muestra botón de acción", () => {
    setup({ ctaTo: undefined, ctaKey: undefined });
    expect(screen.queryByText("announce.motor2_cta")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "common.cancel" })).toBeInTheDocument();
  });
});
