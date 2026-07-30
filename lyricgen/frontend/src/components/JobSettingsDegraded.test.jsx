/**
 * La ficha del video avisa cuando la animación pedida no se pudo hacer.
 *
 * Antes: el operador elegía "Foto animada", Veo fallaba, el pipeline caía al
 * zoom lento con un `logger.warning` y el job terminaba `done`. Pagabas la
 * animación, recibías otra cosa, y no había ninguna forma de enterarse — ni en
 * el job, ni en la ficha, ni en Sentry.
 *
 * El backend ahora persiste `render_params.bg_animation_degraded`. Estos tests
 * cubren que el dato efectivamente LLEGUE a la pantalla: guardar el flag y no
 * mostrarlo deja el problema igual de invisible.
 */
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import JobSettingsCard from "./JobSettingsCard";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key) => key, lang: "es" }),
}));

afterEach(cleanup);

describe("ficha del video: la animación degradada se avisa", () => {
  it("muestra el aviso cuando bg_animation_degraded es true", () => {
    render(<JobSettingsCard renderParams={{ bg_animation_degraded: true }} />);
    expect(screen.getByTestId("bg-animation-degraded")).toBeTruthy();
  });

  it("el aviso se ve SIN desplegar la ficha", () => {
    render(<JobSettingsCard renderParams={{ bg_animation_degraded: true }} />);
    // El cuerpo plegable no está montado...
    expect(screen.queryByTestId("job-settings-body")).toBeNull();
    // ...y aun así el aviso es visible: es algo que le pasó a su material, no
    // un ajuste más que haya que ir a buscar.
    expect(screen.getByTestId("bg-animation-degraded")).toBeTruthy();
  });

  it("un job sin ajustes pero CON degradación igual renderiza la ficha", () => {
    // El guard de "todo en Auto → no vale un panel" no puede tragarse el aviso.
    const { container } = render(
      <JobSettingsCard renderParams={{ bg_animation_degraded: true }} />,
    );
    expect(container.firstChild).not.toBeNull();
  });

  it("no muestra nada cuando la animación salió bien", () => {
    render(<JobSettingsCard renderParams={{ movement_style: "estatico", bg_animation_degraded: false }} />);
    expect(screen.queryByTestId("bg-animation-degraded")).toBeNull();
  });

  it("no muestra nada en un job que nunca pidió animar (clave ausente)", () => {
    render(<JobSettingsCard renderParams={{ movement_style: "estatico" }} />);
    expect(screen.queryByTestId("bg-animation-degraded")).toBeNull();
  });

  it("no confunde un valor truthy que no sea true estricto", () => {
    // Defensa contra un `"false"` string llegando del JSON de la API.
    render(<JobSettingsCard renderParams={{ movement_style: "estatico", bg_animation_degraded: "false" }} />);
    expect(screen.queryByTestId("bg-animation-degraded")).toBeNull();
  });
});
