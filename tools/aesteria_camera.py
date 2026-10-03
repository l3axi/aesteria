#!/usr/bin/env python3
"""aesteria_camera -- read the live camera and project world points to the screen.

THE CAMERA (SDK offsets, read live 2026-10-01)
    PlayerController +1016 -> PlayerCameraManager (AionPlayerCameraManager)
    PlayerCameraManager +6768 = CameraCachePrivate (FCameraCacheEntry); POV at +16 inside it:
        +6784 Location (3 doubles)   +6808 Rotation (pitch, yaw, roll doubles)
        +6832 FOV (float, HORIZONTAL degrees)   +6856 AspectRatio (float)
    Read live: FOV 85.5, aspect 1.778, camera 16 m behind and above the pawn, pitch -21.

VERIFIED ONCE, LIVE (2026-10-01 06:05): HiddenCube_01_V04 1.4 m ahead projected to
    x 50.1 %, y 58.3 % of the screen; the operator: "just below my crosshair". One point, near centre --
    it proves the axes and signs, NOT the FOV scale toward the edges. An off-centre check is next.

UE FRAME: left-handed. X forward, Y right, Z up; the rotation matrix below is FRotationMatrix
    with roll taken as 0 (the game camera never rolls in normal play).
"""

from __future__ import annotations

import math
import struct

PC_CAMERA_MANAGER = 1016
CAM_POV = 6768 + 16
POV_LOCATION = 0
POV_ROTATION = 24
POV_FOV = 48
POV_ASPECT = 72


def _f32(src, addr: int) -> float:
    return struct.unpack("<f", struct.pack("<I", src.u32(addr) or 0))[0]


def _vec(src, addr: int):
    raw = src.read(addr, 24)
    return struct.unpack("<3d", raw) if raw and len(raw) == 24 else None


class Camera:
    """One snapshot of the view. Re-read every frame: the camera moves continuously."""

    def __init__(self, loc, rot, fov: float, aspect: float):
        self.loc, self.fov, self.aspect = loc, fov, aspect
        p, y = math.radians(rot[0]), math.radians(rot[1])
        self.fwd = (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p))
        self.right = (-math.sin(y), math.cos(y), 0.0)
        self.up = (-math.sin(p) * math.cos(y), -math.sin(p) * math.sin(y), math.cos(p))
        self.tan_half = math.tan(math.radians(fov / 2))

    @classmethod
    def read(cls, src, controller: int) -> "Camera | None":
        cm = src.u64(controller + PC_CAMERA_MANAGER)
        if not cm:
            return None
        loc, rot = _vec(src, cm + CAM_POV + POV_LOCATION), _vec(src, cm + CAM_POV + POV_ROTATION)
        fov, aspect = _f32(src, cm + CAM_POV + POV_FOV), _f32(src, cm + CAM_POV + POV_ASPECT)
        if not loc or not rot or not (10 < fov < 170) or not (0.5 < aspect < 4):
            return None
        return cls(loc, rot, fov, aspect)

    def project(self, world, width: int, height: int):
        """(px, py, depth_units) or None when the point is behind the camera.

        Horizontal FOV, so the vertical half-extent is tan_half / aspect. The result may lie
        off-screen; callers clip.
        """
        d = [world[i] - self.loc[i] for i in range(3)]
        x = sum(a * b for a, b in zip(d, self.fwd))
        if x <= 1.0:
            return None
        y = sum(a * b for a, b in zip(d, self.right))
        z = sum(a * b for a, b in zip(d, self.up))
        nx = y / (x * self.tan_half)
        ny = z * self.aspect / (x * self.tan_half)
        return (width / 2) * (1 + nx), (height / 2) * (1 - ny), x
