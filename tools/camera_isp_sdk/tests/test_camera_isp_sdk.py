# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 T3 Foundation
"""Unit tests for the camera-isp-sdk tool. Run: python3 -m unittest discover tools"""

import hashlib
import io
import os
import shutil
import struct
import subprocess
import tarfile
import tempfile
import textwrap
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest import mock

from camera_isp_sdk import board as board_mod
from camera_isp_sdk import checks, cli, dcc, descriptor, install, media, overlay, pipeline
from camera_isp_sdk.conf import ConfError, parse_float, parse_int

ROOT = Path(__file__).resolve().parents[3]
BOARD = board_mod.load(ROOT / "boards" / "t3-gem-o1")

MINIMAL = """\
[sensor]
id=cam
[mode]
width=1920
height=1080
code=SRGGB10_1X10
[csi]
lanes=2
link-frequency=456000000
[overlay]
ports=csi0
[isp]
dcc-id=1234
[ae]
range0=100;33000;1024;8192;256;256
[gain]
model=inverse
k=256
"""


def write_sensor(tmp, text, name="cam"):
    d = Path(tmp) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "sensor.conf").write_text(text)
    (d / "overlay.dtsi").write_text("/* sensor fragment */\n")
    return d


def copy_root(tmp, sensors=("imx230",)):
    """A data root in tmp with the board, the template and some sensors."""
    tmp = Path(tmp)
    for d in ("boards", "include", "sensors/_template", *(f"sensors/{s}" for s in sensors)):
        shutil.copytree(ROOT / d, tmp / d)
    return tmp


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
        status = cli.main([str(a) for a in argv])
    return status, out.getvalue(), err.getvalue()


