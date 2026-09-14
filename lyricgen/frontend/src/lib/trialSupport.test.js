import { createTrialSupport, CRISP_WEBSITE_ID, isTrialSupportEnabled, SUPPORT_SESSION_KEY } from "./trialSupport";

describe("trial support isolation", () => {
  let support;
  beforeEach(() => {
    vi.useFakeTimers();
    sessionStorage.clear();
    document.head.innerHTML = "";
    for (const key of ["$crisp", "CRISP_TOKEN_ID", "CRISP_READY_TRIGGER", "CRISP_WEBSITE_ID"]) delete window[key];
    support = createTrialSupport(window);
  });
  afterEach(() => vi.useRealTimers());

  it.each([
    ["trial", "trial.genly.pro", true],
    ["production", "trial.genly.pro", false],
    ["staging", "trial.genly.pro", false],
    ["trial", "genly.pro", false],
    ["trial", "staging.genly.pro", false],
    ["trial", "preview.vercel.app", false],
    ["trial", "trial.genly.pro.attacker.test", false],
  ])("gates %s / %s", (env, host, expected) => {
    expect(isTrialSupportEnabled(env, host)).toBe(expected);
  });

  it("makes no request on mount/identity binding; loads once only when requested", async () => {
    support.setOwner("tenant:a");
    expect(document.querySelector("script")).toBeNull();
    const first = support.open();
    const second = support.open();
    expect(document.querySelectorAll("script")).toHaveLength(1);
    expect(document.querySelector("script").src).toBe("https://client.crisp.chat/l.js");
    expect(window.CRISP_WEBSITE_ID).toBe(CRISP_WEBSITE_ID);
    expect(window.CRISP_COOKIE_DOMAIN).toBe("trial.genly.pro");
    expect(window.CRISP_TOKEN_ID).not.toContain("tenant");
    expect(window.$crisp).not.toContainEqual(["do", "chat:open"]);
    window.CRISP_READY_TRIGGER();
    await Promise.all([first, second]);
    expect(window.$crisp).toContainEqual(["do", "chat:open"]);
    expect(window.$crisp.some(([action]) => action === "set")).toBe(false);
  });

  it("retains an opaque conversation token on same-account page reload", async () => {
    support.setOwner("tenant:a");
    const first = support.open();
    window.CRISP_READY_TRIGGER();
    await first;
    const token = window.CRISP_TOKEN_ID;
    delete window.CRISP_TOKEN_ID;
    const reloaded = createTrialSupport(window);
    reloaded.setOwner("tenant:a");
    const second = reloaded.open();
    window.CRISP_READY_TRIGGER();
    await second;
    expect(window.CRISP_TOKEN_ID).toBe(token);
  });

  it("does not reuse a previous account's saved token on fresh page load", async () => {
    sessionStorage.setItem(SUPPORT_SESSION_KEY, JSON.stringify({ owner: "tenant:a", token: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" }));
    support.setOwner("tenant:b");
    const opened = support.open();
    window.CRISP_READY_TRIGGER();
    await opened;
    expect(window.CRISP_TOKEN_ID).not.toBe("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
  });

  it("clears and resets on logout, uses a distinct token for another account", async () => {
    support.setOwner("tenant:a");
    const opened = support.open();
    window.CRISP_READY_TRIGGER();
    await opened;
    const token = window.CRISP_TOKEN_ID;
    support.setOwner(null);
    expect(window.CRISP_TOKEN_ID).toBeUndefined();
    expect(sessionStorage.getItem(SUPPORT_SESSION_KEY)).toBeNull();
    expect(window.$crisp.slice(-2)).toEqual([["do", "chat:hide"], ["do", "session:reset"]]);
    await expect(support.open()).rejects.toThrow("signed-in");
    support.setOwner("tenant:b");
    await support.open();
    expect(window.CRISP_TOKEN_ID).not.toBe(token);
  });

  it("does not open a delayed chat after logout", async () => {
    support.setOwner("tenant:a");
    const opened = support.open();
    const rejected = expect(opened).rejects.toThrow();
    support.setOwner(null);
    window.CRISP_READY_TRIGGER();
    await rejected;
    expect(window.$crisp).not.toContainEqual(["do", "chat:open"]);
  });

  it("bounds unavailable/blocked SDK loading, and never auto-opens after a timeout", async () => {
    support.setOwner("tenant:a");
    const opened = support.open();
    const rejected = expect(opened).rejects.toThrow("unavailable");
    await vi.advanceTimersByTimeAsync(12000);
    await rejected;
    window.CRISP_READY_TRIGGER();
    expect(window.$crisp).not.toContainEqual(["do", "chat:open"]);
    await support.open();
    expect(window.$crisp).toContainEqual(["do", "chat:open"]);
  });

  it("handles network failure and blocked storage without affecting the app", async () => {
    vi.spyOn(window.sessionStorage, "getItem").mockImplementation(() => { throw new Error("blocked"); });
    support.setOwner("tenant:a");
    const opened = support.open();
    const rejected = expect(opened).rejects.toThrow("unavailable");
    document.querySelector("script").onerror();
    await rejected;
    vi.restoreAllMocks();
  });
});
