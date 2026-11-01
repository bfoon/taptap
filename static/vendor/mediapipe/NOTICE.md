# MediaPipe (vendored for call video effects)

Used by `static/js/call-fx.js` for background blur/replacement (Selfie Segmentation) and face masks (Face Mesh).
Everything runs in the caller's browser: no video frame is sent anywhere for these effects.

* `selfie_segmentation/` — @mediapipe/selfie_segmentation 0.1.1675465747
* `face_mesh/` — @mediapipe/face_mesh 0.4.1633559619

Only the WebAssembly SIMD builds are kept (Chrome/Edge 91+, Firefox 89+, Safari 16.4+). On an older browser
without SIMD, call-fx.js loads the plain build of the same pinned version from cdn.jsdelivr.net instead.

Copyright Google LLC. Licensed under the Apache License, Version 2.0 — https://www.apache.org/licenses/LICENSE-2.0
