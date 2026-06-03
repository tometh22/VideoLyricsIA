// Vista "Facturación": lista de facturas emitidas y su estado de cobro.
// Datos de /admin/invoices (hook useNegocio). Tabla → DataTable + StatusBadge.
import { fmtDate, fmtMoney, INVOICE_STATUS } from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import StatusBadge from "../../primitives/StatusBadge";
import SectionHeader from "../../layout/SectionHeader";

export default function InvoicesView({ invoices, invoicesLoading }) {
  const columns = [
    { key: "date", header: "Fecha", render: (inv) => <span className="text-gray-400">{fmtDate(inv.created_at)}</span> },
    { key: "description", header: "Descripción", render: (inv) => inv.description || "—" },
    { key: "amount", header: "Monto", align: "right", render: (inv) => <span className="font-medium tabular-nums">{fmtMoney(inv.amount)}</span> },
    { key: "status", header: "Estado", render: (inv) => <StatusBadge status={inv.status} map={INVOICE_STATUS} /> },
    {
      key: "invoice",
      header: "Factura",
      render: (inv) =>
        inv.invoice_url ? (
          <a
            href={inv.invoice_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-caption text-brand hover:text-brand-light"
          >
            Ver
          </a>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <div>
      <SectionHeader
        title="Facturación"
        subtitle="Facturas emitidas y su estado de cobro."
      />
      <div className="glass rounded-card p-5">
        <DataTable
          dense
          columns={columns}
          rows={invoices}
          rowKey={(inv) => inv.id}
          loading={invoicesLoading}
          empty={
            <EmptyState
              title="Sin facturas todavía"
              message="Cuando se emita la primera factura va a aparecer acá."
            />
          }
        />
      </div>
    </div>
  );
}
