// Public installation identifier, never an API credential. Two independent
// gates prevent this integration from loading on production/staging/previews.
export const CRISP_WEBSITE_ID = "7e446151-256e-4de6-b1b4-c2a7e2928154";
export const SUPPORT_SESSION_KEY = "genly:trial-support:v1";

export function supportUserKey(user) {
  const id = user?.id || user?.username;
  return id ? `${user.tenant_id || ""}:${id}` : null;
}

export function isTrialSupportEnabled(environment, hostname) {
  return environment === "trial" && hostname === "trial.genly.pro";
}

// One controller per document. We do not explicitly supply auth tokens, email,
// lyrics, prompts or media. Crisp still receives ordinary connection/page
// metadata after opt-in. Conversations use a random,
// tab-scoped capability, not a guessable user ID or a Genly access token.
export function createTrialSupport(win = window) {
  let owner = null;
  let script = null;
  let ready = false;
  let pending = null;
  let cancelPending = null;
  const push = (...command) => {
    try { win.$crisp?.push(command); } catch { /* Support cannot break editing. */ }
  };
  const clearStored = () => {
    try { win.sessionStorage.removeItem(SUPPORT_SESSION_KEY); } catch { /* optional */ }
  };

  function setOwner(next) {
    if (next === owner) return;
    const previous = owner;
    owner = next;
    if (previous || !next) {
      clearStored();
      win.CRISP_TOKEN_ID = undefined;
      push("do", "chat:hide");
      push("do", "session:reset");
      cancelPending?.();
    }
  }

  function sessionToken() {
    try {
      const saved = JSON.parse(win.sessionStorage.getItem(SUPPORT_SESSION_KEY));
      if (saved?.owner === owner && /^[0-9a-f-]{36}$/i.test(saved.token)) return saved.token;
    } catch { /* Blocked storage simply means no continuity after reload. */ }
    const token = win.crypto.randomUUID();
    try { win.sessionStorage.setItem(SUPPORT_SESSION_KEY, JSON.stringify({ owner, token })); } catch { /* optional */ }
    return token;
  }

  async function open() {
    if (!owner) throw new Error("Support requires a signed-in user");
    const requestedOwner = owner;
    if (!win.CRISP_TOKEN_ID) {
      win.CRISP_TOKEN_ID = sessionToken();
      if (script) push("do", "session:reset");
    }
    if (!script) {
      win.CRISP_WEBSITE_ID = CRISP_WEBSITE_ID;
      win.CRISP_COOKIE_DOMAIN = "trial.genly.pro";
      win.CRISP_COOKIE_EXPIRE = 172800;
      win.CRISP_RUNTIME_CONFIG = { locale: "es", session_merge: false };
      win.$crisp = [];
      push("safe", true);
      push("do", "chat:hide");
      script = win.document.createElement("script");
      script.id = "genly-trial-support-script";
      script.src = "https://client.crisp.chat/l.js";
      script.async = true;
      script.referrerPolicy = "no-referrer";
      pending = new Promise((resolve, reject) => {
        const timeout = win.setTimeout(() => fail(), 12000);
        const fail = () => {
          win.clearTimeout(timeout);
          cancelPending = null;
          reject(new Error("Support unavailable"));
        };
        cancelPending = fail;
        script.onerror = fail;
        win.CRISP_READY_TRIGGER = () => {
          win.clearTimeout(timeout);
          cancelPending = null;
          ready = true;
          // Never open from this callback: a timeout/logout may have occurred.
          push("do", "chat:hide");
          resolve();
        };
      });
      win.document.head.appendChild(script);
    }
    if (!ready) await pending;
    if (owner !== requestedOwner) throw new Error("Support session changed");
    push("do", "chat:show");
    push("do", "chat:open");
  }

  return { setOwner, open };
}

export const trialSupport = typeof window === "undefined" ? null : createTrialSupport();
