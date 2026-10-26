# MediaPipe person matte (live preview, deck layer "background")

- `selfie_segmenter_landscape.tflite` — MediaPipe Selfie Segmenter, landscape (144×256 input), float16, from
  `https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter_landscape/float16/latest/selfie_segmenter_landscape.tflite`
  (fetched 2026-10-26, sha256 `490e9ea734313e0de10fa0cd9e3c6133e36ea4db2b7a49bde9ef019f72796b8e`, 250 177 bytes).
  Licence: Apache-2.0 (model card; `LICENSE` here is the MediaPipe repository's Apache-2.0 text).
- The runtime is `@mediapipe/tasks-vision@1.0.1` (Apache-2.0), loaded on demand by `app/static/live-player.js` from
  `https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.0.1/` (vision_bundle.mjs + wasm/) only when a scene has a
  background deck. To run without the CDN, copy that package's `vision_bundle.mjs` and `wasm/` here and point
  `LivePlayer.MP_CDN` at `/static/vendor/mediapipe`.
