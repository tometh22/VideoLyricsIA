# Editor browser tests

Run from `lyricgen/frontend`:

```bash
npm run test:e2e:install
npm run test:e2e
```

The suite runs the real React/Vite application in Chromium and mocks only the
authenticated API and object-storage upload. This keeps editor interactions
deterministic while exercising pointer events, audio seeking, autosave,
selection, versions and responsive layout in a real browser.
