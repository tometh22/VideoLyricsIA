// Lazy-loaded: keep WASM and its worker out of the app's initial bundle.
import JASSUB from "jassub";
import workerUrl from "jassub/dist/worker/worker.js?worker&url";
import wasmUrl from "jassub/dist/wasm/jassub-worker.wasm?url";
import modernWasmUrl from "jassub/dist/wasm/jassub-worker-modern.wasm?url";
import wasmWorkerUrl from "jassub/dist/wasm/jassub-worker.js?url";
import modernWasmWorkerUrl from "jassub/dist/wasm/jassub-worker-modern.js?url";

export function createSubtitleRenderer(canvas, ass, fonts, height = 1080) {
  return new JASSUB({ canvas, subContent: ass, fonts, queryFonts: false,
    defaultFont: "Montserrat", workerUrl, wasmUrl, modernWasmUrl,
    wasmWorkerUrl, modernWasmWorkerUrl, prescaleFactor: 1 / (window.devicePixelRatio || 1),
    prescaleHeightLimit: height, maxRenderHeight: height });
}
