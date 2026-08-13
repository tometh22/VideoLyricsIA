import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ConflictDialog from "./ConflictDialog";

afterEach(cleanup);

// Audit 2026-08-13: the conflict dialog used to always blame "someone else"
// for a save the current user made themselves (e.g. from another tab, or a
// reconciliation triggered by a background/typography edit that carried a
// stale lyrics snapshot). Confirmed reproduced live by two different real
// accounts. These tests guard the identity-aware copy fix.

const NOOP = () => {};

describe("ConflictDialog attribution copy", () => {
  it("blames a real other teammate by name when updatedBy differs from the viewer", () => {
    render(
      <ConflictDialog
        conflict={{ serverRevision: 147, updatedBy: { id: 21, username: "sebastian.vargas@umusic.com" } }}
        currentUserId={15}
        onUseServer={NOOP}
        onSaveLocal={NOOP}
        onCancel={NOOP}
      />,
    );
    expect(screen.getByText(/sebastian\.vargas@umusic\.com guardó cambios/)).toBeTruthy();
    expect(screen.queryByText(/Guardaste cambios/)).toBeNull();
  });

  it("does NOT claim 'someone else' saved when updatedBy is the current viewer", () => {
    render(
      <ConflictDialog
        conflict={{ serverRevision: 148, updatedBy: { id: 15, username: "tomas@epical.digital" } }}
        currentUserId={15}
        onUseServer={NOOP}
        onSaveLocal={NOOP}
        onCancel={NOOP}
      />,
    );
    expect(screen.getByText(/Guardaste cambios desde otra pestaña/)).toBeTruthy();
    expect(screen.queryByText(/tomas@epical\.digital guardó cambios/)).toBeNull();
  });

  it("falls back to a generic message when updatedBy is unknown", () => {
    render(
      <ConflictDialog
        conflict={{ serverRevision: 3, updatedBy: null }}
        currentUserId={15}
        onUseServer={NOOP}
        onSaveLocal={NOOP}
        onCancel={NOOP}
      />,
    );
    expect(screen.getByText(/Otro integrante guardó cambios/)).toBeTruthy();
  });

  it("treats string/number id mismatches (e.g. JSON round-trip) as equal", () => {
    render(
      <ConflictDialog
        conflict={{ serverRevision: 9, updatedBy: { id: "15", username: "tomas@epical.digital" } }}
        currentUserId={15}
        onUseServer={NOOP}
        onSaveLocal={NOOP}
        onCancel={NOOP}
      />,
    );
    expect(screen.getByText(/Guardaste cambios desde otra pestaña/)).toBeTruthy();
  });

  it("renders nothing when there is no conflict", () => {
    const { container } = render(
      <ConflictDialog conflict={null} currentUserId={15} onUseServer={NOOP} onSaveLocal={NOOP} onCancel={NOOP} />,
    );
    expect(container.firstChild).toBeNull();
  });
});
