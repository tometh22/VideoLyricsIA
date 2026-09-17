// Small, deliberately strict URL contract between the UMG change-request
// workspace and the post-render editor.  These values cross navigation and
// may be typed by hand, so never treat an arbitrary return_to URL as trusted.

const PROPOSAL_ID = /^[A-Za-z0-9_-]{1,36}$/;

export function parseChangeRequestId(search = "") {
  const raw = new URLSearchParams(search).get("change_request_id") || "";
  const id = Number(raw);
  return /^\d+$/.test(raw) && Number.isSafeInteger(id) && id > 0 ? id : null;
}

// Old tabs/bookmarks predate proposal_id. Recover their render intent from
// the authenticated server, never merely from the presence of a query param.
export async function recoverChangeRequestEditContext({ search, job, request }) {
  const explicit = parseChangeRequestEditContext(search);
  const changeRequestId = parseChangeRequestId(search);
  if (explicit || !changeRequestId) return explicit;
  // A supplied but malformed proposal is not a legacy link.
  if (new URLSearchParams(search).has("proposal_id")) {
    throw new Error("invalid_change_request_context");
  }
  const response = await request(`/admin/change-requests/${changeRequestId}/proposals/current`);
  if (response.status === 404) return null; // manual request without a proposal
  if (!response.ok) throw new Error("change_request_context_unavailable");
  const { proposal } = await response.json();
  if (proposal?.job_id !== job.job_id || proposal?.change_request_id !== changeRequestId) {
    throw new Error("change_request_job_mismatch");
  }
  if (!["applied", "partially_applied"].includes(proposal.status)) return null;
  if (!PROPOSAL_ID.test(proposal.id || "")
    || !Number.isInteger(proposal.applied_revision)
    || proposal.applied_revision < 0
    || proposal.applied_revision > (job.segments_revision || 0)) {
    throw new Error("change_request_revision_unavailable");
  }
  return { changeRequestId, proposalId: proposal.id };
}

export function parseChangeRequestEditContext(search = "") {
  const params = new URLSearchParams(search);
  const rawRequestId = params.get("change_request_id") || "";
  const proposalId = params.get("proposal_id") || "";
  if (!/^\d+$/.test(rawRequestId) || !PROPOSAL_ID.test(proposalId)) return null;
  const changeRequestId = Number(rawRequestId);
  if (!Number.isSafeInteger(changeRequestId) || changeRequestId <= 0) return null;
  return { changeRequestId, proposalId };
}

export function changeRequestAdminPath(context, state = null) {
  if (!context?.changeRequestId || !context?.proposalId) return null;
  const params = new URLSearchParams({
    section: "cambios",
    change_request_id: String(context.changeRequestId),
  });
  if (state === "submitted") params.set("render_submitted", "1");
  if (state === "completed") params.set("render_completed", "1");
  return `/admin?${params.toString()}`;
}
