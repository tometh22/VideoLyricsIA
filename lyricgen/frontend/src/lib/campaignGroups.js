// How the campaign list is organized. Campaigns have no "lote" field; the
// operators already encode it in the name ("UMG Agosto 2026 — Rata Blanca —
// Vol 1"), so the first segment of the name is the group. A prefix shared by a
// single campaign is not a group: it falls into "Otras" instead of producing a
// section per QA run.
import { campaignCounts, campaignNextStep } from "./campaignPipeline";

const SEPARATOR = /\s+[—–-]\s+/;
export const OTHER_GROUP = "Otras campañas";
export const CANCELLED_GROUP = "Canceladas";

export function campaignGroupKey(name) {
  const parts = String(name ?? "").split(SEPARATOR);
  return parts.length > 1 && parts[0].trim() ? parts[0].trim() : null;
}

/** Name without its group prefix, so the distinguishing part is never truncated away. */
export function campaignShortName(name, group) {
  const value = String(name ?? "");
  if (!group || campaignGroupKey(value) !== group) return value;
  return value.split(SEPARATOR).slice(1).join(" — ");
}

const NEXT_PRIORITY = { lyrics: 1, qc: 2, ready: 3, approved: 4, attention: 5 };

/** What the operator has to do in this campaign, or null when nothing. */
export function campaignPending(campaign) {
  if (campaign.status === "cancelled") return null;
  const kind = campaign.kind || "lyric_video";
  const next = campaignNextStep(campaignCounts(campaign), kind, { portalSends: campaign.destination_portal !== "files" });
  const requests = Number(campaign.pipeline?.flags?.change_requests_open) || 0;
  const action = next && !next.passive ? next : null;
  if (!requests && !action) return null;
  return { requests, oldestRequestAt: campaign.pipeline?.flags?.oldest_change_request_at || null, next: action };
}

const time = (value) => {
  const parsed = Date.parse(value || "");
  return Number.isNaN(parsed) ? 0 : parsed;
};

/** Client change requests first (oldest waiting first), then by pipeline order. */
export function sortByUrgency(campaigns) {
  return campaigns
    .map((campaign) => ({ campaign, pending: campaignPending(campaign) }))
    .filter(({ pending }) => pending)
    .sort((a, b) => {
      if (!!a.pending.requests !== !!b.pending.requests) return a.pending.requests ? -1 : 1;
      if (a.pending.requests) return time(a.pending.oldestRequestAt) - time(b.pending.oldestRequestAt);
      const order = (NEXT_PRIORITY[a.pending.next?.stage] || 9) - (NEXT_PRIORITY[b.pending.next?.stage] || 9);
      return order || time(b.campaign.updated_at) - time(a.campaign.updated_at);
    })
    .map(({ campaign }) => campaign);
}

/**
 * Groups for the index. Cancelled campaigns get their own trailing group
 * unless the operator is explicitly looking at cancelled ones.
 */
export function groupCampaigns(campaigns, { separateCancelled = true } = {}) {
  const prefixCount = new Map();
  for (const campaign of campaigns) {
    if (separateCancelled && campaign.status === "cancelled") continue;
    const key = campaignGroupKey(campaign.name);
    if (key) prefixCount.set(key, (prefixCount.get(key) || 0) + 1);
  }
  const groups = new Map();
  const add = (key, campaign, lot) => {
    if (!groups.has(key)) groups.set(key, { key, lot, campaigns: [] });
    groups.get(key).campaigns.push(campaign);
  };
  for (const campaign of campaigns) {
    const prefix = campaignGroupKey(campaign.name);
    if (separateCancelled && campaign.status === "cancelled") add(CANCELLED_GROUP, campaign, false);
    else if (prefix && prefixCount.get(prefix) > 1) add(prefix, campaign, true);
    else add(OTHER_GROUP, campaign, false);
  }
  const latest = (group) => Math.max(0, ...group.campaigns.map((campaign) => time(campaign.updated_at || campaign.created_at)));
  const rank = (group) => (group.key === CANCELLED_GROUP ? 2 : group.lot ? 0 : 1);
  // Live work first inside a lot, then most recent.
  const live = (campaign) => (campaign.status === "active" || campaign.status === "paused" || campaignPending(campaign) ? 0 : 1);
  for (const group of groups.values()) group.campaigns.sort((a, b) => live(a) - live(b) || time(b.updated_at || b.created_at) - time(a.updated_at || a.created_at));
  return [...groups.values()].sort((a, b) => rank(a) - rank(b) || latest(b) - latest(a) || a.key.localeCompare(b.key, "es"));
}
