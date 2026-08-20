"""Identidade do instalador EXE e correspondência com aplicativos instalados.

Metadados PE (ProductName / FileDescription) + tokens do arquivo; comparação
numérica de versão (4.10.0 > 4.9.0). Não exige nome exato do instalador.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from remoteops.utils.app_catalog import find_catalog_entry
from remoteops.utils.psinfo import InstalledApp

# Tokens genéricos demais para identificar o produto.
# Tokens com menos de 3 letras já são ignorados em tokenize().
_STOPWORDS = frozenset(
    {
        # Arquitetura / SO
        "amd64",
        "arm64",
        "i386",
        "i686",
        "ia32",
        "win32",
        "win64",
        "windows",
        "wow64",
        "x32",
        "x64",
        "x86",
        # Tipo de pacote / instalador
        "bin",
        "bundle",
        "exe",
        "hotfix",
        "install",
        "installed",
        "installer",
        "msi",
        "offline",
        "online",
        "package",
        "patch",
        "redist",
        "redistributable",
        "setup",
        "silent",
        "update",
        "updater",
        "ver",
        "version",
        # Edição / marketing
        "edition",
        "full",
        "lite",
        "portable",
        # Sobram em ProductName (ex.: Java Platform SE Runtime Environment)
        "application",
        "environment",
        "platform",
        "program",
        "runtime",
        "sdk",
        "software",
        # Locale (3+ letras; en/us/br/pt já caem no filtro de tamanho)
        "enu",
        "ptb",
        "ptbr",
        # Inglês genérico
        "and",
        "for",
        "the",
        "win",
        "with",
    }
)

_MARKS_RE = re.compile(r"[®™]|(\(tm\))|(\(r\))", re.IGNORECASE)
_VERSION_IN_NAME_RE = re.compile(
    r"\b(?:v(?:er(?:sion)?)?\s*)?\d+(?:\.\d+){1,5}[a-z]?\b|\b\d+u\d+\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+]*")
_VERSION_TOKEN_RE = re.compile(r"^\d+[a-z]\d+$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")


@dataclass(frozen=True)
class ExeMetadata:
    path: str
    product_name: str = ""
    file_description: str = ""
    product_version: str = ""
    file_version: str = ""
    file_stem: str = ""

    @property
    def installer_version(self) -> str:
        """ProductVersion, depois FileVersion; só valores com componentes numéricos."""
        for candidate in (self.product_version, self.file_version):
            text = valid_numeric_version(candidate)
            if text:
                return text
        return ""


@dataclass(frozen=True)
class ProductIdentity:
    """Needles ordenados (mais específico primeiro) para achar o app instalado."""

    label: str
    needles: Tuple[str, ...] = field(default_factory=tuple)
    filename_needles: Tuple[str, ...] = field(default_factory=tuple)
    installer_version: str = ""


def read_exe_metadata(path: str) -> ExeMetadata:
    """Lê ProductName / FileDescription / versões do recurso de versão do PE."""
    p = (path or "").strip()
    stem = os.path.splitext(os.path.basename(p))[0] if p else ""
    info = _query_version_strings(p) if p else {}
    return ExeMetadata(
        path=p,
        product_name=info.get("ProductName", ""),
        file_description=info.get("FileDescription", ""),
        product_version=info.get("ProductVersion", ""),
        file_version=info.get("FileVersion", ""),
        file_stem=stem,
    )


def identify_product(path: str, metadata: Optional[ExeMetadata] = None) -> ProductIdentity:
    """Monta needles: ProductName, FileDescription, depois o nome do arquivo."""
    meta = metadata if metadata is not None else read_exe_metadata(path)
    ordered: List[str] = []
    filename_ordered: List[str] = []
    seen: set[str] = set()

    def add(value: str, *, filename: bool = False) -> None:
        text = " ".join((value or "").split()).strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        if filename:
            filename_ordered.append(text)
        else:
            ordered.append(text)

    def add_from_source(source: str, *, filename: bool = False) -> None:
        if not (source or "").strip():
            return
        entry = find_catalog_entry(source)
        if entry:
            add(str(entry.get("displayName") or ""), filename=filename)
        add(source, filename=filename)
        add(family_label(source), filename=filename)
        tokens = tokenize(source)
        for token in sorted(tokens, key=lambda t: (-len(t), t.casefold())):
            add(token, filename=True)

    add_from_source(meta.product_name)
    add_from_source(meta.file_description)
    add_from_source(meta.file_stem, filename=True)

    if not ordered and not filename_ordered and meta.file_stem:
        add(meta.file_stem, filename=True)

    label = (
        ordered[0]
        if ordered
        else (filename_ordered[0] if filename_ordered else "")
    )
    if not label:
        label = meta.file_stem or os.path.basename(meta.path) or ""
    return ProductIdentity(
        label=label,
        needles=tuple(ordered),
        filename_needles=tuple(filename_ordered),
        installer_version=meta.installer_version,
    )


def family_label(text: str) -> str:
    """Remove marcas, versões e stopwords; resta o nome de família (ex.: Java, WinRAR)."""
    cleaned = _MARKS_RE.sub(" ", text or "")
    cleaned = _VERSION_IN_NAME_RE.sub(" ", cleaned)
    parts = tokenize(cleaned)
    return " ".join(parts)


def tokenize(text: str) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for raw in _TOKEN_RE.findall(text or ""):
        key = raw.casefold()
        if key in _STOPWORDS:
            continue
        if key.isdigit():
            continue
        if _VERSION_TOKEN_RE.fullmatch(key):
            continue
        if re.fullmatch(r"[a-z]\d+", key):
            continue
        if len(key) < 3:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(raw)
    return out


def parse_version_key(value: str) -> Optional[Tuple[int, ...]]:
    """Extrai componentes numéricos; None se não houver dígitos."""
    parts = _DIGITS_RE.findall(value or "")
    if not parts:
        return None
    try:
        return tuple(int(p) for p in parts)
    except (TypeError, ValueError):
        return None


def valid_numeric_version(value: str) -> str:
    """Devolve o texto se houver versão numérica comparável; senão vazio."""
    text = (value or "").strip()
    if text and parse_version_key(text) is not None:
        return text
    return ""


def compare_versions(left: str, right: str) -> Optional[int]:
    """
    Comparação numérica com padding de zeros: 4.10.0 > 4.9.0.

    Retorna -1, 0, 1 ou None se algum lado não tiver versão válida.
    """
    a = parse_version_key(left)
    b = parse_version_key(right)
    if a is None or b is None:
        return None
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def highest_version_app(apps: Sequence[InstalledApp]) -> Optional[InstalledApp]:
    """Entre correspondências do mesmo produto, a maior versão válida."""
    if not apps:
        return None
    ranked: List[Tuple[Tuple[int, ...], InstalledApp]] = []
    fallback: List[InstalledApp] = []
    for app in apps:
        key = parse_version_key(app.version or "")
        if key is None:
            fallback.append(app)
            continue
        ranked.append((key, app))
    if not ranked:
        return fallback[0] if fallback else None
    ranked.sort(key=lambda item: _padded_key(item[0]), reverse=True)
    return ranked[0][1]


def match_installed_app(
    apps: Sequence[InstalledApp],
    needles: Sequence[str],
    *,
    filename_needles: Sequence[str] = (),
) -> Optional[InstalledApp]:
    """Primeiro needle de metadados que casar; arquivo só como fallback.

    Metadados usam substring. Tokens do nome do arquivo exigem fronteira de
    palavra, para reduzir falso positivo em outro software.
    """
    for needle in needles:
        hits = _hits_for_needle(apps, needle, strict=False)
        if hits:
            return highest_version_app(hits)
    for needle in filename_needles:
        hits = _hits_for_needle(apps, needle, strict=True)
        if hits:
            return highest_version_app(hits)
    return None


def match_identity_app(
    apps: Sequence[InstalledApp],
    identity: ProductIdentity,
) -> Optional[InstalledApp]:
    return match_installed_app(
        apps,
        identity.needles,
        filename_needles=identity.filename_needles,
    )


def _hits_for_needle(
    apps: Sequence[InstalledApp],
    needle: str,
    *,
    strict: bool,
) -> List[InstalledApp]:
    n = (needle or "").strip()
    if len(n) < 2:
        return []
    return [app for app in apps if _display_name_matches(app.display_name, n, strict=strict)]


def _display_name_matches(display_name: str, needle: str, *, strict: bool) -> bool:
    name = (display_name or "").strip()
    if not name or not needle:
        return False
    if not strict:
        return needle.casefold() in name.casefold()
    pattern = r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])"
    return re.search(pattern, name, re.IGNORECASE) is not None


def format_app_found(app: Optional[InstalledApp]) -> str:
    if app is None:
        return "—"
    name = (app.display_name or "").strip() or "—"
    arch = (app.arch or "").strip()
    if arch in ("64", "32") and f"({arch}-bit)" not in name.casefold():
        return f"{name} ({arch}-bit)"
    return name


def format_app_version(app: Optional[InstalledApp]) -> str:
    if app is None:
        return "—"
    return (app.version or "").strip() or "—"


def _padded_key(key: Tuple[int, ...], width: int = 8) -> Tuple[int, ...]:
    if len(key) >= width:
        return key
    return key + (0,) * (width - len(key))


def _query_version_strings(path: str) -> dict:
    if sys.platform != "win32" or not os.path.isfile(path):
        return {}
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, OSError, AttributeError):
        return {}

    version = ctypes.WinDLL("version", use_last_error=True)
    get_size = version.GetFileVersionInfoSizeW
    get_size.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    get_size.restype = wintypes.DWORD
    get_info = version.GetFileVersionInfoW
    get_info.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    get_info.restype = wintypes.BOOL
    query = version.VerQueryValueW
    query.argtypes = [
        wintypes.LPCVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    query.restype = wintypes.BOOL

    dummy = wintypes.DWORD(0)
    size = int(get_size(path, ctypes.byref(dummy)) or 0)
    if size <= 0:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not get_info(path, 0, size, buf):
        return {}

    translations: List[Tuple[int, int]] = []
    block = ctypes.c_void_p()
    length = wintypes.UINT(0)
    if query(buf, r"\VarFileInfo\Translation", ctypes.byref(block), ctypes.byref(length)):
        count = int(length.value) // 4
        words = ctypes.cast(block, ctypes.POINTER(wintypes.WORD))
        for i in range(count):
            translations.append((int(words[i * 2]), int(words[i * 2 + 1])))
    if not translations:
        translations = [
            (0x0409, 0x04B0),
            (0x0409, 0x04E4),
            (0x0416, 0x04B0),
            (0x0000, 0x04B0),
        ]

    keys = ("ProductName", "FileDescription", "ProductVersion", "FileVersion")
    out: dict = {}
    for lang, codepage in translations:
        prefix = f"\\StringFileInfo\\{lang:04X}{codepage:04X}\\"
        for key in keys:
            if out.get(key):
                continue
            ptr = ctypes.c_void_p()
            ulen = wintypes.UINT(0)
            if not query(buf, prefix + key, ctypes.byref(ptr), ctypes.byref(ulen)):
                continue
            if not ptr.value:
                continue
            text = ctypes.wstring_at(ptr.value).strip()
            if text:
                out[key] = text
        if all(out.get(k) for k in keys):
            break
    return out
