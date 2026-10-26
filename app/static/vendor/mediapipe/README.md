# MediaPipe person matte (live preview, deck layer "background")

- `selfie_segmenter_landscape.tflite` — MediaPipe Selfie Segmenter, landscape (144×256 input), float16, from
  `https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter_landscape/float16/latest/selfie_segmenter_landscape.tflite`
  (fetched 2026-10-26, sha256 `490e9ea734313e0de10fa0cd9e3c6133e36ea4db2b7a49bde9ef019f72796b8e`, 250 177 bytes).
  Licence: Apache-2.0 (model card; `LICENSE` here is the MediaPipe repository's Apache-2.0 text).
- `tasks-vision/` — `@mediapipe/tasks-vision@1.0.1` (Apache-2.0, its `package.json` and `README.md` kept), self-hosted:
  `vision_bundle.mjs` (sha256 `d885630c…713f`) and the ES-module loader `wasm/vision_wasm_module_internal.{js,wasm}`
  (sha256 `da893405…f82d`, `2dabd8e2…3c1b`), from `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.0.1/`. The player
  and the worker load it with `FilesetResolver.forVisionTasks(base, true)` (the module loader), so the classic and the
  no-SIMD loaders are not shipped: a browser without WebAssembly SIMD gets no matte (the speaker isn't cut out there).
- The server's final render uses the same model file (app/tools/deck_layer.py, sha256 checked) with MediaPipe 1.1.1
  (Python, Dockerfile).
