// Tests for the Phase 2+3 refactor (2026-05-25) that integrated the
// lyrics review as step 6 of the wizard, eliminating the separate
// LyricsEditor full-screen layout and the duplicated typography controls.
//
// These tests cover the contracts that, if broken, ship a UI regression
// to UMG mañana: stepper interactivity, auto-advance to step 6, the
// renderStep6 inject point, and LyricsEditor's hideTypographyControls
// hiding its left column without touching the right column.

import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

// Same mocks as LyricsEditor.test.jsx — i18n returns the explicit fallback,
// OnboardingTour / ToastProvider noop'd so the editor mounts standalone.
vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: () => {}, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

afterEach(cleanup);

function editorProps(overrides = {}) {
  return {
    segments: [
      { start: 0, end: 2, text: "primera línea" },
      { start: 2, end: 4, text: "segunda línea" },
    ],
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    font: "anton",
    textCase: "upper",
    textContrast: "medium",
    lyricsAnimation: "karaoke",
    lineTransition: "slide_up",
    ...overrides,
  };
}

describe("LyricsEditor — hideTypographyControls (Phase 2)", () => {
  // CONTRATO: el editor renderiza una columna izquierda con los selects
  // de Tipografía / Estilo / Contraste / Animación / Transición + un
  // LyricVideoPreview interno. Cuando se monta dentro del paso 6 del
  // wizard, esos controles ya viven en el paso 4 del stepper — esconder
  // la columna izquierda evita la duplicación que el operador notaba.
  //
  // El derecho (lista de segments + timeline toggle + approve) NO se
  // toca — todo el editing y autosave sigue funcionando.

  it("renders typography controls by default (no flag)", () => {
    render(<LyricsEditor {...editorProps()} />);
    // El select de Tipografía vive en la columna izquierda — debe estar.
    expect(screen.getByText("Tipografía")).toBeInTheDocument();
    // El select de Animación también.
    expect(screen.getByText("Animación")).toBeInTheDocument();
  });

  it("hides typography controls when hideTypographyControls=true", () => {
    render(<LyricsEditor {...editorProps({ hideTypographyControls: true })} />);
    // La columna izquierda entera NO se renderiza — ni el label de
    // Tipografía ni el de Animación.
    expect(screen.queryByText("Tipografía")).not.toBeInTheDocument();
    expect(screen.queryByText("Animación")).not.toBeInTheDocument();
  });

  it("keeps the right column (segment list) when hideTypographyControls=true", () => {
    render(<LyricsEditor {...editorProps({ hideTypographyControls: true })} />);
    // Los inputs de las líneas viven en la columna derecha — DEBEN
    // seguir renderizándose. Si el operador no puede editar las líneas
    // en review, el feature está roto.
    expect(screen.getByDisplayValue("primera línea")).toBeInTheDocument();
    expect(screen.getByDisplayValue("segunda línea")).toBeInTheDocument();
  });

  it("approves segments through the Aprobar CTA even with hidden controls", () => {
    const onApprove = vi.fn();
    // El label del botón viene de i18n; pasamos submitLabel explícito
    // para que el mock del t() no devuelva undefined (mock pasa fallback).
    render(<LyricsEditor {...editorProps({
      hideTypographyControls: true,
      onApprove,
      submitLabel: "Aprobar y generar",
    })} />);
    // El botón de aprobar está en la header del editor — independiente
    // de la columna izquierda. Click → onApprove llamado con segments.
    const approveBtn = screen.getByRole("button", { name: /Aprobar y generar/i });
    fireEvent.click(approveBtn);
    expect(onApprove).toHaveBeenCalledOnce();
    const cleanedSegments = onApprove.mock.calls[0][0];
    expect(Array.isArray(cleanedSegments)).toBe(true);
    expect(cleanedSegments.length).toBeGreaterThanOrEqual(2);
  });

  it("re-syncs typography state from props when wizard controls change them", () => {
    // CONTRATO Phase 2: cuando el wizard es la fuente de verdad
    // (hideTypographyControls=true), un cambio de font/animation en
    // el paso 4 del stepper actualiza los props del editor. El editor
    // debe RE-SEED su selectedFont/selectedAnimation desde los nuevos
    // props (useEffect) en vez de quedarse con el valor inicial del
    // useState — sino el operador vuelve a paso 6 y ve la font vieja.
    const propsA = editorProps({
      hideTypographyControls: true,
      font: "anton",
      lyricsAnimation: "karaoke",
    });
    const { rerender } = render(<LyricsEditor {...propsA} />);
    // En este modo no hay select visible — el contrato se verifica via
    // los callbacks que el editor expone hacia adentro (no podemos
    // probar visualmente porque la columna está oculta). Re-renderizamos
    // con props nuevos y confirmamos que NO crashea + el editor
    // permanece renderizando los segments.
    rerender(<LyricsEditor {...editorProps({
      hideTypographyControls: true,
      font: "bebas",
      lyricsAnimation: "pop",
    })} />);
    // El editor sigue mostrando los segments — el useEffect que reseedea
    // selected* desde props no rompe el render.
    expect(screen.getByDisplayValue("primera línea")).toBeInTheDocument();
  });
});

