"""Download the AI models used by ProctorAI.

    python scripts/download_models.py          # server face models (YuNet + SFace)
    python scripts/download_models.py --web    # also self-host the browser AI (MediaPipe)

Server: YuNet (face detection) and SFace (face recognition) from the OpenCV
model zoo. Without them the server falls back to Haar cascades + LBP.

Browser (--web): the MediaPipe Tasks Vision library, its WebAssembly runtime
and the face-landmarker / object-detector models are copied into
app/static/vendor so exams do not depend on a CDN being reachable.
"""

import io
import os
import sys
import tarfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
SERVER_MODELS = {
    "face_detection_yunet_2023mar.onnx": f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx": f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}
TASKS_VISION_TGZ = "https://registry.npmjs.org/@mediapipe/tasks-vision/-/tasks-vision-0.10.14.tgz"
MP = "https://storage.googleapis.com/mediapipe-models"
WEB_MODELS = {
    "face_landmarker.task": f"{MP}/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
    "efficientdet_lite0.tflite": f"{MP}/object_detector/efficientdet_lite0/float16/1/efficientdet_lite0.tflite",
}


def fetch(url, path, min_size=100_000):
    if os.path.exists(path) and os.path.getsize(path) >= min_size:
        print(f"✓ {os.path.basename(path)} already present")
        return
    print(f"↓ {os.path.basename(path)} ...", end=" ", flush=True)
    urllib.request.urlretrieve(url, path)
    size = os.path.getsize(path)
    if size < min_size:  # e.g. a git-lfs pointer instead of the model
        os.remove(path)
        raise RuntimeError(f"received a placeholder file; download manually from {url}")
    print(f"{size / 1e6:.1f} MB")


def server_models():
    target = os.environ.get("MODELS_DIR") or os.path.join(ROOT, "models")
    os.makedirs(target, exist_ok=True)
    for name, url in SERVER_MODELS.items():
        fetch(url, os.path.join(target, name))


def web_assets():
    vendor = os.path.join(ROOT, "app", "static", "vendor")
    lib_dir = os.path.join(vendor, "tasks-vision")
    if not os.path.exists(os.path.join(lib_dir, "vision_bundle.mjs")):
        print("↓ @mediapipe/tasks-vision ...", end=" ", flush=True)
        data = urllib.request.urlopen(TASKS_VISION_TGZ).read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                name = member.name.removeprefix("package/")
                if member.isfile() and (name == "vision_bundle.mjs" or name.startswith("wasm/")):
                    dest = os.path.join(lib_dir, name)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as fh:
                        fh.write(tar.extractfile(member).read())
        print("done")
    else:
        print("✓ tasks-vision already present")
    models_dir = os.path.join(vendor, "mediapipe-models")
    os.makedirs(models_dir, exist_ok=True)
    for name, url in WEB_MODELS.items():
        fetch(url, os.path.join(models_dir, name))


def main():
    steps = [server_models] + ([web_assets] if "--web" in sys.argv else [])
    failed = False
    for step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 - report any network failure and keep going
            print(f"failed: {exc}")
            failed = True
    print("Done. Restart the server to pick up the models." if not failed else "Finished with errors (see above).")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
