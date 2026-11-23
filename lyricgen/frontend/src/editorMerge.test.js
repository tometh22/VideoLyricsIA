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

// Regresión del incidente f866cbcf0e49 (UMG Chile, 1-sep-2026). El operador
// duplicó el estribillo a mano; `duplicateSeg` clonaba el `segment_id` del
// padre, así que 5 filas compartían la clave estable del merge. Con la clave
// pelada, `order` visitaba esa clave 5 veces y `localMap.get(key)` devolvía
// siempre la MISMA fila: el merge salía con 44 filas pero sólo 38 contenidos
// distintos, y el deduplicador de colisiones remataba borrando las copias.
// Resultado: 6 líneas de letra desaparecidas de un entregable, sin conflicto
// y sin un solo error. Un rebase cualquiera (un 409, un reconcile) alcanzaba.
describe("filas con segment_id duplicado (incidente f866cbcf0e49)", () => {
  const chorus = (start, extra = {}) => ({
    segment_id: "4", start, end: start + 1, text: "¡Esta es nuestra fiesta!", ...extra,
  });
  const doc = [
    { segment_id: "0", start: 0, end: 0.9, text: "Algo nuevo" },
    chorus(6.5), chorus(7.9), chorus(11.0), chorus(15.8), chorus(20.7),
    { segment_id: "8", start: 41.9, end: 43.3, text: "Un tren se desprende de mi mente" },
  ];

  it("no colapsa las repeticiones en un rebase sin cambios", () => {
    const result = mergeThreeWay(doc, doc, doc);

    expect(result.merged).toHaveLength(doc.length);
    expect(result.conflicts).toHaveLength(0);
    // Y cada fila es una fila DISTINTA, no cinco referencias a la última:
    // ésa era la forma exacta del bug, y lo que después borraba el
    // deduplicador de colisiones.
    expect(new Set(result.merged.map((row) => row.start)).size).toBe(doc.length);
    expect(result.merged.map((row) => row.start)).toEqual(doc.map((row) => row.start));
  });

  it("conserva las repeticiones cuando el otro lado editó una línea distinta", () => {
    const remote = doc.map((row, index) => (
      index === 6 ? { ...row, text: "Un tren se desprende de mi mente (bis)" } : row
    ));
    const result = mergeThreeWay(doc, doc, remote);

    expect(result.merged).toHaveLength(doc.length);
    expect(result.merged.filter((row) => row.text.includes("nuestra fiesta"))).toHaveLength(5);
    expect(result.merged[6].text).toBe("Un tren se desprende de mi mente (bis)");
  });

  it("edita la repetición correcta y deja intactas las demás", () => {
    const local = doc.map((row, index) => (
      index === 3 ? { ...row, text: "¡Ésta es nuestra fiesta!" } : row
    ));
    const result = mergeThreeWay(doc, local, doc);

    expect(result.merged).toHaveLength(doc.length);
    expect(result.merged.map((row) => row.text)).toEqual(local.map((row) => row.text));
  });

  it("sigue reportando el id estable pelado en los conflictos", () => {
    const local = doc.map((row, index) => (index === 3 ? { ...row, text: "local" } : row));
    const remote = doc.map((row, index) => (index === 3 ? { ...row, text: "remoto" } : row));
    const result = mergeThreeWay(doc, local, remote);

    expect(result.conflicts).toHaveLength(1);
    expect(result.conflicts[0].key).toBe("4");
  });
});

it('merges a local start and a remote text without mixing opposing timing edges', () => {
  const base=[{segment_id:'one',start:1,end:3,text:'Original',locked:false}];
  const merged=mergeThreeWay(base,[{...base[0],start:2,locked:true}],[{...base[0],text:'Remote correction'}]);
  expect(merged.conflicts).toHaveLength(0);
  expect(merged.merged[0]).toMatchObject({start:2,end:3,text:'Remote correction',locked:true});
  const conflict=mergeThreeWay(base,[{...base[0],start:2}],[{...base[0],end:1.5}]);
  expect(conflict.conflicts[0].fields).toContain('timing');
  expect(conflict.merged[0]).toMatchObject({start:2,end:3});
});

