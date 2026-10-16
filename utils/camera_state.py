"""Whether the camera is there, for code that only needs to ask.

The camera service (core/camera/service.py) finds the camera, notices it
going away and owns it; this is a thin view of it kept for the callers
that used the old scanner.
"""
from core.camera.service import get_camera_service


class CameraState:
    def is_available(self) -> bool:
        return get_camera_service().is_available()

    def get_last_check_time(self) -> float:
        return 0.0


camera_state = CameraState()
