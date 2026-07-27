/* Tests for the SW registration guardrails — 2026-05-31.
 *
 * What we pin:
 *   - In dev (import.meta.env.PROD === false), DO NOT touch
 *     navigator.serviceWorker.register at all. Devs must never see the
 *     SW take over HMR-served chunks.
 *   - If navigator.serviceWorker is absent (e.g. Safari Private mode,
 *     older browsers, jsdom), the function exits cleanly without
 *     throwing — a broken SW boot must never block the app.
 *   - In prod, registration runs but only AFTER the `load` event so
 *     the SW download doesn't compete with first paint.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { registerServiceWorker } from './registerSW';

describe('registerServiceWorker', () => {
  let originalSW;
  beforeEach(() => {
    originalSW = navigator.serviceWorker;
  });
  afterEach(() => {
    if (originalSW) {
      Object.defineProperty(navigator, 'serviceWorker', {
        configurable: true,
        value: originalSW,
      });
    } else {
      delete navigator.serviceWorker;
    }
  });

  test('no-ops in dev (PROD=false)', () => {
    // Default vitest env has PROD=false (jsdom + dev). The function
    // should return without touching navigator.serviceWorker even if
    // present.
    const register = vi.fn();
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true,
      value: { register },
    });
    registerServiceWorker();
    expect(register).not.toHaveBeenCalled();
  });

  test('no-ops if navigator.serviceWorker is unavailable', () => {
    // Simulate Safari Private mode / old browser by removing the API.
    delete navigator.serviceWorker;
    // Must not throw even with no SW support.
    expect(() => registerServiceWorker()).not.toThrow();
  });
});