class DescriptorTest(unittest.TestCase):
    def test_shipped_sensors_load(self):
        sensors = descriptor.discover(ROOT / "sensors")
        self.assertIn("imx477", sensors)
        imx477 = sensors["imx477"]
        self.assertEqual(imx477.mode.gst_format, "rggb12")
        self.assertEqual(imx477.format_msb, 11)
        self.assertEqual(imx477.dcc_path("viss"), "/opt/imaging/imx477/linear/dcc_viss.bin")
        self.assertFalse(imx477.has_ldc)
        self.assertTrue(sensors["imx230"].has_ldc)
        ov5647 = sensors["ov5647"]
        self.assertEqual(ov5647.mode.gst_format, "bggr10")
        self.assertEqual(ov5647.format_msb, 9)

    def test_minimal_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = descriptor.load(write_sensor(tmp, MINIMAL))
            self.assertEqual(s.entity, "cam")
            self.assertEqual(s.mode.fps, 30)
            self.assertEqual(s.format_msb, 9)
            self.assertEqual(s.exposure_model, "lines")
            self.assertEqual(s.mode.media_ctl_format, "[fmt:SRGGB10_1X10/1920x1080 field:none]")

    def test_rejects(self):
        cases = {
            "bad id": MINIMAL.replace("id=cam", "id=../cam"),
            "bad code": MINIMAL.replace("SRGGB10_1X10", "UYVY8_1X16"),
            "no dcc id": MINIMAL.replace("dcc-id=1234\n", ""),
            "gain below 1x": MINIMAL.replace("100;33000;1024", "100;33000;512"),
            "short range": MINIMAL.replace("range0=100;33000;1024;8192;256;256", "range0=1;2;3"),
            "inverse without k": MINIMAL.replace("k=256\n", ""),
            "unknown gain model": MINIMAL.replace("model=inverse", "model=magic"),
            "inline comment": MINIMAL.replace("k=256", "k=256 # sony"),
            # What the plugin's g_ascii_strtoll()/g_ascii_strtod() reject
            "digit separator": MINIMAL.replace("1024;8192", "1_024;8192"),
            "binary": MINIMAL.replace("1024;8192", "0b10000000000;8192"),
            "0o octal": MINIMAL.replace("1024;8192", "0o2000;8192"),
            "negative exposure": MINIMAL.replace("range0=100;", "range0=-1;"),
            "exposure above 32 bits": MINIMAL.replace("33000;", "4294967296;"),
            "table gain 0": MINIMAL.replace("model=inverse\nk=256", "model=table\ntable=0:0;2048:16"),
            "nan": MINIMAL.replace("k=256", "k=nan"),
            "inf": MINIMAL.replace("k=256", "k=inf"),
            "float separator": MINIMAL.replace("k=256", "k=2_56"),
        }
        for name, text in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(ConfError):
                    descriptor.load(write_sensor(tmp, text))


    def test_c_number_syntax(self):
        self.assertEqual([parse_int(x) for x in ("0", "42", "-7", "+7", "0x1F", "010")], [0, 42, -7, 7, 31, 8])
        for bad in ("", "08", "0x", "1_0", "0b1", "0o7", " 1", "1.0"):
            with self.subTest(bad), self.assertRaises(ValueError):
                parse_int(bad)
        self.assertEqual([parse_float(x) for x in ("0.3", "-1", "1e3", ".5", "0x1p3")], [0.3, -1, 1000, 0.5, 8])
        for bad in ("nan", "inf", "-Infinity", "1e999", "1_0", ""):
            with self.subTest(bad), self.assertRaises(ValueError):
                parse_float(bad)

    def test_dcc_ldc_needs_sources(self):
        text = (ROOT / "sensors/imx477/sensor.conf").read_text().replace("dcc-id=477", "dcc-id=477\ndcc-ldc=dcc_ldc.bin")
        with tempfile.TemporaryDirectory() as tmp, self.assertRaisesRegex(ConfError, r"\[dcc\] ldc lists none"):
            descriptor.load(write_sensor(tmp, text, "imx477"))

    def test_vblank(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(descriptor.load(write_sensor(tmp, MINIMAL)).mode.vblank)
            s = descriptor.load(write_sensor(tmp, MINIMAL.replace("[csi]", "vblank=413\n[csi]")))
            self.assertEqual(s.mode.vblank, 413)

    def test_search_path_matches_plugin(self):
        header = (ROOT / "gst/src/camera_isp_sdk_sensor.h").read_text()
        self.assertIn(f'#define CAMERA_ISP_SDK_SENSOR_PATH_ENV "{descriptor.SENSOR_PATH_ENV}"', header)
        self.assertIn(f'"{descriptor.SENSOR_PATH_DEFAULT}"', header)

    def test_plugin_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "etc", Path(tmp) / "share"
            (second / "cam").mkdir(parents=True)
            (second / "cam/sensor.conf").write_text("")
            env = {descriptor.SENSOR_PATH_ENV: f"{first}::{second}"}
            self.assertEqual(descriptor.plugin_descriptor("cam", env), second / "cam/sensor.conf")
            first.mkdir()
            (first / "cam.conf").write_text("")
            self.assertEqual(descriptor.plugin_descriptor("cam", env), first / "cam.conf")
            self.assertIsNone(descriptor.plugin_descriptor("other", env))


class ChecksTest(unittest.TestCase):
    def sensor(self, **mode):
        s = descriptor.discover(ROOT / "sensors")["imx477"]
        return replace(s, mode=replace(s.mode, **mode))

    def test_shipped_sensors_pass(self):
        sensors = descriptor.discover(ROOT / "sensors")
        for s in sensors.values():
            errors, _ = checks.check(s, BOARD, sensors)
            self.assertEqual(errors, [], s.id)

    def test_width_alignment(self):
        self.assertEqual(BOARD.width_alignment(8), 16)
        self.assertEqual(BOARD.width_alignment(12), 8)
        errors, _ = checks.check(self.sensor(width=2028, height=1520, fps=40), BOARD)
        self.assertTrue(any("multiple of 8" in e and "2024" in e for e in errors), errors)

    def test_bandwidth(self):
        errors, _ = checks.check(self.sensor(width=4056, height=3040, fps=60), BOARD)
        self.assertTrue(any("needs" in e and "Mbps" in e for e in errors), errors)

    def test_dcc_bit_depth(self):
        errors, _ = checks.check(self.sensor(code="SRGGB10_1X10"), BOARD)
        self.assertTrue(any("12-bit data" in e for e in errors), errors)

    def test_h3a_outside_frame(self):
        errors, _ = checks.check(self.sensor(width=1280, height=720), BOARD)
        self.assertTrue(any("H3A" in e for e in errors), errors)

    def test_cfa_bayer_order(self):
        errors, _ = checks.check(self.sensor(code="SBGGR12_1X12"), BOARD)
        self.assertTrue(any("CFA coefficients are for RGGB" in e and "fit-cfa" in e for e in errors), errors)

    def test_missing_port_header(self):
        ports = {**BOARD.ports, "csi0": replace(BOARD.ports["csi0"], header=Path("/nonexistent/csi0.h"))}
        errors, _ = checks.check(self.sensor(), replace(BOARD, ports=ports))
        self.assertTrue(any("csi0 header" in e for e in errors), errors)


class DccTest(unittest.TestCase):
    def test_fit_reproduces_imx230_grid(self):
        xml = ROOT / "sensors/imx230/dcc/linear/imx230_h3a_aewb_dcc.xml"
        grid = dcc.read_h3a(xml)
        self.assertEqual(dcc.fit_h3a(grid, 1920, 1080), grid)

    def test_fit_other_sizes(self):
        grid = dcc.read_h3a(ROOT / "sensors/imx230/dcc/linear/imx230_h3a_aewb_dcc.xml")
        for w, h in ((2016, 1520), (1328, 990), (4056, 3040), (640, 480)):
            with self.subTest(size=f"{w}x{h}"):
                self.assertEqual(dcc.check_h3a(dcc.fit_h3a(grid, w, h), w, h), [])

    def test_write_h3a_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "h3a.xml"
            shutil.copyfile(ROOT / "sensors/imx230/dcc/linear/imx230_h3a_aewb_dcc.xml", xml)
            fitted = dcc.fit_h3a(dcc.read_h3a(xml), 2016, 1520)
            dcc.write_h3a(xml, fitted)
            self.assertEqual(dcc.read_h3a(xml), fitted)
            self.assertIn("// h_size", xml.read_text().replace("h_size:", "h_size"))

    def test_bootstrap(self):
        template = descriptor.discover(ROOT / "sensors")["imx230"]
        with tempfile.TemporaryDirectory() as tmp:
            renames = dcc.bootstrap(template, "newcam", 4321, tmp, 2016, 1520)
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertIn("newcam_cfa_dcc.xml", names)
            self.assertIn("lut_cfa_global.txt", names)
            self.assertNotIn("newcam_mesh_ldc_dcc.xml", names)  # lens specific
            self.assertEqual(renames["imx230_cfa_dcc.xml"], "newcam_cfa_dcc.xml")
            for xml in Path(tmp).glob("*.xml"):
                self.assertEqual(set(dcc.xml_module_ids(xml)), {4321}, xml.name)
            grid = dcc.read_h3a(Path(tmp) / "newcam_h3a_aewb_dcc.xml")
            self.assertEqual(dcc.check_h3a(grid, 2016, 1520), [])
            self.assertEqual(dcc.read_cfa_bayer(Path(tmp) / "newcam_cfa_dcc.xml"), "rggb")

    def test_bootstrap_includes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_root(tmp)
            src = root / "sensors/imx230/dcc/linear"
            (src / "luts").mkdir()
            shutil.copy(src / "lut_cfa_global.txt", src / "imx230_lut.txt")
            shutil.copy(src / "lut_remap_16bto10b.txt", src / "luts/x.txt")
            shutil.copy(src / "imx230_viss_blc.xml", src / "luts/imx230_blc.xml")
            cfa = src / "imx230_cfa_dcc.xml"
            cfa.write_text(cfa.read_text().replace('#include "lut_cfa_global.txt"', '#include "imx230_lut.txt"'))
            mux = src / "imx230_h3a_mux_luts_dcc.xml"
            mux.write_text(mux.read_text().replace('#include "lut_remap_16bto10b.txt"', '#include "luts/x.txt"'))
            conf = root / "sensors/imx230/sensor.conf"
            conf.write_text(conf.read_text().replace("\nviss=", "\nviss=luts/imx230_blc.xml;"))
            template = descriptor.load(root / "sensors/imx230")

            dest = Path(tmp) / "new"
            renames = dcc.bootstrap(template, "newcam", 4321, dest, 1920, 1080)
            self.assertEqual(renames["imx230_lut.txt"], "newcam_lut.txt")
            self.assertEqual(renames["luts/imx230_blc.xml"], "luts/newcam_blc.xml")
            self.assertEqual(set(dcc.xml_module_ids(dest / "luts/newcam_blc.xml")), {4321})
            self.assertIn('#include "newcam_lut.txt"', (dest / "newcam_cfa_dcc.xml").read_text())
            self.assertIn('#include "luts/x.txt"', (dest / "newcam_h3a_mux_luts_dcc.xml").read_text())
            self.assertTrue((dest / "luts/x.txt").is_file())

            status, out, _ = run_cli("--root", root, "new", "newcam", "--width", 1920, "--height", 1080,
                                     "--code", "SRGGB10_1X10", "--dcc-id", 4321)
            self.assertEqual(status, 0, out)
            status, out, _ = run_cli("--root", root, "validate", "newcam")
            self.assertEqual(status, 0, out)

    def test_bootstrap_bayer(self):
        template = descriptor.discover(ROOT / "sensors")["imx230"]
        with tempfile.TemporaryDirectory() as tmp:
            dcc.bootstrap(template, "newcam", 4321, tmp, 1920, 1080, "grbg")
            self.assertEqual(dcc.read_cfa_bayer(Path(tmp) / "newcam_cfa_dcc.xml"), "grbg")

    def test_cfa_bayer(self):
        sensors = descriptor.discover(ROOT / "sensors")
        for sid, name in (("imx230", "imx230_cfa_dcc.xml"), ("imx477", "imx477_cfa.xml"),
                          ("ov5647", "ov5647_cfa_dcc.xml")):
            with self.subTest(sid):
                s = sensors[sid]
                self.assertEqual(dcc.read_cfa_bayer(s.dcc_source / name), s.mode.bayer)
        self.assertIsNone(dcc.read_cfa_bayer(ROOT / "sensors/imx230/dcc/linear/imx230_viss_blc.xml"))

    def test_write_cfa_bayer(self):
        src = ROOT / "sensors/imx230/dcc/linear/imx230_cfa_dcc.xml"

        def kernels(path):
            _, fir = dcc._cfa_fir(path.read_text(), path)
            return [int(m.group(1)) for m in fir]

        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "cfa.xml"
            shutil.copyfile(src, xml)
            rggb = kernels(xml)
            for order in ("bggr", "gbrg", "grbg"):
                with self.subTest(order):
                    self.assertTrue(dcc.write_cfa_bayer(xml, order))
                    self.assertEqual(dcc.read_cfa_bayer(xml), order)
                    self.assertFalse(dcc.write_cfa_bayer(xml, order))
                    # The red pixel's kernel moves to the red phase of the new order
                    moved, red = kernels(xml), dcc.BAYER_RED_PHASE[order]
                    self.assertEqual(moved[2 * 144 + red * 36:2 * 144 + red * 36 + 36], rggb[2 * 144:2 * 144 + 36])
                    self.assertTrue(dcc.write_cfa_bayer(xml, "rggb"))
                    self.assertEqual(xml.read_text(), src.read_text())

    def test_cfa_unknown_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            xml = Path(tmp) / "cfa.xml"
            xml.write_text('<cfg_flxd_cfa_dcc type="cfg_flxd_cfa_vars">{' + "1, " * 1784 + "}</cfg_flxd_cfa_dcc>")
            with self.assertRaisesRegex(dcc.DccError, "cannot tell"):
                dcc.read_cfa_bayer(xml)
            xml.write_text('<cfg_flxd_cfa_dcc type="cfg_flxd_cfa_vars">{1, 2}</cfg_flxd_cfa_dcc>')
            with self.assertRaisesRegex(dcc.DccError, "expected at least"):
                dcc.read_cfa_bayer(xml)

    def test_module_id(self):
        text = "<camera_module_id>     390 </camera_module_id>"
        self.assertEqual(dcc.set_module_id(text, 477), "<camera_module_id>     477 </camera_module_id>")

    def test_inspect(self):
        def component(module_id, descriptor_id, payload):
            size = 80 + len(payload)
            words = [module_id, descriptor_id] + [0] * 17 + [size]
            return struct.pack("<20I", *words) + payload

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dcc.bin"
            path.write_bytes(component(477, 1, b"x" * 12) + component(477, 9, b""))
            parts = dcc.inspect(path)
            self.assertEqual([(c.module_id, c.descriptor, c.size) for c in parts],
                             [(477, "VISS_BLC", 92), (477, "VISS_CFA", 80)])
            path.write_bytes(component(477, 1, b"")[:-4])
            with self.assertRaises(dcc.DccError):
                dcc.inspect(path)


class FetchToolsTest(unittest.TestCase):
    TOOL = b"\x7fELF stand-in for dcc_gen_linux"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cache = self.tmp / "cache"
        for patch in (mock.patch.dict(os.environ, {"CAMERA_ISP_SDK_DCC_TOOLS": str(self.cache)}),
                      mock.patch.object(dcc, "TI_TOOL_SHA256", hashlib.sha256(self.TOOL).hexdigest()),
                      mock.patch.object(dcc, "DOWNLOAD_RETRY_DELAYS", (0, 0))):
            patch.start()
            self.addCleanup(patch.stop)

    def urlopen(self, *responses):
        patch = mock.patch("urllib.request.urlopen", side_effect=list(responses))
        self.addCleanup(patch.stop)
        return patch.start()

    @staticmethod
    def http_error(code):
        return urllib.error.HTTPError(dcc.TI_TOOL_URL, code, "error", {}, None)

    def fetch(self, source=None):
        return dcc.fetch_tools(source, log=lambda *_: None)

    def assert_installed(self):
        tool = dcc.require_tools()
        self.assertEqual(tool.read_bytes(), self.TOOL)
        self.assertTrue(os.access(tool, os.X_OK))
        self.assertEqual([p.name for p in self.cache.iterdir()], [dcc.TI_TOOL])

    def test_from_checkout(self):
        for layout in ("tools/dcc_tools", "imaging/tools/dcc_tools", ""):
            with self.subTest(layout=layout):
                shutil.rmtree(self.cache, ignore_errors=True)
                src = self.tmp / "src" / layout / dcc.TI_TOOL
                src.parent.mkdir(parents=True, exist_ok=True)
                src.write_bytes(self.TOOL)
                self.fetch(self.tmp / "src")
                self.assert_installed()
                shutil.rmtree(self.tmp / "src")

    def test_from_wrong_file(self):
        src = self.tmp / dcc.TI_TOOL
        src.write_bytes(b"something else")
        with self.assertRaisesRegex(dcc.DccError, "checksum mismatch"):
            self.fetch(src)
        self.assertEqual(list(self.cache.iterdir()), [])
        with self.assertRaisesRegex(dcc.DccError, "no dcc_gen_linux"):
            self.fetch(self.tmp / "empty")

    def test_download_retries_server_errors(self):
        urlopen = self.urlopen(self.http_error(503), OSError("timed out"), io.BytesIO(self.TOOL))
        self.fetch()
        self.assertEqual(urlopen.call_count, 3)
        self.assert_installed()
        self.fetch()  # verified copy is kept
        self.assertEqual(urlopen.call_count, 3)

    def test_download_gives_up(self):
        urlopen = self.urlopen(*[self.http_error(503)] * 3)
        with self.assertRaisesRegex(dcc.DccError, "fetch-tools --from"):
            self.fetch()
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual(list(self.cache.iterdir()), [])

        urlopen = self.urlopen(self.http_error(404))
        with self.assertRaises(dcc.DccError):
            self.fetch()
        self.assertEqual(urlopen.call_count, 1)


TOPOLOGY = textwrap.dedent('''\
    Media controller API version 6.12.24

    Device topology
    - entity 1: 30102000.ticsi2rx (7 pads, 7 links, 1 route)
                type V4L2 subdev subtype Unknown flags 0
                device node name /dev/v4l-subdev0
    \troutes:
    \t\t0/0 -> 1/0 [ACTIVE]
    \tpad0: Sink
    \t\t[stream:0 fmt:SRGGB12_1X12/1920x1080 field:none]
    \t\t<- "cdns_csi2rx.30101000.csi-bridge":1 [ENABLED,IMMUTABLE]
    \tpad1: Source
    \t\t-> "30102000.ticsi2rx context 0":0 [ENABLED,IMMUTABLE]

    - entity 9: cdns_csi2rx.30101000.csi-bridge (5 pads, 2 links, 1 route)
                type V4L2 subdev subtype Unknown flags 0
                device node name /dev/v4l-subdev1
    \tpad0: Sink
    \t\t<- "imx477 2-001a":0 [ENABLED,IMMUTABLE]
    \tpad1: Source
    \t\t-> "30102000.ticsi2rx":0 [ENABLED,IMMUTABLE]

    - entity 15: imx477 2-001a (1 pad, 1 link, 0 routes)
                 type V4L2 subdev subtype Sensor flags 0
                 device node name /dev/v4l-subdev2
    \tpad0: Source
    \t\t[stream:0 fmt:SRGGB12_1X12/4056x3040 field:none colorspace:raw
    \t\t crop:(8,16)/4056x3040]
    \t\t-> "cdns_csi2rx.30101000.csi-bridge":0 [ENABLED,IMMUTABLE]

    - entity 21: 30102000.ticsi2rx context 0 (1 pad, 1 link)
                 type Node subtype V4L flags 0
                 device node name /dev/video2
    \tpad0: Sink
    \t\t<- "30102000.ticsi2rx":1 [ENABLED,IMMUTABLE]
    ''')


class MediaTest(unittest.TestCase):
    def test_parse(self):
        entities = media.parse_topology(TOPOLOGY)
        sensor = entities["imx477 2-001a"]
        self.assertEqual(sensor.devnode, "/dev/v4l-subdev2")
        self.assertEqual(sensor.pads[0].fmt, ("SRGGB12_1X12", 4056, 3040))
        self.assertEqual(sensor.remote(0).entity, "cdns_csi2rx.30101000.csi-bridge")
        self.assertEqual(entities["30102000.ticsi2rx context 0"].devnode, "/dev/video2")

    def test_parse_uppercase_pads(self):
        # v4l-utils 1.28 media-ctl spells the pad direction in upper case.
        upper = TOPOLOGY.replace("pad0: Sink", "pad0: SINK").replace(
            "pad0: Source", "pad0: SOURCE").replace("pad1: Source", "pad1: SOURCE")
        self.assertNotIn(": Source", upper)
        entities = media.parse_topology(upper)
        self.assertEqual(entities["imx477 2-001a"].pads[0].kind, "Source")
        self.assertEqual(entities["imx477 2-001a"].remote(0).entity,
                         "cdns_csi2rx.30101000.csi-bridge")

    def test_find_cameras(self):
        sensors = descriptor.discover(ROOT / "sensors")
        original = media._media_ctl
        media._media_ctl = lambda device, *args: TOPOLOGY
        try:
            cams = media.find_cameras(sensors, ["/dev/media0"])
        finally:
            media._media_ctl = original
        self.assertEqual(len(cams), 1)
        cam = cams[0]
        self.assertEqual((cam.sensor.id, cam.bridge, cam.receiver, cam.video),
                         ("imx477", "cdns_csi2rx.30101000.csi-bridge", "30102000.ticsi2rx", "/dev/video2"))
        self.assertEqual(cam.video_link, "/dev/video-imx477-cam0")


    def camera(self):
        s = descriptor.discover(ROOT / "sensors")["imx477"]
        return media.Camera(s, 0, "/dev/media0", "imx477 2-001a", "/dev/v4l-subdev2",
                            "cdns_csi2rx.30101000.csi-bridge", "30102000.ticsi2rx", "/dev/video2")

    def test_configure(self):
        commands = []

        def run(tool, device, *args):
            commands.append((tool, *args))
            if args[0] == "--get-v4l2":
                return '\t\t[fmt:SRGGB12_1X12/1920x1080 field:none]'
            if args[0].startswith("--get-ctrl"):
                return "pixel_rate: 840000000\nhorizontal_blanking: 9332\nvertical_blanking: 413\n"
            return ""

        cam = self.camera()
        with mock.patch.object(media, "_run", run), mock.patch.object(media.os, "symlink") as symlink, \
                mock.patch.object(media.os, "replace"):
            media.configure(cam, log=lambda _: None)
        self.assertIn(("v4l2-ctl", "--set-ctrl=vertical_blanking=413"), commands)
        self.assertAlmostEqual(cam.frame_rate, 50, places=1)
        symlink.assert_any_call("/dev/video2", "/dev/video-imx477-cam0.tmp")

    def test_configure_wrong_format_leaves_links(self):
        def run(tool, device, *args):
            return "[fmt:SRGGB12_1X12/4056x3040 field:none]" if args[0] == "--get-v4l2" else ""

        with mock.patch.object(media, "_run", run), mock.patch.object(media.os, "symlink") as symlink:
            with self.assertRaisesRegex(media.MediaError, "driver chose"):
                media.configure(self.camera(), log=lambda _: None)
        symlink.assert_not_called()

    def test_configure_link_error(self):
        def run(tool, device, *args):
            return "[fmt:SRGGB12_1X12/1920x1080 field:none]" if args[0] == "--get-v4l2" else ""

        with mock.patch.object(media, "_run", run), \
                mock.patch.object(media.os, "symlink", side_effect=PermissionError(13, "Permission denied")):
            with self.assertRaisesRegex(media.MediaError, "Permission denied"):
                media.configure(self.camera(), log=lambda _: None)

    def test_frame_rate_without_controls(self):
        with mock.patch.object(media, "_run", side_effect=media.MediaError("unknown control")):
            self.assertIsNone(media.frame_rate("/dev/v4l-subdev2", 1920, 1080))


class PipelineTest(unittest.TestCase):
    def test_display(self):
        s = descriptor.discover(ROOT / "sensors")["imx477"]
        cmd = " ".join(pipeline.command(s, "/dev/video-imx477-cam0", "/dev/v4l-imx477-subdev0",
                                        pipeline.Options()))
        self.assertIn("video/x-bayer,width=1920,height=1080,format=rggb12,framerate=50/1", cmd)
        self.assertIn("tiovxisp sensor-name=imx477 dcc-isp-file=/opt/imaging/imx477/linear/dcc_viss.bin "
                      "format-msb=11", cmd)
        self.assertIn("sink_0::device=/dev/v4l-imx477-subdev0", cmd)
        self.assertTrue(cmd.endswith("kmssink driver-name=tidss sync=false"))

    def test_ldc_requires_tuning(self):
        s = descriptor.discover(ROOT / "sensors")["imx477"]
        with self.assertRaises(ValueError):
            pipeline.command(s, "v", "s", pipeline.Options(ldc=True))


VERMAGIC = b"\0vermagic=6.12.24-ti SMP preempt_rt mod_unload aarch64\0"


class InstallTest(unittest.TestCase):
    def test_module_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            ko = Path(tmp) / "cam.ko"
            ko.write_bytes(b"\x7fELF\0license=GPL" + VERMAGIC)
            self.assertEqual(install.module_release(ko), "6.12.24-ti")
            ko.write_bytes(b"\x7fELF\0license=GPL\0")
            with self.assertRaises(ValueError):
                install.module_release(ko)

    def test_stage(self):
        s = descriptor.discover(ROOT / "sensors")["imx219"]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for port in s.ports:
                dtbo = tmp / "build/overlays" / f"{BOARD.overlay_name(port, s.id)}.dtbo"
                dtbo.parent.mkdir(parents=True, exist_ok=True)
                dtbo.write_bytes(b"")
            (tmp / "driver").mkdir()
            (tmp / "driver/imx219.ko").write_bytes(VERMAGIC)
            sensors = {s.id: replace(s, driver_dir=tmp / "driver")}
            install.stage(ROOT, BOARD, sensors, tmp / "build", tmp / "dest", log=lambda *a: None)

            share = tmp / "dest/usr/share/camera-isp-sdk"
            self.assertEqual(
                sorted(str(p.relative_to(share)) for p in share.rglob("*") if p.is_file()),
                ["boards/t3-gem-o1/board.conf", "gst/build.sh",
                 *(f"gst/patches/{p.name}" for p in sorted((ROOT / "gst/patches").glob("*.patch"))),
                 "gst/src/camera_isp_sdk_sensor.c", "gst/src/camera_isp_sdk_sensor.h", "sensors/imx219/sensor.conf"])
            self.assertTrue((tmp / "dest/lib/modules/6.12.24-ti/updates/imx219.ko").is_file())

    def test_package_mtime(self):
        # A constant mtime would let Python keep the previous version's .pyc
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "stage/usr/lib").mkdir(parents=True)
            py = tmp / "stage/usr/lib/a.py"
            py.write_text("x = 1\n")
            os.utime(py, (1_800_000_000, 1_800_000_000))
            for epoch, expected in ((None, 1_800_000_000), ("1700000000", 1_700_000_000), ("1900000000", 1_800_000_000)):
                env = {} if epoch is None else {"SOURCE_DATE_EPOCH": epoch}
                with self.subTest(epoch=epoch), mock.patch.dict(os.environ, env):
                    if epoch is None:
                        os.environ.pop("SOURCE_DATE_EPOCH", None)
                    install.package(tmp / "stage", tmp / "p.tar.gz")
                    with tarfile.open(tmp / "p.tar.gz") as tar:
                        self.assertEqual([m.mtime for m in tar.getmembers()], [expected])


