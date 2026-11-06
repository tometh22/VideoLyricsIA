import { describe, expect, it } from "vitest";
import { campaignGroupKey, campaignPending, campaignShortName, groupCampaigns, sortByUrgency } from "./campaignGroups";

describe("campaign groups", () => {
  it("takes the lot from the first segment of the name, whatever dash the operator typed", () => {
    expect(campaignGroupKey("UMG Agosto 2026 — Rata Blanca — Vol 1")).toBe("UMG Agosto 2026");
    expect(campaignGroupKey("UMG Agosto 2026 - canary 10 - etapa 1")).toBe("UMG Agosto 2026");
    expect(campaignGroupKey("UMG Agosto 2026 – Art Tracks")).toBe("UMG Agosto 2026");
    expect(campaignGroupKey("Gate 4-6 disposable verification 2026-09-03")).toBeNull();
    expect(campaignGroupKey("Sin lote")).toBeNull();
    expect(campaignShortName("UMG Agosto 2026 — Rata Blanca — Vol 1", "UMG Agosto 2026")).toBe("Rata Blanca — Vol 1");
    expect(campaignShortName("UMG Chile — Octubre", null)).toBe("UMG Chile — Octubre");
  });

  it("does not open a lot for a prefix used once, nor count cancelled campaigns toward one", () => {
    const groups = groupCampaigns([
      { id: "a", name: "UMG Chile — Octubre", status: "active" },
      { id: "b", name: "UMG Chile — Septiembre", status: "completed" },
      { id: "c", name: "QA 1.1.17 — algo", status: "completed" },
      { id: "d", name: "QA 1.1.17 — otro", status: "cancelled" },
    ]);
    expect(groups.map((group) => [group.key, group.campaigns.map((campaign) => campaign.id)])).toEqual([
      ["UMG Chile", ["a", "b"]], ["Otras campañas", ["c"]], ["Canceladas", ["d"]],
    ]);
    expect(groupCampaigns([{ id: "d", name: "QA 1.1.17 — otro", status: "cancelled" }, { id: "e", name: "QA 1.1.17 — más", status: "cancelled" }], { separateCancelled: false })
      .map((group) => group.key)).toEqual(["QA 1.1.17"]);
  });

  it("lists live campaigns first inside a lot", () => {
    const [group] = groupCampaigns([
      { id: "new", name: "L — nuevo", status: "completed", updated_at: "2026-10-20T00:00:00Z" },
      { id: "live", name: "L — vivo", status: "active", updated_at: "2026-10-01T00:00:00Z" },
    ]);
    expect(group.campaigns.map((campaign) => campaign.id)).toEqual(["live", "new"]);
  });

  it("ranks client change requests before review work and ignores cancelled or merely processing campaigns", () => {
    const old = "2026-10-01T00:00:00Z";
    const recent = "2026-10-20T00:00:00Z";
    const campaigns = [
      { id: "ready", status: "active", pipeline: { counts: { ready: 3 } } },
      { id: "lyrics", status: "active", pipeline: { counts: { lyrics: 1 } } },
      { id: "recent", status: "completed", pipeline: { counts: { delivered: 3 }, flags: { change_requests_open: 1, oldest_change_request_at: recent } } },
      { id: "old", status: "active", pipeline: { counts: { delivered: 3 }, flags: { change_requests_open: 4, oldest_change_request_at: old } } },
      { id: "busy", status: "active", pipeline: { counts: { rendering: 2 } } },
      { id: "dead", status: "cancelled", pipeline: { counts: { lyrics: 9 } } },
    ];
    expect(sortByUrgency(campaigns).map((campaign) => campaign.id)).toEqual(["old", "recent", "lyrics", "ready"]);
    expect(campaignPending(campaigns[4])).toBeNull();
    expect(campaignPending(campaigns[5])).toBeNull();
  });
});