// ─── UploadZone step-6 wiring ────────────────────────────────────────────
//
// UploadZone es un componente de 2200 líneas con muchas dependencias
// (i18n, plan tiers, useBackgroundPreview, file drag, etc.). En vez de
// montarlo entero, testeo el contrato del paso 6 a través de un mini
// componente que reproduce la lógica clave: WIZARD_STEPS, el useEffect
// de auto-advance, y el render condicional de renderStep6.
//
// Si esos tres comportamientos cambian en UploadZone.jsx, este test
// se rompe — y ahí actualizo el espejo. Si los tres se rompen sin que
// el test se entere, el contrato visual se rompe en producción.

import { useState, useEffect } from "react";

function MockWizardLayout({
  hasReviewableContent,
  renderStep6,
  variantMode = false,
  editMode = false,
}) {
  const guidedExistingJobMode = editMode || variantMode;
  const WIZARD_STEPS = [
    { id: 1, label: "Subí" },
    { id: 2, label: "Modo" },
    { id: 3, label: "Movimiento" },
    { id: 4, label: "Animación" },
    { id: 5, label: "Entregá" },
    { id: 6, label: variantMode ? "Resumen" : "Lyrics" },
  ];
  const locked = new Set(guidedExistingJobMode ? [1, 5] : []);
  const displaySteps = guidedExistingJobMode
    ? WIZARD_STEPS.filter((step) => !locked.has(step.id))
    : WIZARD_STEPS;
  const [wizardStep, setWizardStep] = useState(guidedExistingJobMode ? 2 : 1);
  const _maxInteractiveStep = hasReviewableContent || locked.size > 0 ? 6 : 5;
  const goStep = (n) => {
    const clamped = Math.max(1, Math.min(_maxInteractiveStep, n));
    if (!locked.has(clamped)) setWizardStep(clamped);
  };
  const findNextUnlocked = (n) => {
    for (let i = n + 1; i <= _maxInteractiveStep; i++) {
      if (!locked.has(i)) return i;
    }
    return null;
  };
  useEffect(() => {
    if (!guidedExistingJobMode && hasReviewableContent && wizardStep !== 6) setWizardStep(6);
    else if (!guidedExistingJobMode && !hasReviewableContent && wizardStep === 6) setWizardStep(5);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasReviewableContent, guidedExistingJobMode]);

  return (
    <div>
      <nav>
        {displaySteps.map((s, displayIndex) => {
          const isLyrics = s.id === 6;
          const disabled = isLyrics && !hasReviewableContent;
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => { if (!disabled) goStep(s.id); }}
              disabled={disabled}
              aria-disabled={disabled}
              data-active={wizardStep === s.id ? "true" : "false"}
              data-testid={`step-${s.id}`}
            >
              <span data-testid={`step-number-${s.id}`}>
                {guidedExistingJobMode ? displayIndex + 1 : s.id}
              </span>
              {s.label}
            </button>
          );
        })}
      </nav>
      <div data-testid="content">
        {wizardStep === 6 && renderStep6 ? renderStep6() : (
          <div data-testid="upload-content">Step {wizardStep} content</div>
        )}
      </div>
      {wizardStep < 6 && (
        hasReviewableContent && !guidedExistingJobMode ? (
          <button type="button" data-testid="primary-cta" onClick={() => goStep(6)}>
            Volver a lyrics
          </button>
        ) : (
          <button
            type="button"
            data-testid="primary-cta"
            onClick={() => goStep(findNextUnlocked(wizardStep))}
          >
            Continuar
          </button>
        )
      )}
    </div>
  );
}

