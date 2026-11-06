import { describe, expect, it } from "vitest";
import { linesNear, splitRequestItems } from "./changeRequestWorkflow";

describe("splitRequestItems", () => {
  it("separa por // y conserva los tiempos citados", () => {
    const items = splitRequestItems("0:14 y 0:53 anda a lavartelos // 0:39 LUSTRADOR con sus labios //  ");
    expect(items.map((item) => item.text)).toEqual(["0:14 y 0:53 anda a lavartelos", "0:39 LUSTRADOR con sus labios"]);
    expect(items[0].times).toEqual([14, 53]);
    expect(items[1].times).toEqual([39]);
  });
  it("separa viñetas en renglones o en la misma línea", () => {
    const multiline = splitRequestItems('- 1:26 quitar el DEL\n- 2:26 la línea es "Ligero, iba de prisa"');
    expect(multiline.map((item) => item.times)).toEqual([[86], [146]]);
    const inline = splitRequestItems('- 1:26 quitar el DEL - 2:26 la línea es "Ligero"');
    expect(inline).toHaveLength(2);
  });
  it("no parte un rango ni un guion dentro de una frase", () => {
    const items = splitRequestItems("Desde 0:17 hasta 0:21 debe decir: el exilio - te vistió");
    expect(items).toHaveLength(1);
    expect(items[0].times).toEqual([17, 21]);
  });
  it("un pedido vacío no inventa puntos", () => {
    expect(splitRequestItems("")).toEqual([]);
    expect(splitRequestItems(null)).toEqual([]);
  });
});

describe("linesNear", () => {
  const segments = [0, 5, 10, 14, 19, 30, 40].map((start) => ({ start, text: `línea ${start}` }));
  it("muestra las líneas alrededor del tiempo citado", () => {
    expect(linesNear(segments, [14]).map((line) => line.start)).toEqual([10, 14, 19]);
  });
  it("cubre un rango completo y respeta el máximo", () => {
    expect(linesNear(segments, [5, 30], { max: 3 }).map((line) => line.start)).toEqual([5, 10, 14]);
  });
  it("sin tiempos o sin letra no devuelve nada", () => {
    expect(linesNear(segments, [])).toEqual([]);
    expect(linesNear(null, [10])).toEqual([]);
  });
});
