// Sección "Gestión" — administrar la plataforma (consolidación 2026-06-11).
// Absorbe las viejas secciones Usuarios (gestión de cuentas), Contenido
// (fondos + compliance) y Negocio (costos + facturación): sub-tabs de
// ADMINISTRACIÓN, separadas de las vistas de operación/análisis.
//
// Esta sección solo enruta el subTab; cada rama monta su hook únicamente
// cuando está activa.
//
// "Costos y márgenes" y "Costo de infra" eran dos sub-tabs distintas hasta
// ago-2026 y respondían la misma pregunta con dos períodos y dos números
// que no coincidían. Ahora son una sola página con tres bloques rotulados
// (ver `CostosPage`).
import GestionView from "../usuarios/GestionView";
import FondosView from "../contenido/FondosView";
import ComplianceView from "../contenido/ComplianceView";
import useContenido from "../contenido/useContenido";
import CostosPage from "../negocio/CostosPage";
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

function InvoicesTab() {
  // `soloFacturas` evita que esta tab dispare la agregación de /admin/margin,
  // que no usa. Antes `useNegocio` cargaba los dos datasets al montar y cada
  // tab pagaba la consulta de la otra.
  const n = useNegocio({ soloFacturas: true });
  return <InvoicesView invoices={n.invoices} invoicesLoading={n.invoicesLoading} />;
}

export default function GestionSection({ subTab }) {
  if (subTab === "fondos") return <FondosTab />;
  if (subTab === "compliance") return <ComplianceTab />;
  // El alias `infra` se ELIMINÓ. `leerDestino` valida el sub-tab contra el
  // NAV antes de restaurarlo, así que un `infra` guardado ya se coerciona al
  // default de Gestión y nunca llega hasta acá: la rama era muerta y su
  // comentario prometía lo contrario de lo que pasaba. Si alguna vez hace
  // falta redirigir sub-vistas retiradas, va como mapa de alias EN el NAV,
  // que es la única fuente de verdad, no como un `if` acá abajo.
  if (subTab === "costos") return <CostosPage />;
  if (subTab === "facturacion") return <InvoicesTab />;
  if (subTab === "creditos") return <CreditsView />;
  return <GestionView />;
}