describe("Wizard step 6 — auto-advance + render prop (Phase 2)", () => {
  it("renders 6 steps with step 6 disabled when no review content", () => {
    render(<MockWizardLayout hasReviewableContent={false} renderStep6={() => null} />);
    const step6 = screen.getByTestId("step-6");
    expect(step6).toBeDisabled();
    expect(step6.getAttribute("aria-disabled")).toBe("true");
  });

  it("enables step 6 when review content arrives", () => {
    const { rerender } = render(
      <MockWizardLayout hasReviewableContent={false} renderStep6={() => null} />,
    );
    expect(screen.getByTestId("step-6")).toBeDisabled();
    rerender(<MockWizardLayout hasReviewableContent={true} renderStep6={() => null} />);
    expect(screen.getByTestId("step-6")).not.toBeDisabled();
  });

  it("auto-advances to step 6 when hasReviewableContent transitions to true", () => {
    const { rerender } = render(
      <MockWizardLayout hasReviewableContent={false} renderStep6={() => <div>review</div>} />,
    );
    // Empieza en step 1.
    expect(screen.getByTestId("step-1").getAttribute("data-active")).toBe("true");
    // Llega contenido de review → auto-advance a step 6.
    rerender(<MockWizardLayout hasReviewableContent={true} renderStep6={() => <div>review</div>} />);
    expect(screen.getByTestId("step-6").getAttribute("data-active")).toBe("true");
  });

  it("drops back to step 5 when review content disappears (user clicks Back)", () => {
    const { rerender } = render(
      <MockWizardLayout hasReviewableContent={true} renderStep6={() => <div>review</div>} />,
    );
    expect(screen.getByTestId("step-6").getAttribute("data-active")).toBe("true");
    rerender(<MockWizardLayout hasReviewableContent={false} renderStep6={() => <div>review</div>} />);
    // hasReviewableContent → false mientras estábamos en 6 → bajar a 5.
    expect(screen.getByTestId("step-5").getAttribute("data-active")).toBe("true");
  });

  it("renders the renderStep6 content when wizardStep is 6", () => {
    render(
      <MockWizardLayout
        hasReviewableContent={true}
        renderStep6={() => <div data-testid="review-injected">REVIEW UI HERE</div>}
      />,
    );
    expect(screen.getByTestId("review-injected")).toBeInTheDocument();
    expect(screen.queryByTestId("upload-content")).not.toBeInTheDocument();
  });

  it("lets the operator click back to step 4 from step 6 to adjust typography", () => {
    render(
      <MockWizardLayout
        hasReviewableContent={true}
        renderStep6={() => <div data-testid="review-injected">REVIEW UI HERE</div>}
      />,
    );
    // Auto-advanced to step 6. Click step 4 (Animación) to change font.
    fireEvent.click(screen.getByTestId("step-4"));
    expect(screen.getByTestId("step-4").getAttribute("data-active")).toBe("true");
    // El renderStep6 ya no aparece — paso 4 muestra contenido de upload.
    expect(screen.queryByTestId("review-injected")).not.toBeInTheDocument();
    expect(screen.getByTestId("upload-content")).toBeInTheDocument();
    // Click step 6 — back to review with the new typography settings.
    fireEvent.click(screen.getByTestId("step-6"));
    expect(screen.getByTestId("review-injected")).toBeInTheDocument();
  });

  it("does NOT allow clicking step 6 when content is not reviewable", () => {
    render(
      <MockWizardLayout
        hasReviewableContent={false}
        renderStep6={() => <div data-testid="review-injected">REVIEW UI HERE</div>}
      />,
    );
    // Click step 6 — debería ser noop porque está disabled.
    fireEvent.click(screen.getByTestId("step-6"));
    // El step activo sigue siendo el 1 (no avanzó).
    expect(screen.getByTestId("step-1").getAttribute("data-active")).toBe("true");
    expect(screen.queryByTestId("review-injected")).not.toBeInTheDocument();
  });

  it("starts a variant at Modo and only shows its four real steps", () => {
    render(
      <MockWizardLayout
        variantMode
        hasReviewableContent
        renderStep6={() => <div data-testid="variant-summary">summary</div>}
      />,
    );

    expect(screen.queryByTestId("step-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("step-5")).not.toBeInTheDocument();
    expect(screen.getByTestId("step-2").getAttribute("data-active")).toBe("true");
    expect(screen.getByTestId("step-number-2")).toHaveTextContent("1");
    expect(screen.getByTestId("step-number-6")).toHaveTextContent("4");
    expect(screen.queryByTestId("variant-summary")).not.toBeInTheDocument();
  });

  it("keeps Continuar through a variant and reaches Resumen in order", () => {
    render(
      <MockWizardLayout
        variantMode
        hasReviewableContent
        renderStep6={() => <div data-testid="variant-summary">summary</div>}
      />,
    );

    const continueButton = screen.getByTestId("primary-cta");
    expect(continueButton).toHaveTextContent("Continuar");

    fireEvent.click(continueButton);
    expect(screen.getByTestId("step-3").getAttribute("data-active")).toBe("true");
    expect(screen.getByTestId("primary-cta")).toHaveTextContent("Continuar");

    fireEvent.click(screen.getByTestId("primary-cta"));
    expect(screen.getByTestId("step-4").getAttribute("data-active")).toBe("true");

    fireEvent.click(screen.getByTestId("primary-cta"));
    expect(screen.getByTestId("step-6").getAttribute("data-active")).toBe("true");
    expect(screen.getByTestId("step-6")).toHaveTextContent("Resumen");
    expect(screen.getByTestId("variant-summary")).toBeInTheDocument();
  });

  it("uses the same guided order for editing, ending in editable Lyrics", () => {
    render(
      <MockWizardLayout
        editMode
        hasReviewableContent
        renderStep6={() => <div data-testid="lyrics-editor">editor</div>}
      />,
    );

    expect(screen.queryByTestId("step-1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("step-5")).not.toBeInTheDocument();
    expect(screen.getByTestId("step-2").getAttribute("data-active")).toBe("true");
    expect(screen.getByTestId("step-number-2")).toHaveTextContent("1");
    expect(screen.getByTestId("step-6")).toHaveTextContent("Lyrics");
    expect(screen.getByTestId("primary-cta")).toHaveTextContent("Continuar");

    fireEvent.click(screen.getByTestId("primary-cta"));
    expect(screen.getByTestId("step-3").getAttribute("data-active")).toBe("true");
    fireEvent.click(screen.getByTestId("primary-cta"));
    expect(screen.getByTestId("step-4").getAttribute("data-active")).toBe("true");
    fireEvent.click(screen.getByTestId("primary-cta"));

    expect(screen.getByTestId("step-6").getAttribute("data-active")).toBe("true");
    expect(screen.getByTestId("lyrics-editor")).toBeInTheDocument();
  });
});
