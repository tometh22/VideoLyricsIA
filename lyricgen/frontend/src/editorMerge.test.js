import { describe, expect, it } from "vitest";
import { mergeThreeWay } from "./editorMerge";

const base = [
  { _id: 1, start: 0, end: 1, text: "línea uno" },
  { _id: 2, start: 1, end: 2, text: "línea dos" },
];

describe("mergeThreeWay", () => {
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
