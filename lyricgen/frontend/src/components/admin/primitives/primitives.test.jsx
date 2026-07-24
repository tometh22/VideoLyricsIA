/**
 * Component tests for the Admin Panel v2 primitives.
 *
 * These primitives replace the ~6 hand-rolled tables and the inline
 * StatCards of the old 2.100-line AdminPanel monolith. The contract this
 * file pins:
 *   - DataTable: one row per item, render fns applied, loading/empty
 *     branches, row click, single-row expansion, dense typography.
 *   - KpiCard: value/label/hint, tone → text color, loading skeleton.
 *   - StatusBadge: known status → label+accent classes; unknown →
 *     raw string + neutral classes; custom map override (invoices).
 *   - FilterBar.{Chips,Toggle,Search,Select}: the onChange/onSubmit
 *     callbacks fire with the right argument; active chip is highlighted.
 *   - EmptyState: title + message render.
 *   - adminApi pure helpers: fmtDuration / fmtMoney / reworkTotal /
 *     hasActivity edge cases.
 *
 * All six primitives are pure presentational — no fetch, no timers — so
 * nothing is mocked. localStorage is already stubbed by test-setup.js;
 * none of these components touch authHeaders, so no token is needed.
 */
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";

import DataTable from "./DataTable";
import KpiCard from "./KpiCard";
import FilterBar from "./FilterBar";
import StatusBadge from "./StatusBadge";
import EmptyState from "./EmptyState";
import { INVOICE_STATUS, fmtDuration, fmtMoney, reworkTotal, hasActivity } from "../adminApi";

afterEach(() => {
  cleanup();
});

describe("DataTable", () => {
  const columns = [
    { key: "name", header: "Nombre", render: (row) => <span>{row.name.toUpperCase()}</span> },
    { key: "count", header: "Cantidad" }, // no render fn → row[col.key]
  ];
  const rows = [
    { id: "a", name: "ana", count: 3 },
    { id: "b", name: "beto", count: 7 },
  ];

  it("renders one row per item with column render fns applied", () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} />);
    // render fn uppercased the name
    expect(screen.getByText("ANA")).toBeInTheDocument();
    expect(screen.getByText("BETO")).toBeInTheDocument();
    // no-render column falls back to row[col.key]
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    // exactly two data rows in the tbody
    const bodyRows = document.querySelectorAll("tbody tr");
    expect(bodyRows.length).toBe(2);
  });

  it("shows the skeleton (no rows) when loading=true", () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} loading />);
    // skeleton renders no table at all
    expect(document.querySelector("table")).toBeNull();
    expect(screen.queryByText("ANA")).not.toBeInTheDocument();
  });

  it("shows the empty node when rows=[]", () => {
    render(
      <DataTable
        columns={columns}
        rows={[]}
        rowKey={(r) => r.id}
        empty={<div>Sin datos</div>}
      />
    );
    expect(screen.getByText("Sin datos")).toBeInTheDocument();
    expect(document.querySelector("table")).toBeNull();
  });

  it("fires onRowClick with the clicked row", () => {
    const onRowClick = vi.fn();
    render(
      <DataTable columns={columns} rows={rows} rowKey={(r) => r.id} onRowClick={onRowClick} />
    );
    fireEvent.click(screen.getByText("BETO"));
    expect(onRowClick).toHaveBeenCalledTimes(1);
    expect(onRowClick).toHaveBeenCalledWith(rows[1]);
  });

  it("shows expanded content only for the row matching expandedKey", () => {
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.id}
        expandedKey="b"
        renderExpanded={(row) => <div>detalle de {row.name}</div>}
      />
    );
    expect(screen.getByText("detalle de beto")).toBeInTheDocument();
    expect(screen.queryByText("detalle de ana")).not.toBeInTheDocument();
  });

  it("applies the text-label class when dense", () => {
    render(<DataTable columns={columns} rows={rows} rowKey={(r) => r.id} dense />);
    const table = document.querySelector("table");
    expect(table.className).toContain("text-label");
    expect(table.className).not.toContain("text-caption");
  });
});

describe("KpiCard", () => {
  it("shows the value and label", () => {
    render(<KpiCard value="68" label="Videos creados" />);
    expect(screen.getByText("68")).toBeInTheDocument();
    expect(screen.getByText("Videos creados")).toBeInTheDocument();
  });

  it("renders the hint when provided", () => {
    render(<KpiCard value="68" label="Videos" hint="últimos 30 días" />);
    expect(screen.getByText("últimos 30 días")).toBeInTheDocument();
  });

  it("applies text-red-400 when tone='danger'", () => {
    render(<KpiCard value="3" label="Errores" tone="danger" />);
    const valueEl = screen.getByText("3");
    expect(valueEl.className).toContain("text-red-400");
  });

  it("shows a skeleton instead of the value when loading", () => {
    render(<KpiCard value="68" label="Videos" loading />);
    expect(screen.queryByText("68")).not.toBeInTheDocument();
    // label still renders alongside the skeleton
    expect(screen.getByText("Videos")).toBeInTheDocument();
  });
});

