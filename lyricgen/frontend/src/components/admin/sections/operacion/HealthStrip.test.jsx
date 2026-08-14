import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import HealthStrip from "./HealthStrip";

afterEach(cleanup);

const HEALTHY = {
  status: "ok",
  db: "up",
  redis: "up",
  r2: "up",
  workers_alive: 2,
  queue_depth: { enterprise: 0, default: 0 },
  disk_free_gb: 100,
  api_keys: { openai: true, vertex: true },
};

describe("HealthStrip live health semantics", () => {
  it("does not degrade infrastructure for historical monthly errors", () => {
    render(<HealthStrip health={HEALTHY} issueCount={12} stuckCount={0} />);
    expect(screen.getByText("Sistema Operativo")).toBeInTheDocument();
    expect(screen.getByText(/12 errores del mes/)).toBeInTheDocument();
    expect(screen.queryByText("Sistema Degradado")).not.toBeInTheDocument();
  });

  it("does degrade for jobs that are stuck now", () => {
    render(<HealthStrip health={HEALTHY} issueCount={0} stuckCount={2} />);
    expect(screen.getByText("Sistema Degradado")).toBeInTheDocument();
    expect(screen.getByText(/2 jobs atascados ahora/)).toBeInTheDocument();
  });
});
