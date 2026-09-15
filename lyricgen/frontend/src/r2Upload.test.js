import { afterEach, describe, expect, it, vi } from "vitest";
import { __test__ } from "./r2Upload";

class HangingXhr {
  static latest = null;

  constructor() {
    this.upload = {};
    this.status = 0;
    this.statusText = "";
    HangingXhr.latest = this;
  }

  open() {}
  setRequestHeader() {}
  send() {}
  getResponseHeader() { return null; }
  abort() { this.onabort?.(); }
}

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("direct R2 upload transport", () => {
  it("fails a PUT that makes no progress instead of spinning forever", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("XMLHttpRequest", HangingXhr);
    const pending = __test__.putToR2WithProgress(
      "https://r2.invalid/signed", new Blob(["audio"]), "audio/mpeg",
      null, null, 100,
    );
    const assertion = expect(pending).rejects.toMatchObject({
      message: "R2 PUT stalled",
      timedOut: true,
    });
    await vi.advanceTimersByTimeAsync(100);
    await assertion;
  });

  it("resets the watchdog whenever upload progress arrives", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("XMLHttpRequest", HangingXhr);
    let settled = false;
    const pending = __test__.putToR2WithProgress(
      "https://r2.invalid/signed", new Blob(["audio"]), "audio/mpeg",
      vi.fn(), null, 100,
    ).finally(() => { settled = true; });
    const assertion = expect(pending).rejects.toMatchObject({ timedOut: true });
    await vi.advanceTimersByTimeAsync(80);
    HangingXhr.latest.upload.onprogress({ lengthComputable: true, loaded: 2, total: 5 });
    await vi.advanceTimersByTimeAsync(80);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(20);
    await assertion;
  });
});