describe("StatusBadge", () => {
  it("shows 'Listo' with accent classes for status='done' (default map)", () => {
    render(<StatusBadge status="done" />);
    const badge = screen.getByText("Listo");
    expect(badge.className).toContain("text-accent");
    expect(badge.className).toContain("bg-accent/10");
  });

  it("falls back to the raw status string with neutral classes for unknown status", () => {
    render(<StatusBadge status="some_weird_status" />);
    const badge = screen.getByText("some_weird_status");
    expect(badge.className).toContain("text-gray-400");
    expect(badge.className).not.toContain("text-accent");
  });

  it("honours a custom map override (INVOICE_STATUS paid → 'Pagada')", () => {
    render(<StatusBadge status="paid" map={INVOICE_STATUS} />);
    const badge = screen.getByText("Pagada");
    expect(badge.className).toContain("text-accent");
  });
});

describe("FilterBar", () => {
  describe("Chips", () => {
    const options = [
      { id: 7, label: "7d" },
      { id: 30, label: "30d" },
    ];

    it("calls onChange with the option id when clicked", () => {
      const onChange = vi.fn();
      render(<FilterBar.Chips value={7} onChange={onChange} options={options} />);
      fireEvent.click(screen.getByText("30d"));
      expect(onChange).toHaveBeenCalledWith(30);
    });

    it("applies the active classes to the selected option", () => {
      render(<FilterBar.Chips value={7} onChange={() => {}} options={options} />);
      const active = screen.getByText("7d");
      const inactive = screen.getByText("30d");
      expect(active.className).toContain("bg-brand/20");
      expect(inactive.className).not.toContain("bg-brand/20");
    });
  });

  describe("Toggle", () => {
    it("calls onChange with the new boolean when clicked", () => {
      const onChange = vi.fn();
      render(<FilterBar.Toggle checked={false} onChange={onChange} label="Ocultar inactivos" />);
      fireEvent.click(screen.getByRole("checkbox"));
      expect(onChange).toHaveBeenCalledWith(true);
    });
  });

  describe("Search", () => {
    it("calls onChange when typing", () => {
      const onChange = vi.fn();
      render(<FilterBar.Search value="" onChange={onChange} onSubmit={() => {}} />);
      fireEvent.change(screen.getByRole("textbox"), { target: { value: "ana" } });
      expect(onChange).toHaveBeenCalledWith("ana");
    });

    it("calls onSubmit when Enter is pressed", () => {
      const onSubmit = vi.fn();
      render(<FilterBar.Search value="ana" onChange={() => {}} onSubmit={onSubmit} />);
      fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter" });
      expect(onSubmit).toHaveBeenCalledTimes(1);
    });
  });

  describe("Select", () => {
    const options = [
      { id: "all", label: "Todos" },
      { id: "active", label: "Activos" },
    ];

    it("calls onChange with the selected value", () => {
      const onChange = vi.fn();
      render(<FilterBar.Select value="all" onChange={onChange} options={options} />);
      fireEvent.change(screen.getByRole("combobox"), { target: { value: "active" } });
      expect(onChange).toHaveBeenCalledWith("active");
    });
  });
});

describe("EmptyState", () => {
  it("renders the title and message", () => {
    render(<EmptyState title="Sin facturas" message="Todavía no hay facturas." />);
    expect(screen.getByText("Sin facturas")).toBeInTheDocument();
    expect(screen.getByText("Todavía no hay facturas.")).toBeInTheDocument();
  });
});

describe("adminApi helpers", () => {
  it("fmtDuration formats minutes and hours", () => {
    expect(fmtDuration(0)).toBe("0m");
    expect(fmtDuration(7440)).toBe("2h 4m");
  });

  it("fmtMoney formats USD with two decimals", () => {
    expect(fmtMoney(3.5)).toBe("$3.50");
  });

  it("reworkTotal sums rework signals, 0 for null", () => {
    expect(
      reworkTotal({ variants: 1, total_edits: 2, retries: 1, corrected_jobs: 1, abandoned_recreated: 0 })
    ).toBe(5);
    expect(reworkTotal(null)).toBe(0);
  });

  it("hasActivity reflects videos created or rework done", () => {
    expect(hasActivity({ videos: { total: 0 }, rework: {} })).toBe(false);
    expect(hasActivity({ videos: { total: 3 }, rework: {} })).toBe(true);
  });
});
