import { useParams } from "react-router-dom";
import CampaignDetailPage from "./campaigns/CampaignDetailPage";
import CampaignListPage from "./campaigns/CampaignListPage";

export default function CampaignsPage() {
  const { campaignId } = useParams();
  return campaignId ? <CampaignDetailPage key={campaignId} id={campaignId} /> : <CampaignListPage />;
}
