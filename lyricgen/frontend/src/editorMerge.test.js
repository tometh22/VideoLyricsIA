import { describe, expect, it } from "vitest";
import { mergeThreeWay, segmentsEquivalent } from "./editorMerge";

const base = [
  { _id: 1, start: 0, end: 1, text: "línea uno" },
  { _id: 2, start: 1, end: 2, text: "línea dos" },
];

describe("mergeThreeWay", () => {
  it("treats renderer metadata and local ids as non-conflicting", () => {
    expect(segmentsEquivalent(
      [{ _id: 1, start: 0, end: 1, text: "línea", words: [{ start: 0 }] }],
      [{ _id: 99, start: 0, end: 1, text: "línea", words: [{ start: 0.2 }], review: true }],
    )).toBe(true);
  });

  it("merges independent line edits without opening a conflict", () => {
    const result = mergeThreeWay(
      base,
      [{ ...base[0], text: "línea uno local" }, base[1]],
      [base[0], { ...base[1], text: "línea dos remota" }],
    );

    expect(result.conflicts).toHaveLength(0);
    expect(result.merged.map((line) => line.text)).toEqual([
      "línea uno local",
      "línea dos remota",
    ]);
  });

  it("reports a conflict when both sides edit the same line", () => {
    const result = mergeThreeWay(
      base,
      [{ ...base[0], text: "cambio local" }, base[1]],
      [{ ...base[0], text: "cambio remoto" }, base[1]],
    );

    expect(result.conflicts).toHaveLength(1);
    expect(result.conflicts[0].key).toBe("1");
  });
});

describe("precisión de tiempos vs el redondeo del backend", () => {
  // Causa raíz de los conflictos falsos: el backend persiste con
  // `round(start, 4)` (editor.py) pero el cliente conservaba el float crudo
  // que envió. Al comparar byte-exacto, CADA drag de timeline parecía una
  // edición remota -> conflicto falso -> editor trabado sin salida.
  const dragged = { _id: 1, start: 12.34567891, end: 15.98765432, text: "línea" };
  const roundedByServer = { _id: 1, start: 12.3457, end: 15.9877, text: "línea" };

  it("no marca como distinto un timing que sólo difiere por el redondeo del servidor", () => {
    expect(segmentsEquivalent([dragged], [roundedByServer])).toBe(true);
  });

  it("no abre conflicto cuando el remoto es el mismo drag ya redondeado", () => {
    const result = mergeThreeWay([dragged], [dragged], [roundedByServer]);
    expect(result.conflicts).toHaveLength(0);
  });

  it("sigue detectando un cambio de timing REAL del operador", () => {
    const moved = { ...dragged, start: 12.5, end: 16.1 };
    expect(segmentsEquivalent([dragged], [moved])).toBe(false);
  });

  it("sigue detectando un cambio de texto real", () => {
    expect(segmentsEquivalent(
      [dragged],
      [{ ...roundedByServer, text: "otra línea" }],
    )).toBe(false);
  });
});
