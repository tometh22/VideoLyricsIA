// Sección "Gestión" — administrar la plataforma (consolidación 2026-06-11).
// Absorbe las viejas secciones Usuarios (gestión de cuentas), Contenido
// (fondos + compliance) y Negocio (costos + facturación): cinco sub-tabs
// de ADMINISTRACIÓN, separadas de las vistas de operación/análisis.
//
// Las vistas y hooks existentes se reusan tal cual desde sus carpetas de
// origen — esta sección solo enruta el subTab; cada rama monta su hook
// únicamente cuando está activa (mismo costo de red que antes).
import GestionView from "../usuarios/GestionView";
import FondosView from "../contenido/FondosView";
import ComplianceView from "../contenido/ComplianceView";
import useContenido from "../contenido/useContenido";
import CostosView from "../negocio/CostosView";
import InvoicesView from "../negocio/InvoicesView";
import CreditsView from "../negocio/CreditsView";
import useNegocio from "../negocio/useNegocio";

function FondosTab() {
  const c = useContenido();
  return (
    <FondosView
      backgrounds={c.backgrounds}
      bgName={c.bgName}
      setBgName={c.setBgName}
      bgTags={c.bgTags}
      setBgTags={c.setBgTags}
      bgOwnerTenant={c.bgOwnerTenant}
      setBgOwnerTenant={c.setBgOwnerTenant}
      bgTenants={c.bgTenants}
      bgListFilter={c.bgListFilter}
      setBgListFilter={c.setBgListFilter}
      bgUploading={c.bgUploading}
      handleUploadBg={c.handleUploadBg}
      handleDeleteBg={c.handleDeleteBg}
    />
  );
}

function ComplianceTab() {
  const c = useContenido();
  return <ComplianceView compliance={c.compliance} />;
}

function CostosTab() {
  // El "Margen por tenant" (super-admin) ya NO vive acá: se mudó al panel
  // Insights, donde está el resto de la vista por-tenant del CEO. Esta tab
  // conserva el dashboard global de costos (nivel admin).
  const n = useNegocio();
  return (
    <CostosView
      costSinceDays={n.costSinceDays}
      setCostSinceDays={n.setCostSinceDays}
      costRevenuePerVideo={n.costRevenuePerVideo}
      setCostRevenuePerVideo={n.setCostRevenuePerVideo}
      costDashboard={n.costDashboard}
      costLoading={n.costLoading}
    />
  );
}

function InvoicesTab() {
  const n = useNegocio();
  return <InvoicesView invoices={n.invoices} invoicesLoading={n.invoicesLoading} />;
}

export default function GestionSection({ subTab }) {
  if (subTab === "fondos") return <FondosTab />;
  if (subTab === "compliance") return <ComplianceTab />;
  if (subTab === "costos") return <CostosTab />;
  if (subTab === "facturacion") return <InvoicesTab />;
  if (subTab === "creditos") return <CreditsView />;
  return <GestionView />;
}
