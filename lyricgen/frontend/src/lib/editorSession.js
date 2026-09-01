const KEY = "genly_editor_tab_session";

export function getEditorSessionId() {
  if (typeof window === "undefined") return "server-session";
  let value = window.sessionStorage.getItem(KEY);
  if (!value) {
    value = globalThis.crypto?.randomUUID?.()
      || `tab-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    window.sessionStorage.setItem(KEY, value);
  }
  return value;
}

export function editorSessionHeaders(headers = {}) {
  return { ...headers, "X-Editor-Session": getEditorSessionId() };
}
