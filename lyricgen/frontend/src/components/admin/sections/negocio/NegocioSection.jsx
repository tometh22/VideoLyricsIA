// Sección "Negocio" del Admin Panel v2 — sub-tabs "costos" y "invoices",
// elegidos desde el sidebar (el padre pasa subTab).
//
// Rediseño 2026-06 (PR B): las dos tabs se rediseñaron con las primitivas
// compartidas (DataTable / KpiCard / FilterBar / StatusBadge / EmptyState),
// siguiendo el patrón de Usuarios. La data vive en useNegocio; esta sección
// solo cablea el hook a la vista correspondiente.
import CostosView from "./CostosView";
import MargenTenantsView from "./MargenTenantsView";
import InvoicesView from "./InvoicesView";
import useNegocio from "./useNegocio";

export default function NegocioSection({ subTab }) {
  const {
    economics,
    economicsForbidden,
    economicsLoading,
    costSinceDays,
    setCostSinceDays,
    costRevenuePerVideo,
    setCostRevenuePerVideo,
    costDashboard,
    costLoading,
    invoices,
    invoicesLoading,
  } = useNegocio();

  if (subTab === "invoices") {
    return <InvoicesView invoices={invoices} invoicesLoading={invoicesLoading} />;
  }

  if (subTab === "costos") {
    return (
      <>
        <CostosView
          costSinceDays={costSinceDays}
          setCostSinceDays={setCostSinceDays}
          costRevenuePerVideo={costRevenuePerVideo}
          setCostRevenuePerVideo={setCostRevenuePerVideo}
          costDashboard={costDashboard}
          costLoading={costLoading}
        />
        <MargenTenantsView
          economics={economics}
          loading={economicsLoading}
          forbidden={economicsForbidden}
        />
      </>
    );
  }

  return null;
}
