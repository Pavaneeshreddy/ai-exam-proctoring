from flask import current_app

from .face import FaceAnalyzer
from .objects import ObjectDetector


def init_ai(app):
    app.extensions["face_analyzer"] = FaceAnalyzer(app.config["MODELS_DIR"])
    app.extensions["object_detector"] = ObjectDetector(app.config["YOLO_MODEL"], app.config["ENABLE_YOLO"])


def face_analyzer():
    return current_app.extensions["face_analyzer"]


def object_detector():
    return current_app.extensions["object_detector"]
