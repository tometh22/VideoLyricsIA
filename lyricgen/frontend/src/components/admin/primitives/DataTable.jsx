// Tabla de datos del admin. Reemplaza las ~6 tablas hand-rolled del
// monolito con un solo componente que maneja loading / vacío / expansión.
//
//   <DataTable
//     columns={[{ key: "user", header: "Usuario", render: (row) => <... /> }]}
//     rows={users}
//     rowKey={(u) => u.user_id}
//     loading={loading}
//     empty={<EmptyState title="Sin usuarios" />}
//     onRowClick={(row) => toggle(row.user_id)}
//     expandedKey={expandedId}
//     renderExpanded={(row) => <Detail row={row} />}
//     dense
//   />
//
// Column def: { key, header, align?: "left"|"right"|"center", width?,
//               render?: (row) => node, className? }
import { Fragment } from "react";

import TableSkeleton from "./TableSkeleton";

export default function DataTable({
  columns,
  rows,
  rowKey,
  loading = false,
  skeletonRows = 6,
  empty = null,
  onRowClick = null,
  expandedKey = null,
  renderExpanded = null,
  dense = false,
}) {
  const keyOf = (row, i) => (rowKey ? rowKey(row) : row.id ?? i);
  const alignClass = (a) => (a === "right" ? "text-right" : a === "center" ? "text-center" : "text-left");

  if (loading) return <TableSkeleton rows={skeletonRows} cols={Math.min(columns.length, 6)} />;
  if (!rows || rows.length === 0) return empty;

  return (
    <div className="overflow-x-auto max-h-[68vh]">
      <table className={`w-full ${dense ? "text-label" : "text-caption"}`}>
        <thead className="sticky top-0 z-10 bg-surface-1">
          <tr className="text-gray-500 uppercase tracking-wide text-section">
            {columns.map((col) => (
              <th
                key={col.key}
                className={`font-medium pb-2.5 px-3 first:pl-2 last:pr-2 ${alignClass(col.align)} ${col.className || ""}`}
                style={col.width ? { width: col.width } : undefined}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-white/[0.04]">
          {rows.map((row, i) => {
            const k = keyOf(row, i);
            const isExpanded = expandedKey != null && expandedKey === k;
            return (
              <Fragment key={k}>
                <tr
                  onClick={onRowClick ? () => onRowClick(row) : undefined}
                  className={`transition-colors duration-brand ${
                    onRowClick ? "cursor-pointer hover:bg-white/[0.03]" : ""
                  } ${isExpanded ? "bg-white/[0.04]" : ""}`}
                >
                  {columns.map((col) => (
                    <td
                      key={col.key}
                      className={`py-2.5 px-3 first:pl-2 last:pr-2 ${alignClass(col.align)} ${col.className || ""}`}
                    >
                      {col.render ? col.render(row) : row[col.key]}
                    </td>
                  ))}
                </tr>
                {isExpanded && renderExpanded && (
                  <tr>
                    <td colSpan={columns.length} className="p-4 bg-surface-2/30">
                      {renderExpanded(row)}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
