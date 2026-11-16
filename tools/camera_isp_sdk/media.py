# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 T3 Foundation
"""Media controller set-up on the target.

Finds sensors described by a descriptor in the media graphs, propagates the
descriptor's mode through the CSI-2 bridge and receiver, sets the frame
length the descriptor asks for, and creates the /dev/video-<id>-camN and
/dev/v4l-<id>-subdevN links that TI's Edge AI applications expect. Mirrors
the direct-attach branch of TI's setup_cameras.sh, driven by descriptors
instead of per-sensor code.
"""

import glob
import os
import re
import subprocess
from dataclasses import dataclass, field

ENTITY_RE = re.compile(r"^- entity (\d+): (.+?) \(\d+ pads?")
DEVNODE_RE = re.compile(r"device node name (\S+)")
# media-ctl from v4l-utils 1.28 (the T3 Edge AI image) prints SINK/SOURCE,
# older releases Sink/Source.
PAD_RE = re.compile(r"^\s*pad(\d+): (Sink|Source)", re.IGNORECASE)
LINK_RE = re.compile(r'^\s*(->|<-) "([^"]+)":(\d+) \[([^\]]*)\]')
FMT_RE = re.compile(r"fmt:(\w+)/(\d+)x(\d+)")
CTRL_RE = re.compile(r"^(\w+): (-?\d+)$", re.M)
# setup warns when the driver's timing is this far from [mode] fps
FPS_TOLERANCE = 0.05


class MediaError(Exception):
    pass


@dataclass
class Link:
    direction: str
    entity: str
    pad: int
    flags: str

    @property
    def enabled(self):
        return "ENABLED" in self.flags


@dataclass
class Pad:
    index: int
    kind: str
    fmt: tuple | None = None
    links: list = field(default_factory=list)


@dataclass
class Entity:
    id: int
    name: str
    devnode: str | None = None
    pads: dict = field(default_factory=dict)

    def remote(self, pad=0, direction="->"):
        p = self.pads.get(pad)
        for link in (p.links if p else []):
            if link.direction == direction and link.enabled:
                return link
        return None


def parse_topology(text):
    """Parses `media-ctl -p` output into {name: Entity}."""
    entities = {}
    entity = pad = None
    for line in text.splitlines():
        m = ENTITY_RE.match(line)
        if m:
            entity = Entity(int(m.group(1)), m.group(2))
            entities[entity.name] = entity
            pad = None
            continue
        if entity is None:
            continue
        m = DEVNODE_RE.search(line)
        if m and pad is None:
            entity.devnode = m.group(1)
            continue
        m = PAD_RE.match(line)
        if m:
            pad = Pad(int(m.group(1)), m.group(2).capitalize())
            entity.pads[pad.index] = pad
            continue
        if pad is None:
            continue
        m = LINK_RE.match(line)
        if m:
            pad.links.append(Link(m.group(1), m.group(2), int(m.group(3)), m.group(4)))
            continue
        m = FMT_RE.search(line)
        if m and pad.fmt is None:
            pad.fmt = (m.group(1), int(m.group(2)), int(m.group(3)))
    return entities


def _run(tool, device, *args):
    try:
        return subprocess.run([tool, "-d", device, *args], check=True,
                              capture_output=True, text=True).stdout
    except FileNotFoundError:
        raise MediaError(f"{tool} not found; install v4l-utils") from None
    except subprocess.CalledProcessError as e:
        raise MediaError(f"{tool} -d {device} {' '.join(args)}: {e.stderr.strip()}") from None


def _media_ctl(device, *args):
    return _run("media-ctl", device, *args)


def _v4l2_ctl(device, *args):
    return _run("v4l2-ctl", device, *args)


def video_link(sensor_id, index):
    """The /dev link to the capture video node of camera index of a sensor."""
    return f"/dev/video-{sensor_id}-cam{index}"


def subdev_link(sensor_id, index):
    """The /dev link to the V4L2 subdev of camera index of a sensor."""
    return f"/dev/v4l-{sensor_id}-subdev{index}"


@dataclass
class Camera:
    sensor: object
    index: int
    media: str
    entity: str
    subdev: str
    bridge: str
    receiver: str
    video: str
    active_fmt: tuple | None = None
    frame_rate: float | None = None

    @property
    def video_link(self):
        return video_link(self.sensor.id, self.index)

    @property
    def subdev_link(self):
        return subdev_link(self.sensor.id, self.index)

    def links_ok(self):
        """Whether both /dev links exist and point at this camera's nodes."""
        return all(os.path.realpath(link) == os.path.realpath(target)
                   for link, target in ((self.video_link, self.video), (self.subdev_link, self.subdev)))


