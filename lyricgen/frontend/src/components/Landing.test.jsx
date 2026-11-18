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

  it("muestra en el hero el loop de estilos nuevos, en autoplay y mudo", () => {
    renderLanding();

    const video = document.querySelector(".hm-feature video");
    expect(video).toHaveAttribute("src", "/landing/hero/estilos-genly.mp4");
    expect(video).toHaveAttribute("poster", "/landing/hero/estilos-genly.jpg");
    expect(video).toHaveAttribute("aria-label", "Fragmentos de lyric videos con ocho estilos de letra distintos.");
    expect(video).toHaveAttribute("width", "1280");
    expect(video).toHaveAttribute("height", "720");
    expect(video.autoplay).toBe(true);
    expect(video.muted).toBe(true);
    expect(video.loop).toBe(true);
    const caption = document.querySelector(".hm-feature figcaption");
    expect(within(caption).getByText("Estilos nuevos").tagName).toBe("STRONG");
    expect(caption).toHaveTextContent("Arco, Recorte, Perspectiva y más");
    expect(caption).toHaveTextContent("Lyric videos · Fragmentos");
    expect(document.body.textContent).not.toMatch(/pincel/i);
  });

  it("con movimiento reducido el hero no arranca solo y queda el poster", () => {
    const original = window.matchMedia;
    window.matchMedia = vi.fn().mockImplementation((query) => ({
      matches: query.includes("prefers-reduced-motion"),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    try {
      renderLanding();
      const video = document.querySelector(".hm-feature video");
      expect(video.autoplay).toBe(false);
      expect(video).toHaveAttribute("poster", "/landing/hero/estilos-genly.jpg");
    } finally {
      window.matchMedia = original;
    }
  });

  it("ofrece los diez estilos nuevos, con Arco elegido por defecto", async () => {
    const user = userEvent.setup();
    renderLanding();

    const looks = document.querySelector("#estilos");
    expect(within(looks).getByRole("heading", { level: 2, name: "Un estilo de letra para cada canción." })).toBeInTheDocument();
    expect(within(looks).getByText("Capturas de videos recientes.")).toBeInTheDocument();
    const chips = within(within(looks).getByRole("group", { name: /estilos de letra/i })).getAllByRole("button");
    expect(chips.map((chip) => chip.textContent)).toEqual(["Arco", "Recorte", "Perspectiva 3D", "Bloque", "Romántico", "Díptico", "Duotono", "Golpe", "Cósmico", "Cine"]);
    expect(chips[0]).toHaveAttribute("aria-pressed", "true");
    expect(within(looks).getByAltText("Estilo de letra Arco")).toHaveAttribute("src", "/landing/looks/arco.jpg");

    const perspectiva = within(looks).getByRole("button", { name: "Perspectiva 3D" });
    await user.click(perspectiva);
    expect(perspectiva).toHaveAttribute("aria-pressed", "true");
    expect(chips[0]).toHaveAttribute("aria-pressed", "false");
    expect(within(looks).getByAltText("Estilo de letra Perspectiva 3D")).toHaveAttribute("src", "/landing/looks/perspectiva.jpg");
  });

  it("muestra art tracks con el pie debajo de la imagen", () => {
    renderLanding();

    const reel = document.querySelector("#videos");
    expect(within(reel).getByRole("heading", { level: 3, name: "Y también art tracks" })).toBeInTheDocument();
    const cards = reel.querySelectorAll(".hm-art__item");
    expect(cards).toHaveLength(4);
    expect([...cards].map((card) => card.querySelector("figcaption").textContent)).toEqual(["Mon LaferteNiña", "Los BunkersComa", "KudaiDéjame Gritar", "Tiro de GraciaMelaza"]);
    expect(cards[0].querySelector("img")).toHaveAttribute("src", "/landing/art/mon-laferte-nina-800.jpg");
    // El pie va después de la imagen, no superpuesto.
    expect(cards[0].firstElementChild.tagName).toBe("IMG");
  });

  it("explica por qué un lyric video sin cifras, entre el reel y los estilos", () => {
    renderLanding();

    const heading = screen.getByRole("heading", { level: 2, name: "Por qué un lyric video" });
    const why = heading.closest("section");
    expect(why.previousElementSibling).toHaveAttribute("id", "videos");
    expect(why.nextElementSibling).toHaveAttribute("id", "estilos");
    expect(within(why).getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["Más ingresos en YouTube", "Te encuentran por la letra", "Contenido para redes"]);
    const bodies = [...why.querySelectorAll("li p")].map((p) => p.textContent).join(" ");
    expect(bodies).not.toMatch(/\d|%/);
  });

  it("promete Canvas y art tracks sólo para Genly Studio", () => {
    renderLanding();

    const [platform, studio] = document.querySelectorAll("#para-quien .hm-path");
    expect(platform.textContent).not.toMatch(/canvas|art track/i);
    const studioPoints = [...studio.querySelectorAll(".hm-checks li")].map((li) => li.textContent);
    expect(studioPoints.slice(-2)).toEqual(["También Canvas de Spotify, art tracks y entrega en ProRes", "Precio según volumen"]);
    expect(within(studio).getByRole("link", { name: /ver precios para sellos/i })).toHaveAttribute("href", "/sellos");

    expect(document.querySelector("#como-funciona").textContent).not.toMatch(/canvas/i);
    const faq = screen.getByText("¿Qué me llevo por cada canción?").closest("details");
    expect(faq).toHaveTextContent("Si producimos para tu sello con Genly Studio, también el Canvas de Spotify");
  });

  it("mantiene la experiencia multilingüe y actualiza el metadata visible", async () => {
    const user = userEvent.setup();
    renderLanding();

    await user.click(screen.getAllByRole("button", { name: "en" })[0]);

    expect(screen.getByRole("heading", { level: 1, name: /make the lyric video for your song/i })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe("en");
    expect(document.title).toMatch(/make the lyric video/i);
    expect(screen.getByRole("heading", { level: 2, name: "A lyric style for every song." })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Why a lyric video" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "And art tracks too" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Cut-out" })).toBeInTheDocument();
    expect(screen.getByAltText("Arc lyric style")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /see label pricing/i })).toHaveAttribute("href", "/sellos");
    expect(document.querySelector(".hm-feature video")).toHaveAttribute("aria-label", "Excerpts from lyric videos in eight different lyric styles.");

    await user.click(screen.getAllByRole("button", { name: "pt" })[0]);
    expect(screen.getByRole("heading", { level: 2, name: "Um estilo de letra para cada música." })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Por que um lyric video" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Duotom" })).toBeInTheDocument();
    expect(screen.getByAltText("Estilo de letra Arco")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /ver preços para gravadoras/i })).toHaveAttribute("href", "/sellos");
  });

  it("envía el CTA público al acceso existente sin cambiar rutas", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn();
    renderLanding({ onLogin });

    await user.click(screen.getAllByRole("button", { name: /empezar gratis/i })[0]);
    expect(onLogin).toHaveBeenCalledTimes(1);
  });
});