class CliTest(unittest.TestCase):
    def test_host_commands_refused_on_installed_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            # The installed data root: board.conf without the port headers
            (Path(tmp) / "boards/t3-gem-o1").mkdir(parents=True)
            shutil.copy(ROOT / "boards/t3-gem-o1/board.conf", Path(tmp) / "boards/t3-gem-o1")
            (Path(tmp) / "sensors").mkdir()
            err = io.StringIO()
            with mock.patch.object(cli, "INSTALLED_ROOT", Path(tmp)), mock.patch("sys.stderr", err), \
                    mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(cli.main(["--root", tmp, "overlay", "build"]), 1)
                self.assertEqual(cli.main(["--root", tmp, "list"]), 0)
            self.assertIn("'overlay build' works on the camera-isp-sdk source tree", err.getvalue())

    def test_validate_only_named_broken_sensor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_root(tmp, ("imx219", "imx230"))
            conf = root / "sensors/imx230/sensor.conf"
            conf.write_text(conf.read_text().replace("fps=30", "fps=abc"))
            status, out, err = run_cli("--root", root, "validate", "imx230")
        self.assertEqual(status, 1)
        self.assertIn("imx230: FAIL", out)
        self.assertNotIn("imx219", out + err)

    def test_new_without_dcc_bits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_root(tmp)
            conf = root / "sensors/imx230/sensor.conf"
            conf.write_text("\n".join(line for line in conf.read_text().splitlines() if not line.startswith("bits=")))
            status, out, _ = run_cli("--root", root, "new", "cam", "--width", 1920, "--height", 1080,
                                     "--code", "SRGGB10_1X10", "--dcc-id", 4321)
            self.assertEqual(status, 0, out)
            self.assertNotIn("None-bit", out)
            self.assertIsNone(descriptor.load(root / "sensors/cam").dcc_bits)

    def test_pipeline_run_keeps_overrides(self):
        for env, expected in (({}, f"{descriptor.SENSOR_OVERRIDE_DIR}:{ROOT / 'sensors'}"),
                              ({descriptor.SENSOR_PATH_ENV: "/x"}, "/x")):
            with self.subTest(env=env), mock.patch.dict(os.environ, env), \
                    mock.patch.object(cli.subprocess, "call", return_value=0) as call:
                if not env:
                    os.environ.pop(descriptor.SENSOR_PATH_ENV, None)
                self.assertEqual(run_cli("--root", ROOT, "pipeline", "imx477", "--run")[0], 0)
                self.assertEqual(call.call_args.kwargs["env"][descriptor.SENSOR_PATH_ENV], expected)

    def test_driver_build(self):
        out = io.StringIO()
        with mock.patch.object(cli.subprocess, "call", return_value=0) as call, mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["--root", str(ROOT), "driver", "build", "--kdir", "/k", "imx477", "ov5647"]), 0)
        call.assert_called_once_with(["make", "-C", "/k", f"M={ROOT / 'sensors/imx477/driver'}", "ARCH=arm64",
                                      "CROSS_COMPILE=aarch64-linux-gnu-", "modules"])
        self.assertIn("ov5647: driver is in the kernel", out.getvalue())

    def test_plugin_source_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = Path(tmp) / "libgsttiovx.so"
            inspect = f"Plugin Details:\n  Name                     tiovx\n  Filename                 {plugin}\n"
            self.assertIsNone(cli._plugin_source_hash(inspect))
            Path(f"{plugin}.camera-isp-sdk-sha256").write_text("abc\n")
            self.assertEqual(cli._plugin_source_hash(inspect), "abc")

    @unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "bash and sha256sum are required")
    def test_gst_source_hash_matches_build_sh(self):
        script = 'source <(sed -n "/^source_hash()/,/^}/p" "$1"); source_hash'
        res = subprocess.run(["bash", "-c", script, "bash", str(ROOT / "gst/build.sh")],
                             env={**os.environ, "HERE": str(ROOT / "gst")},
                             capture_output=True, text=True, check=True)
        self.assertEqual(res.stdout.strip(), cli.gst_source_hash(ROOT / "gst"))

    @unittest.skipUnless(shutil.which("bash"), "bash is required")
    def test_build_sh_reports_missing_plugin(self):
        # With errexit and pipefail a failing gst-inspect-1.0 used to end the
        # script silently in plugin="$(plugin_file)".
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "gst-inspect-1.0"
            fake.write_text("#!/bin/sh\nexit 1\n")
            fake.chmod(0o755)
            res = subprocess.run(["bash", str(ROOT / "gst/build.sh"), "--restore"], capture_output=True, text=True,
                                 env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
        self.assertEqual(res.returncode, 1)
        self.assertIn("tiovxisp is not installed", res.stderr)


@unittest.skipUnless(shutil.which("cpp") and shutil.which("dtc"), "cpp and dtc are required")
class OverlayTest(unittest.TestCase):
    def test_build_all(self):
        sensors = descriptor.discover(ROOT / "sensors")
        with tempfile.TemporaryDirectory() as tmp:
            for s in sensors.values():
                for port in s.ports:
                    dtbo = overlay.build(ROOT, BOARD, s, port, tmp)
                    self.assertGreater(dtbo.stat().st_size, 0)

    def test_csi_from_descriptor(self):
        s = descriptor.discover(ROOT / "sensors")["imx230"]
        self.assertEqual(overlay.csi_defines(s), ["#define CAM_DATA_LANES\t1 2", "#define CAM_LINK_FREQ\t493500000",
                                                  "#define CAM_CLOCK_NONCONTINUOUS"])
        with tempfile.TemporaryDirectory() as tmp:
            dts = Path(tmp) / "imx230.dts"
            for lanes, freq in ((2, 493500000), (1, 300000000)):
                with self.subTest(lanes=lanes):
                    dtbo = overlay.build(ROOT, BOARD, replace(s, lanes=lanes, link_frequency=freq), "csi0", tmp)
                    subprocess.run(["dtc", "-q", "-I", "dtb", "-O", "dts", "-o", str(dts), str(dtbo)], check=True)
                    text = dts.read_text()
                    self.assertIn(f"link-frequencies = <0x00 {freq:#x}>", text)
                    self.assertIn("data-lanes = <0x01 0x02>" if lanes == 2 else "data-lanes = <0x01>", text)

    def test_fragment_must_not_define_csi(self):
        s = descriptor.discover(ROOT / "sensors")["imx219"]
        with tempfile.TemporaryDirectory() as tmp:
            fragment = Path(tmp) / "overlay.dtsi"
            fragment.write_text("#define CAM_DATA_LANES\t1\n" + s.overlay.read_text())
            s = replace(s, overlay=fragment)
            self.assertTrue(any("defines CAM_DATA_LANES" in e for e in checks.check(s, BOARD)[0]))
            with self.assertRaisesRegex(overlay.OverlayError, "CAM_DATA_LANES"):
                overlay.build(ROOT, BOARD, s, "csi0", tmp)

    def test_new_ids_make_valid_labels(self):
        self.assertEqual([overlay.dt_label(i) for i in ("imx477", "my-cam", "5640")], ["imx477", "my_cam", "cam_5640"])
        with tempfile.TemporaryDirectory() as tmp:
            root = copy_root(tmp)
            for i, sid in enumerate(("my-cam", "5640")):
                status, out, err = run_cli("--root", root, "new", sid, "--width", 1920, "--height", 1080,
                                           "--code", "SRGGB10_1X10", "--dcc-id", 4000 + i)
                self.assertEqual(status, 0, out + err)
                status, out, err = run_cli("--root", root, "--build-dir", Path(tmp) / "build", "overlay", "build", sid)
                self.assertEqual(status, 0, out + err)


if __name__ == "__main__":
    unittest.main()
