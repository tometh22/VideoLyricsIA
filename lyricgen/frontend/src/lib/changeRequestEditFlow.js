// Small, deliberately strict URL contract between the UMG change-request
// workspace and the post-render editor.  These values cross navigation and
// may be typed by hand, so never treat an arbitrary return_to URL as trusted.

const PROPOSAL_ID = /^[A-Za-z0-9_-]{1,36}$/;

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
