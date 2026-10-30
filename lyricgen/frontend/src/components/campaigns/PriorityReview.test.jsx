import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import PriorityReview, { RICARDO_PRIORITY } from "./PriorityReview";

describe("Ricardo's priority review list", () => {
  it("lists every audited song, distinguishes unresolved listening from saved edits, and opens the exact job", () => {
    const onOpen = vi.fn();
    render(<PriorityReview songs={[{ job_id: "49c027206392", stage: "ready" }, { job_id: "c72601f10898", stage: "lyrics" }]} onOpen={onOpen} />);
    expect(RICARDO_PRIORITY).toHaveLength(17);
    expect(screen.getAllByRole("button", { name: "Abrir letra" })).toHaveLength(17);
    expect(screen.getByText(/«son capaz»/).parentElement).toHaveTextContent("Señal para cotejo manual");
    expect(screen.getByText(/Tildes de «verás»/).parentElement).toHaveTextContent("Corrección guardada · pendiente de revalidación");
    fireEvent.click(screen.getAllByRole("button", { name: "Abrir letra" })[0]);
    expect(onOpen).toHaveBeenCalledWith("49c027206392");
  });
});
