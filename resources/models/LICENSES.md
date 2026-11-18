# Bundled face models

DeskGuard uses these two models from the [OpenCV model zoo](https://github.com/opencv/opencv_zoo).
They run entirely on your PC through OpenCV's DNN module. They are loaded only
while *Watch my back* or face setup is in use.

| File | Purpose | Source | Licence |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | Finds faces (YuNet) | `models/face_detection_yunet` | MIT |
| `face_recognition_sface_2021dec_int8.onnx` | Tells faces apart (SFace, int8) | `models/face_recognition_sface` | Apache License 2.0 |

SHA-256 checksums are pinned in `tools/fetch_models.py`. The full licence texts are in
`LICENSE_face_detection_yunet.txt` and `LICENSE_face_recognition_sface.txt`.

- **YuNet**, Copyright (c) 2020 Shiqi Yu. Released under the MIT License.
- **SFace**, by Yaoyao Zhong and Weihong Deng ("SFace: Sigmoid-Constrained
  Hypersphere Loss for Robust Face Recognition", IEEE TIP 2021). Released
  under the Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0