it('preserves concurrent additions with the same historical ID but different content', () => {
  const local=[{segment_id:'16',start:1,end:2,text:'Local addition'}];
  const remote=[{segment_id:'16',start:3,end:4,text:'Remote addition'}];
  expect(mergeThreeWay([],local,remote).merged.map(row=>row.text)).toEqual(['Local addition','Remote addition']);
});

it('keeps a remote correction when a previous duplicate was removed on either side', () => {
  const base=[{segment_id:'16',start:1,end:2,text:'First'}, {segment_id:'16',start:5,end:6,text:'Survivor'}];
  const local=[{...base[1],text:'Survivor corrected locally'}];
  expect(mergeThreeWay(base,local,base).merged.map(row=>row.text)).toEqual(['Survivor corrected locally']);
  expect(mergeThreeWay(base,base,[{...base[1],text:'Survivor corrected remotely'}]).merged.map(row=>row.text)).toEqual(['Survivor corrected remotely']);
});

it('compares semantic fields independently from JSON property order', () => {
  expect(segmentsEquivalent([{start:1,end:2,text:'Frase',pos:{x:.2,y:.5}}],
    [{pos:{y:.5,x:.2},text:'Frase',end:2,start:1}])).toBe(true);
});

it('retains both corrections when a legacy occurrence has no safe base match', () => {
  const base=[{segment_id:'16',start:1,end:2,text:'First'}, {segment_id:'16',start:5,end:6,text:'Second'}];
  const local=[{segment_id:'16',start:7,end:8,text:'Moved and rewritten'}];
  const remote=[base[0],{...base[1],text:'Remote correction with evidence'}];
  const result=mergeThreeWay(base,local,remote);
  expect(result.merged.map(row=>row.text)).toEqual(['Remote correction with evidence','Moved and rewritten']);
  expect(result.conflicts.some(conflict=>conflict.reason==='ambiguous-identity')).toBe(true);
});

it('keeps independent corrections through deletions across many legacy groups', () => {
  for (let count=1;count<=40;count++) {
    const base=Array.from({length:count*4},(_,i)=>({segment_id:String(Math.floor(i/4)),start:i*2,end:i*2+1,text:`Line ${i}`}));
    const local=base.filter((_,i)=>i%4!==0).map(row=>Number(row.start/2)%4===2?{...row,text:row.text+' local'}:row);
    const remote=base.map((row,i)=>i%4===1?{...row,end:row.end+.2}:i%4===3?{...row,text:row.text+' remote'}:row);
    const expected=remote.filter((_,i)=>i%4!==0).map(row=>Number(row.start/2)%4===2?{...row,text:row.text+' local'}:row);
    const result=mergeThreeWay(base,local,remote);
    expect(result.conflicts).toHaveLength(0);
    expect(result.merged.map(({_id,...row})=>row)).toEqual(expected);
  }
});

it('matches ID-less server rows to local React IDs without minting persisted identities', () => {
  const base=[{start:1,end:2,text:'First'},{start:3,end:4,text:'Second'}];
  const local=base.map((row,i)=>({...row,_id:i,text:i===0?'First local':row.text}));
  const remote=base.map((row,i)=>i===1?{...row,text:'Second remote'}:row);
  const result=mergeThreeWay(base,local,remote);
  expect(result.conflicts).toHaveLength(0);
  expect(result.merged.map(row=>row.text)).toEqual(['First local','Second remote']);
  expect(result.merged.every(row=>row.segment_id===undefined)).toBe(true);
});

it('keeps an ID-less survivor bound to its base after its previous sibling was deleted', () => {
  const base=[{start:1,end:2,text:'First'},{start:3,end:4,text:'Survivor'}];
  const local=[{...base[1],_id:1}];
  const remote=[base[0],{...base[1],text:'Survivor remote'}];
  expect(mergeThreeWay(base,local,remote).merged).toEqual([expect.objectContaining({text:'Survivor remote',start:3,end:4})]);
});
