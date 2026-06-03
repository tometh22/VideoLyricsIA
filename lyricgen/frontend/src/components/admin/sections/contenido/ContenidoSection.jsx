// Sección "Contenido" del Admin Panel v2 — dos sub-vistas (Fondos | Compliance)
// elegidas desde el sidebar del admin (el padre pasa subTab).
//
// Switch fino: llama a useContenido() una sola vez y baja las piezas que cada
// vista necesita como props (mismo espíritu que UsuariosSection, que delega en
// vistas dedicadas). La data y las mutaciones viven en el hook.
import FondosView from "./FondosView";
import ComplianceView from "./ComplianceView";
import useContenido from "./useContenido";

export default function ContenidoSection({ subTab }) {
  const {
    backgrounds,
    bgName,
    setBgName,
    bgTags,
    setBgTags,
    bgOwnerTenant,
    setBgOwnerTenant,
    bgTenants,
    bgListFilter,
    setBgListFilter,
    bgUploading,
    handleUploadBg,
    handleDeleteBg,
    compliance,
  } = useContenido();

  if (subTab === "compliance") {
    return <ComplianceView compliance={compliance} />;
  }

  return (
    <FondosView
      backgrounds={backgrounds}
      bgName={bgName}
      setBgName={setBgName}
      bgTags={bgTags}
      setBgTags={setBgTags}
      bgOwnerTenant={bgOwnerTenant}
      setBgOwnerTenant={setBgOwnerTenant}
      bgTenants={bgTenants}
      bgListFilter={bgListFilter}
      setBgListFilter={setBgListFilter}
      bgUploading={bgUploading}
      handleUploadBg={handleUploadBg}
      handleDeleteBg={handleDeleteBg}
    />
  );
}
