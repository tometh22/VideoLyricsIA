import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { campaignRequest } from "../lib/campaignApi";
import { Banner, Button, EmptyState } from "./campaigns/ui";

/**
 * The review queue now lives inside each campaign (stage "Letra"). This
 * route keeps old bookmarks, sidebar muscle memory and editor fallbacks
 * working by forwarding to the right campaign with the same context.
 */
export function reviewQueueTarget(campaigns, params, remembered) {
  const requested = params.get("campaign");
  const byId = (id) => campaigns.find((campaign) => campaign.id === id);
  const campaign = (requested && byId(requested))
    || (remembered && byId(remembered)?.status === "active" && byId(remembered))
    || campaigns.find((item) => item.status === "active" && (item.kind || "lyric_video") !== "art_track")
    || campaigns.find((item) => (item.kind || "lyric_video") !== "art_track");
  if (!campaign) return null;
  const next = new URLSearchParams({ view: "lyrics" });
  if (params.get("q")) next.set("q", params.get("q"));
  if (params.get("approved")) next.set("approved", params.get("approved"));
  if (params.get("scope") === "drafts") next.set("drafts", "1");
  if (params.get("scope") === "approved" || params.get("scope") === "all") next.set("view", "all");
  if (params.get("scope") === "discarded") next.set("view", "discarded");
  return `/campaigns/${encodeURIComponent(campaign.id)}?${next}`;
}

export default function ReviewQueuePage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [state, setState] = useState({ loading: true, error: "", empty: false });
  useEffect(() => {
    const controller = new AbortController();
    campaignRequest("/batch/campaigns", { signal: controller.signal }).then((body) => {
      let remembered = null;
      try { remembered = localStorage.getItem("genly:last-campaign"); } catch { /* private mode */ }
      const target = reviewQueueTarget(body.items || [], params, remembered);
      if (target) navigate(target, { replace: true });
      else setState({ loading: false, error: "", empty: true });
    }).catch((error) => { if (!controller.signal.aborted) setState({ loading: false, error: error.message, empty: false }); });
    return () => controller.abort();
  }, [navigate, params]);
  return <div className="mx-auto max-w-3xl py-10" data-testid="review-queue-page">
    {state.loading && <p role="status" className="text-sm text-ink-secondary">Abriendo la cola de revisión…</p>}
    {state.error && <Banner tone="danger" action={<Button size="sm" onClick={() => window.location.reload()}>Reintentar</Button>}>{state.error}</Banner>}
    {state.empty && <EmptyState title="No hay campañas de lyric videos" description="La revisión de letras vive dentro de cada campaña."
      action={<Button variant="primary" onClick={() => navigate("/campaigns")}>Ir a Campañas</Button>} />}
  </div>;
}
