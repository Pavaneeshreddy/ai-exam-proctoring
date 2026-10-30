"""Server-side analysis of one webcam frame.

The browser streams a frame every few seconds. This module is the
authoritative check (client-side detections can be tampered with):

* camera health  – blocked/covered lens, too dark
* face presence  – none / one / several faces, face too small (too far)
* identity       – similarity of the visible face to the photo taken at the
                   start of the exam (the same person must stay in front of
                   the camera for the whole exam)
* head pose      – yaw/pitch from facial landmarks
* objects        – YOLOv8 phones, books, extra screens, extra persons
"""

import base64
import binascii

import cv2
import numpy as np


def decode_image(data):
    """Accepts a data-URL / base64 string or raw bytes and returns a BGR image."""
    if data is None:
        return None
    if isinstance(data, str):
        if "," in data[:100]:
            data = data.split(",", 1)[1]
        try:
            data = base64.b64decode(data)
        except (binascii.Error, ValueError):
            return None
    arr = np.frombuffer(data, dtype=np.uint8)
    if arr.size == 0:
        return None
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def frame_quality(img):
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (160, 120))
    return {
        "brightness": round(float(small.mean()), 1),
        "contrast": round(float(small.std()), 1),
        "sharpness": round(float(cv2.Laplacian(small, cv2.CV_64F).var()), 1),
    }


def analyze_frame(img, face_analyzer, object_detector=None, reference=None, exam=None):
    result = {"faces": 0, "identity": None, "pose": None, "objects": None, "quality": None, "events": []}
    events = result["events"]
    quality = frame_quality(img)
    result["quality"] = quality

    if quality["contrast"] < 9 and quality["sharpness"] < 15:
        events.append(("camera_blocked", 0.9, {"brightness": quality["brightness"], "contrast": quality["contrast"]}))
        return result
    if quality["brightness"] < 35:
        events.append(("low_light", 0.8, {"brightness": quality["brightness"]}))

    faces = face_analyzer.detect(img)
    result["faces"] = len(faces)
    h, w = img.shape[:2]

    if not faces:
        profile = face_analyzer.detect_profile(img)
        if profile:
            result["pose"] = {"profile": True}
            events.append(("head_turned", 0.7, {"source": "profile-detector"}))
        else:
            events.append(("face_missing", 0.85, {}))
    else:
        if len(faces) > 1:
            # ignore tiny detections (posters, reflections) relative to the main face
            main = faces[0].area
            significant = [f for f in faces if f.area >= 0.25 * main]
            if len(significant) > 1:
                events.append(("multiple_faces", 0.9, {"count": len(significant)}))
                result["faces"] = len(significant)
        main = faces[0]
        if main.area < 0.012 * w * h:
            events.append(("face_too_far", 0.7, {"area_ratio": round(main.area / (w * h), 4)}))

        pose = face_analyzer.estimate_pose(main)
        result["pose"] = pose
        if pose and (abs(pose["yaw"]) > 35 or pose["pitch"] > 40):
            events.append(("head_turned", 0.75, pose))

        if reference is not None:
            emb = face_analyzer.embed(img, main)
            sim = face_analyzer.similarity(reference, emb)
            if sim is not None:
                result["identity"] = {
                    "similarity": round(sim, 3),
                    "match": face_analyzer.is_match(sim),
                    "threshold": face_analyzer.threshold,
                    "backend": face_analyzer.backend,
                }

    if object_detector is not None and (exam is None or exam.monitor_objects):
        detected = object_detector.detect(img)
        if detected is not None:
            result["objects"] = detected
            for item in detected["items"]:
                if item["event"] == "book_detected" and exam is not None and exam.mode == "written":
                    continue  # answer booklet is expected in written mode
                events.append((item["event"], item["confidence"], {"label": item["label"], "source": "yolov8"}))
            if detected["persons"] > 1:
                events.append(("multiple_persons", 0.85, {"persons": detected["persons"], "source": "yolov8"}))
    return result


def encode_jpeg(img, quality=80):
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes() if ok else None
