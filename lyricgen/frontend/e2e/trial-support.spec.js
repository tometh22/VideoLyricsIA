import { expect, test } from "@playwright/test";

// Real browser + real React/support code; deterministic vendor transport.
// No customer login, customer credits, production API or Crisp messages in CI.
const origin = "https://trial.genly.pro";
const fixture = `<!doctype html><html><body data-browsing-ignore><div id="root"></div>
<script type="module">
import RefreshRuntime from '/@react-refresh';
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {}; window.$RefreshSig$ = () => (type) => type;
window.__vite_plugin_react_preamble_installed__ = true;
const { default: React } = await import('/node_modules/.vite/deps/react.js');
const { default: ReactDOM } = await import('/node_modules/.vite/deps/react-dom_client.js');
const { default: Chat } = await import('/src/components/TrialSupportChat.jsx');
const root = ReactDOM.createRoot(document.getElementById('root'));
window.setSupportUser = (userKey) => root.render(React.createElement(React.StrictMode, null, React.createElement(Chat, { userKey })));
window.setSupportUser('qa:a');
</script></body></html>`;

const sdk = `window.supportCommands = [...window.$crisp];
window.$crisp = { push(command) {
  window.supportCommands.push(command);
  if(command[1] === 'chat:open') document.getElementById('vendor-chat').hidden = false;
  if(command[1] === 'chat:hide') document.getElementById('vendor-chat').hidden = true;
}};
const panel = document.createElement('div'); panel.id = 'vendor-chat'; panel.hidden = true;
panel.textContent = 'Vendor chat open'; document.body.append(panel);
window.CRISP_READY_TRIGGER();`;

async function mount(page, { host = origin, environment = "trial", blocked = false } = {}) {
  let loads = 0;
  await page.route("https://client.crisp.chat/l.js", async (route) => {
    loads += 1;
    if (blocked) await route.abort();
    else await route.fulfill({ contentType: "application/javascript", body: sdk });
  });
  await page.route(`${host}/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/") return route.fulfill({ contentType: "text/html", body: fixture });
    if (path === "/@vite/client") return route.fulfill({ contentType: "application/javascript", body: "export const injectQuery = (url) => url; export function createHotContext() { return { accept() {}, dispose() {}, invalidate() {}, prune() {}, on() {}, send() {} }; }" });
    if (path === "/src/env.js") return route.fulfill({ contentType: "application/javascript", body: `export const APP_ENV = ${JSON.stringify(environment)};` });
    const url = new URL(route.request().url());
    const response = await route.fetch({ url: `http://127.0.0.1:4173${url.pathname}${url.search}` });
    return route.fulfill({ response });
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto(host);
  await expect.poll(async () => errors.length ? errors.join("; ") : page.evaluate(() => typeof window.setSupportUser), { timeout: 8000 }).toBe("function");
  return { loads: () => loads };
}

test("support is opt-in, stable across remount, and reset on account change", async ({ page }) => {
  const harness = await mount(page);
  await expect(page.getByRole("button", { name: "Soporte Genly" })).toBeVisible();
  expect(harness.loads()).toBe(0);
  await page.getByRole("button", { name: "Soporte Genly" }).click();
  expect(harness.loads()).toBe(0);
  await page.getByRole("button", { name: "Abrir chat" }).click();
  await expect(page.locator("#vendor-chat")).toBeVisible();
  const firstToken = await page.evaluate(() => window.CRISP_TOKEN_ID);
  expect(harness.loads()).toBe(1);
  await page.evaluate(() => window.setSupportUser(null));
  await expect(page.locator("#vendor-chat")).toBeHidden();
  expect(await page.evaluate(() => window.CRISP_TOKEN_ID)).toBeUndefined();
  await page.evaluate(() => window.setSupportUser("qa:b"));
  await page.getByRole("button", { name: "Soporte Genly" }).click();
  await page.getByRole("button", { name: "Abrir chat" }).click();
  await expect(page.locator("#vendor-chat")).toBeVisible();
  expect(await page.evaluate(() => window.CRISP_TOKEN_ID)).not.toBe(firstToken);
  expect(harness.loads()).toBe(1);
});

test("blocked vendor leaves an email fallback and usable page", async ({ page }) => {
  await mount(page, { blocked: true });
  await page.getByRole("button", { name: "Soporte Genly" }).click();
  await page.getByRole("button", { name: "Abrir chat" }).click();
  await expect(page.getByRole("alert")).toContainText("No pudimos abrir el chat");
  await expect(page.getByRole("link", { name: "Contactar por email" })).toBeVisible();
  await page.getByRole("button", { name: "Cerrar ayuda" }).click();
  await expect(page.getByRole("button", { name: "Soporte Genly" })).toBeVisible();
});

for (const options of [{ environment: "production" }, { host: "https://staging.genly.pro" }]) {
  test(`support never loads outside its exact trial gate ${JSON.stringify(options)}`, async ({ page }) => {
    const harness = await mount(page, options);
    await expect(page.getByRole("button", { name: "Soporte Genly" })).toHaveCount(0);
    expect(harness.loads()).toBe(0);
  });
}
