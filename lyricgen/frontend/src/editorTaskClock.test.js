import { describe, expect, it } from "vitest";
import {
  TASKS, bucketByTask, classifyTask, isEditableTarget, readTaskAttr,
} from "./editorTaskClock";

describe("classifyTask", () => {
  it("un atributo explícito gana sobre todo lo demás", () => {
    expect(classifyTask({ taskAttr: "timing", editable: true, isPlaying: true }))
      .toBe("timing");
    expect(classifyTask({ taskAttr: "export" })).toBe("export");
    expect(classifyTask({ taskAttr: "  TIMING  " })).toBe("timing");
  });

  it("ignora atributos que no son tareas conocidas", () => {
    expect(classifyTask({ taskAttr: "cualquiera", editable: true })).toBe("text");
  });

  it("escribir en un campo es texto aunque suene el audio", () => {
    expect(classifyTask({ editable: true, isPlaying: true, interactedRecently: true }))
      .toBe("text");
  });

  it("audio sonando sin interacción es escuchar", () => {
    expect(classifyTask({ isPlaying: true, interactedRecently: false })).toBe("listen");
  });

  it("interacción sin tipo identificable es buscar", () => {
    expect(classifyTask({ interactedRecently: true })).toBe("search");
  });

  it("sin señales no inventa una tarea", () => {
    expect(classifyTask()).toBe("unknown");
    expect(classifyTask({})).toBe("unknown");
  });

  it("siempre devuelve una tarea conocida", () => {
    const cases = [
      {}, { editable: true }, { isPlaying: true }, { interactedRecently: true },
      { taskAttr: "vocalization" }, { taskAttr: null },
    ];
    cases.forEach((signals) => expect(TASKS).toContain(classifyTask(signals)));
  });
});

describe("isEditableTarget", () => {
  it("reconoce textarea, inputs de texto y contenteditable", () => {
    expect(isEditableTarget({ tagName: "TEXTAREA" })).toBe(true);
    expect(isEditableTarget({ tagName: "INPUT", type: "text" })).toBe(true);
    expect(isEditableTarget({ tagName: "INPUT" })).toBe(true);
    expect(isEditableTarget({ tagName: "DIV", isContentEditable: true })).toBe(true);
  });

  it("no confunde botones ni controles con edición de texto", () => {
    expect(isEditableTarget({ tagName: "INPUT", type: "range" })).toBe(false);
    expect(isEditableTarget({ tagName: "INPUT", type: "checkbox" })).toBe(false);
    expect(isEditableTarget({ tagName: "BUTTON" })).toBe(false);
    expect(isEditableTarget(null)).toBe(false);
  });
});

describe("readTaskAttr", () => {
  it("lee el ancestro más cercano y tolera nodos sin closest", () => {
    const node = {
      closest: (selector) => (selector === "[data-editor-task]"
        ? { getAttribute: () => "timing" } : null),
    };
    expect(readTaskAttr(node)).toBe("timing");
    expect(readTaskAttr({})).toBeNull();
    expect(readTaskAttr(null)).toBeNull();
  });

  it("no explota si closest tira", () => {
    expect(readTaskAttr({ closest: () => { throw new Error("boom"); } })).toBeNull();
  });
});

describe("bucketByTask", () => {
  it("atribuye el hueco a la tarea del latido más nuevo", () => {
    const beats = [
      { atMs: 0, task: "listen" },
      { atMs: 15000, task: "listen" },
      { atMs: 30000, task: "text" },
    ];
    expect(bucketByTask(beats)).toEqual({ listen: 15000, text: 15000 });
  });

  it("descarta huecos largos: no cuenta el editor abierto sin nadie", () => {
    const beats = [
      { atMs: 0, task: "text" },
      { atMs: 600000, task: "text" },
      { atMs: 615000, task: "text" },
    ];
    expect(bucketByTask(beats)).toEqual({ text: 15000 });
  });

  it("ordena por tiempo y trata tareas desconocidas como unknown", () => {
    const beats = [
      { atMs: 15000, task: "inventada" },
      { atMs: 0, task: "search" },
    ];
    expect(bucketByTask(beats)).toEqual({ unknown: 15000 });
  });

  it("con menos de dos latidos no acredita tiempo", () => {
    expect(bucketByTask([{ atMs: 0, task: "text" }])).toEqual({});
    expect(bucketByTask([])).toEqual({});
    expect(bucketByTask(null)).toEqual({});
  });
});
