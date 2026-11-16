# SPDX-License-Identifier: Apache-2.0
# Copyright (C) 2026 T3 Foundation
"""Key-file helpers.

Descriptors are read both here and by GLib's GKeyFile in the GStreamer
plugin (gst/src/camera_isp_sdk_sensor.c), so only the common subset is accepted:
whole-line '#' comments, key=value pairs, ';'-separated lists. Numbers
follow the C library the plugin uses: integers as strtoll() with base 0
(decimal, 0x hexadecimal, leading-0 octal), finite numbers as strtod().
"""

import configparser
import math
import re
from pathlib import Path

# g_ascii_strtoll(base 0) syntax: no '_', '0b' or '0o' as Python's int(x, 0) has
_INT_RE = re.compile(r"[+-]?(?:0[xX][0-9a-fA-F]+|0[0-7]*|[1-9][0-9]*)")
# g_ascii_strtod() syntax, decimal or hexadecimal; no '_', 'inf' or 'nan'
_FLOAT_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_HEX_FLOAT_RE = re.compile(r"[+-]?0[xX](?:[0-9a-fA-F]+\.?[0-9a-fA-F]*|\.[0-9a-fA-F]+)(?:[pP][+-]?\d+)?")


def parse_int(text):
    """Parses an integer as the plugin's g_ascii_strtoll(text, &end, 0) does.

    Raises ValueError for anything it would not consume entirely.
    """
    if not _INT_RE.fullmatch(text):
        raise ValueError(text)
    sign = -1 if text[0] == "-" else 1
    digits = text.lstrip("+-")
    if digits[:2].lower() == "0x":
        return sign * int(digits[2:], 16)
    if len(digits) > 1 and digits[0] == "0":
        return sign * int(digits, 8)
    return sign * int(digits)


def parse_float(text):
    """Parses a finite number as the plugin's g_ascii_strtod() + isfinite() do."""
    if _FLOAT_RE.fullmatch(text):
        value = float(text)
    elif _HEX_FLOAT_RE.fullmatch(text):
        value = float.fromhex(text)
    else:
        raise ValueError(text)
    if not math.isfinite(value):
        raise ValueError(text)
    return value


class ConfError(Exception):
    pass


class Conf:
    def __init__(self, path):
        self.path = Path(path)
        self._cp = configparser.ConfigParser(
            interpolation=None,
            comment_prefixes=("#",),
            inline_comment_prefixes=None,
            delimiters=("=",),
            strict=True,
            empty_lines_in_values=False,
        )
        self._cp.optionxform = str
        try:
            with open(self.path, encoding="utf-8") as f:
                self._cp.read_file(f)
        except (OSError, configparser.Error) as e:
            raise ConfError(f"{self.path}: {e}") from None

    def _where(self, section, key):
        return f"{self.path}: [{section}] {key}"

    def sections(self):
        return self._cp.sections()

    def has(self, section, key=None):
        if key is None:
            return self._cp.has_section(section)
        return self._cp.has_option(section, key)

    def keys(self, section):
        return list(self._cp[section].keys()) if self.has(section) else []

    def str(self, section, key, default=None, required=False):
        if not self.has(section, key):
            if required:
                raise ConfError(f"{self._where(section, key)}: required")
            return default
        return self._cp.get(section, key).strip()

    def int(self, section, key, default=None, required=False, lo=None, hi=None):
        raw = self.str(section, key, required=required)
        if raw is None:
            return default
        return self._check_int(section, key, raw, lo, hi)

    def _check_int(self, section, key, raw, lo=None, hi=None):
        try:
            value = parse_int(raw)
        except ValueError:
            raise ConfError(f"{self._where(section, key)}: expected an integer, got '{raw}'") from None
        if lo is not None and value < lo:
            raise ConfError(f"{self._where(section, key)}: {value} is below the minimum {lo}")
        if hi is not None and value > hi:
            raise ConfError(f"{self._where(section, key)}: {value} is above the maximum {hi}")
        return value

    def float(self, section, key, default=None, required=False):
        raw = self.str(section, key, required=required)
        if raw is None:
            return default
        try:
            return parse_float(raw)
        except ValueError:
            raise ConfError(f"{self._where(section, key)}: expected a number, got '{raw}'") from None

    def bool(self, section, key, default=False):
        raw = self.str(section, key)
        if raw is None:
            return default
        if raw.lower() in ("true", "1"):
            return True
        if raw.lower() in ("false", "0"):
            return False
        raise ConfError(f"{self._where(section, key)}: expected true or false, got '{raw}'")

    def list(self, section, key, default=None, required=False):
        raw = self.str(section, key, required=required)
        if raw is None:
            return [] if default is None else default
        items = [item.strip() for item in raw.split(";")]
        if items and items[-1] == "":
            items.pop()
        return items

    def int_list(self, section, key, required=False, lo=None, hi=None):
        return [self._check_int(section, key, item, lo, hi)
                for item in self.list(section, key, required=required)]
