# Bundled models

DeskGuard uses these models from the [OpenCV model zoo](https://github.com/opencv/opencv_zoo).
They run entirely on your PC through OpenCV's DNN module. Each is loaded only
while the feature that needs it is switched on.

| File | Purpose | Source | Licence |
|---|---|---|---|
| `face_detection_yunet_2023mar.onnx` | Finds faces (YuNet) | `models/face_detection_yunet` | MIT |
| `face_recognition_sface_2021dec_int8.onnx` | Tells faces apart (SFace, int8) | `models/face_recognition_sface` | Apache License 2.0 |
| `human_segmentation_pphumanseg_2023mar.onnx` | Body outlines (PP-HumanSeg) | `models/human_segmentation_pphumanseg` | Apache License 2.0 |
| `palm_detection_mediapipe_2023feb.onnx` | Finds hands (MediaPipe palm detector) | `models/palm_detection_mediapipe` | Apache License 2.0 |
| `handpose_estimation_mediapipe_2023feb.onnx` | 21 hand points (MediaPipe hand landmarks) | `models/handpose_estimation_mediapipe` | Apache License 2.0 |

SHA-256 checksums are pinned in `tools/fetch_models.py`. The full licence texts are in
the `LICENSE_<model>.txt` files next to the models. The palm/hand pre- and
post-processing in `deskguard/gestures.py` is adapted from the zoo's
`mp_palmdet.py` and `mp_handpose.py` (Apache License 2.0, Copyright (C) 2021,
Shenzhen Institute of Artificial Intelligence and Robotics for Society).

- **YuNet**, Copyright (c) 2020 Shiqi Yu. Released under the MIT License.
- **SFace**, by Yaoyao Zhong and Weihong Deng ("SFace: Sigmoid-Constrained
  Hypersphere Loss for Robust Face Recognition", IEEE TIP 2021). Released
  under the Apache License 2.0: https://www.apache.org/licenses/LICENSE-2.0
