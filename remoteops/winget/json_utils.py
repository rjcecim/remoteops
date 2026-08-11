"""Parsing resiliente de JSON vindo do host remoto."""

from __future__ import annotations

import json
import re

_JSON_BAD_BACKSLASH_RE = re.compile(r'\\(?!["\\/bfnrtu])')


def _try_load(raw: str) -> dict:
    txt = (raw or "").strip()
    if not txt:
        raise ValueError("empty json")
    if not txt.startswith("{"):
        i = txt.find("{")
        if i >= 0:
            txt = txt[i:]
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(txt)
    if not isinstance(obj, dict):
        raise ValueError("json root is not an object")
    return obj


def loads_json_best_effort(s: str) -> dict:
    """Parse resiliente: tolera ruído antes/depois do JSON e corrige
    barras invertidas inválidas (paths) que quebram ``json.loads``.
    """
    try:
        return _try_load(s)
    except Exception:
        fixed = _JSON_BAD_BACKSLASH_RE.sub(r"\\\\", s or "")
        return _try_load(fixed)
