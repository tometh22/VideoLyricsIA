import { describe, expect, it } from "vitest";
import { campaignOrder, campaignOverview, inCampaignView } from "./campaignOverview";
const campaign = (counts, extra = {}) => ({ id: "chile", status: "active", pipeline: { portal_status_available: true, counts }, ...extra });
describe("campaign delivery overview", () => {
  it("does not call approved videos delivered or complete", () => {
    const row = campaignOverview(campaign({ approved: 5, delivered: 2 }));
    expect(row).toMatchObject({ total: 7, delivered: 2, pending: 5, complete: false });
    expect(row.action.href).toContain("view=approved");
  });
  it("does not report zero deliveries when the portal is unavailable or the snapshot is missing", () => {
    expect(campaignOverview(campaign({}, { pipeline: { portal_status_available: false, counts: { approved: 5 } } })).delivered).toBeNull();
    expect(campaignOverview({ id: "legacy", counters: { done: 5 } }).delivered).toBeNull();
  });
  it("includes expected uploads and excludes discarded songs from remaining work", () => {
    expect(campaignOverview(campaign({ delivered: 2, discarded: 1 }, { expected_count: 5 }))).toMatchObject({ total: 5, pending: 2, complete: false });
    expect(campaignOverview(campaign({ delivered: 2, discarded: 1 }))).toMatchObject({ pending: 0, complete: true });
    expect(campaignOverview(campaign({ discarded: 3 })).complete).toBe(false);
  });
  it("distinguishes downloadable files from portal deliveries", () => {
    expect(campaignOverview(campaign({ approved: 3 }, { destination_portal: "files" }))).toMatchObject({ files: true, delivered: 3, complete: true });
  });
  it("keeps client requests open even after delivery and hides cancelled work by default", () => {
    const row = campaign({ delivered: 4 });
    row.pipeline.flags = { change_requests_open: 2 };
    expect(inCampaignView(row, "attention")).toBe(true);
    expect(inCampaignView(row, "done")).toBe(false);
    expect(inCampaignView({ ...row, status: "cancelled" }, "active")).toBe(false);
    expect(inCampaignView({ ...row, status: "cancelled" }, "attention")).toBe(false);
  });
  it("defaults to calendar while retaining effort and learning modes", () => {
    expect(campaignOrder(null)).toBe("calendar");
    expect(campaignOrder("effort")).toBe("effort");
    expect(campaignOrder("learning")).toBe("learning");
  });
});