def find_cameras(sensors, media_devices=None):
    """Finds every descriptor-backed sensor in the media graphs."""
    cameras = []
    counts = {}
    for device in media_devices or sorted(glob.glob("/dev/media*")):
        entities = parse_topology(_media_ctl(device, "-p"))
        for entity in entities.values():
            sensor = next((s for s in sensors.values() if entity.name.startswith(f"{s.entity} ")), None)
            if sensor is None:
                continue
            bridge_link = entity.remote(0)
            if bridge_link is None or "csi-bridge" not in bridge_link.entity:
                raise MediaError(f"{device}: '{entity.name}' is not linked to a CSI-2 bridge; "
                                 "serializer (FPD-Link/GMSL) set-ups are not handled")
            bridge = entities[bridge_link.entity]
            receiver_link = next((link for p in bridge.pads.values() if p.kind == "Source"
                                  for link in p.links if link.enabled and "ticsi2rx" in link.entity), None)
            if receiver_link is None:
                raise MediaError(f"{device}: '{bridge.name}' is not linked to a TI CSI-2 receiver")
            receiver = entities[receiver_link.entity]
            context = entities.get(f"{receiver.name} context 0")
            if context is None or context.devnode is None:
                raise MediaError(f"{device}: '{receiver.name} context 0' video node not found")
            index = counts.get(sensor.id, 0)
            counts[sensor.id] = index + 1
            cameras.append(Camera(sensor, index, device, entity.name, entity.devnode,
                                  bridge.name, receiver.name, context.devnode))
    return cameras


def frame_rate(subdev, width, height):
    """Frame rate the sensor's PIXEL_RATE, HBLANK and VBLANK controls give.

    None if the driver lacks one of them.
    """
    try:
        out = _v4l2_ctl(subdev, "--get-ctrl=pixel_rate,horizontal_blanking,vertical_blanking")
    except MediaError:
        return None
    ctrls = {name: int(value) for name, value in CTRL_RE.findall(out)}
    try:
        frame = (width + ctrls["horizontal_blanking"]) * (height + ctrls["vertical_blanking"])
        return ctrls["pixel_rate"] / frame if frame > 0 else None
    except KeyError:
        return None


def configure(camera, dry_run=False, log=print):
    """Sets the descriptor mode along sensor -> bridge -> receiver.

    Then sets [mode] vblank, if given, and links the camera's /dev nodes.
    The links are only made once the driver took the mode, so they never
    point at a camera left in another format.
    """
    mode = camera.sensor.mode
    fmt = mode.media_ctl_format
    commands = [
        ["--set-v4l2", f'"{camera.entity}":0 {fmt}'],
        ["--set-v4l2", f'"{camera.bridge}":0 {fmt}'],
        ["--set-v4l2", f'"{camera.receiver}":0 {fmt}'],
    ]
    for args in commands:
        log(f"media-ctl -d {camera.media} {args[0]} '{args[1]}'")
        if not dry_run:
            _media_ctl(camera.media, *args)

    wanted = (mode.code, mode.width, mode.height)
    if dry_run:
        camera.active_fmt = wanted
    else:
        out = _media_ctl(camera.media, "--get-v4l2", f'"{camera.entity}":0')
        m = FMT_RE.search(out)
        camera.active_fmt = (m.group(1), int(m.group(2)), int(m.group(3))) if m else None
    if camera.active_fmt != wanted:
        raise MediaError(f"{camera.entity}: driver chose {camera.active_fmt}, descriptor wants {wanted}; "
                         f"fix [mode] in {camera.sensor.path}")

    # Setting the format resets the frame length to the driver's default
    if mode.vblank is not None:
        log(f"v4l2-ctl -d {camera.subdev} --set-ctrl=vertical_blanking={mode.vblank}")
        if not dry_run:
            _v4l2_ctl(camera.subdev, f"--set-ctrl=vertical_blanking={mode.vblank}")
    if not dry_run:
        camera.frame_rate = frame_rate(camera.subdev, mode.width, mode.height)

    for link, target in ((camera.video_link, camera.video), (camera.subdev_link, camera.subdev)):
        log(f"ln -snf {target} {link}")
        if dry_run:
            continue
        tmp = f"{link}.tmp"
        try:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            os.symlink(target, tmp)
            os.replace(tmp, link)
        except OSError as e:
            raise MediaError(f"cannot link {link} -> {target}: {e.strerror} (run setup as root)") from None
    return camera
