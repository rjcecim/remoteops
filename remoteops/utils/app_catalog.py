from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# InstalledApp importado lazy em funções que precisam — evita ciclo

_CATALOG_FILENAME = "ApplicationCatalog.json"
_CATALOG_SUBDIR = "config"
_cached_path: Optional[str] = None
_cached_mtime: Optional[float] = None
_cached_entries: List[Dict[str, Any]] = []
_cached_warnings: List[str] = []

# Campos conhecidos do schema. architecture / requiresElevation / requiresReboot
# são METADATA DOCUMENTADA — não alteram o comportamento de desinstalação atual.
# uninstallArgs é o único campo que influencia a geração do comando (EXE).
SCHEMA_FIELDS = (
    "displayName",
    "publisher",
    "publishers",
    "match",
    "uninstallArgs",
    "installerType",
    "requiresReboot",
    "requiresElevation",
    "architecture",
)

VALID_ARCHITECTURES = {"x86", "x64", "any", "arm64", ""}


@dataclass
class CatalogValidationResult:
    entries: List[Dict[str, Any]] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _app_dir() -> str:
    from remoteops.paths import project_root

    return str(project_root())


def catalog_candidates() -> List[str]:
    """Locais possíveis do ApplicationCatalog.json (prioridade: config/)."""
    from remoteops.paths import meipass_dir

    root = _app_dir()
    paths = [
        os.path.join(root, _CATALOG_SUBDIR, _CATALOG_FILENAME),
        os.path.join(root, _CATALOG_FILENAME),
    ]
    meipass = meipass_dir()
    if meipass is not None:
        paths.append(os.path.join(str(meipass), _CATALOG_SUBDIR, _CATALOG_FILENAME))
        paths.append(os.path.join(str(meipass), _CATALOG_FILENAME))
    out: List[str] = []
    seen: set[str] = set()
    for p in paths:
        key = os.path.normcase(os.path.normpath(p))
        if key in seen:
            continue
        seen.add(key)
        out.append(os.path.normpath(p))
    return out


def resolve_catalog_path() -> Optional[str]:
    for path in catalog_candidates():
        if os.path.isfile(path):
            return path
    return None


def validate_catalog_data(data: Any) -> CatalogValidationResult:
    """Valida o JSON do catálogo sem encerrar o app por entrada inválida."""
    result = CatalogValidationResult()
    if not isinstance(data, dict):
        result.errors.append("Raiz do JSON deve ser um objeto.")
        return result
    apps = data.get("applications")
    if apps is None:
        result.errors.append('Campo obrigatório ausente: "applications".')
        return result
    if not isinstance(apps, list):
        result.errors.append('"applications" deve ser uma lista.')
        return result

    seen_names: set[str] = set()
    for idx, item in enumerate(apps):
        prefix = f"applications[{idx}]"
        if not isinstance(item, dict):
            result.warnings.append(f"{prefix}: entrada ignorada (não é objeto).")
            continue

        display = str(item.get("displayName") or "").strip()
        match = item.get("match")
        if not display:
            result.warnings.append(f"{prefix}: displayName ausente — entrada ignorada.")
            continue
        if not isinstance(match, list) or not match:
            result.warnings.append(
                f"{prefix} ({display}): match ausente/inválido — entrada ignorada."
            )
            continue

        # Tipos
        if item.get("uninstallArgs") is not None and not isinstance(
            item.get("uninstallArgs"), str
        ):
            result.warnings.append(
                f"{prefix} ({display}): uninstallArgs deve ser string — ignorado."
            )
            item = dict(item)
            item["uninstallArgs"] = ""

        for bool_field in ("requiresReboot", "requiresElevation"):
            if bool_field in item and not isinstance(item[bool_field], bool):
                result.warnings.append(
                    f"{prefix} ({display}): {bool_field} deve ser boolean "
                    "(metadata; não afeta desinstalação)."
                )

        arch = item.get("architecture")
        if arch is not None and str(arch).strip().lower() not in VALID_ARCHITECTURES:
            result.warnings.append(
                f"{prefix} ({display}): architecture={arch!r} inválida "
                "(metadata; valores: x86, x64, any, arm64)."
            )

        name_key = display.casefold()
        if name_key in seen_names:
            result.warnings.append(
                f"{prefix} ({display}): displayName duplicado no catálogo."
            )
        seen_names.add(name_key)

        # Documenta campos metadata
        for meta in ("architecture", "requiresElevation", "requiresReboot"):
            if meta in item:
                # silencioso — já documentado no módulo
                pass

        result.entries.append(item)

    return result


def load_catalog(force: bool = False) -> List[Dict[str, Any]]:
    """Carrega a lista de aplicações do ApplicationCatalog.json (com cache por mtime)."""
    global _cached_path, _cached_mtime, _cached_entries, _cached_warnings

    path = resolve_catalog_path()
    if not path:
        _cached_path = None
        _cached_mtime = None
        _cached_entries = []
        _cached_warnings = ["ApplicationCatalog.json não encontrado."]
        return []

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []

    if (
        not force
        and _cached_path == path
        and _cached_mtime == mtime
        and _cached_entries is not None
    ):
        return _cached_entries

    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except OSError as exc:
        _cached_path = path
        _cached_mtime = mtime
        _cached_entries = []
        _cached_warnings = [f"Erro ao ler catálogo: {exc}"]
        return []
    except json.JSONDecodeError as exc:
        _cached_path = path
        _cached_mtime = mtime
        _cached_entries = []
        _cached_warnings = [f"JSON inválido no catálogo: {exc}"]
        return []

    validated = validate_catalog_data(data)
    _cached_path = path
    _cached_mtime = mtime
    _cached_entries = validated.entries
    _cached_warnings = list(validated.errors) + list(validated.warnings)
    return validated.entries


def catalog_warnings() -> List[str]:
    load_catalog()
    return list(_cached_warnings)


def entry_publishers(entry: Dict[str, Any]) -> List[str]:
    """Aceita ``publishers`` (lista) e/ou ``publisher`` (string) no JSON."""
    out: List[str] = []
    seen: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                _add(item)
            return
        text = str(value or "").strip()
        if not text:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(text)

    _add(entry.get("publishers"))
    _add(entry.get("publisher"))
    return out


def _normalize_name(name: str) -> str:
    return " ".join((name or "").casefold().split())


def _publisher_matches(entry: Dict[str, Any], publisher: str) -> bool:
    pub = _normalize_name(publisher)
    if not pub:
        return False
    for entry_pub in entry_publishers(entry):
        ep = _normalize_name(entry_pub)
        if ep in pub or pub in ep:
            return True
    return False


def _score_match(
    entry: Dict[str, Any],
    display_name: str,
    publisher: str = "",
    version: str = "",
    architecture: str = "",
) -> Tuple[int, int, int, int]:
    """
    Retorna chave de ordenação (maior = melhor):
    (exact_name, pattern_len, publisher_ok, arch_ok)
    """
    name = _normalize_name(display_name)
    if not name:
        return (-1, -1, -1, -1)

    patterns = entry.get("match") or []
    if not isinstance(patterns, list):
        return (-1, -1, -1, -1)

    best_pat_len = -1
    exact = 0
    for pat in patterns:
        p = _normalize_name(str(pat or ""))
        if not p:
            continue
        if p == name or name == _normalize_name(str(entry.get("displayName") or "")):
            exact = 1
            best_pat_len = max(best_pat_len, len(p))
        elif p in name:
            best_pat_len = max(best_pat_len, len(p))

    if best_pat_len < 0:
        return (-1, -1, -1, -1)

    pub_ok = 1 if _publisher_matches(entry, publisher) else 0
    arch_ok = 0
    entry_arch = str(entry.get("architecture") or "").strip().lower()
    app_arch = str(architecture or "").strip().lower()
    if entry_arch and app_arch:
        # metadata: se ambos presentes e compatíveis, pequeno boost
        if entry_arch in ("any", app_arch, f"x{app_arch}", app_arch.replace("bit", "")):
            arch_ok = 1
        elif app_arch in ("64", "x64") and entry_arch == "x64":
            arch_ok = 1
        elif app_arch in ("32", "x86") and entry_arch == "x86":
            arch_ok = 1

    # version reservado para futuros matches de faixa — ainda não filtra
    _ = version
    return (exact, best_pat_len, pub_ok, arch_ok)


def find_catalog_entry(
    display_name: str,
    publisher: str = "",
    *,
    version: str = "",
    architecture: str = "",
) -> Optional[Dict[str, Any]]:
    """
    Localiza entrada do catálogo.

    Prioridade: nome exato > padrão mais longo > publisher > arquitetura (metadata).
    Substring curta ainda funciona (compat), mas perde para padrões mais específicos.
    """
    name = _normalize_name(display_name)
    if not name:
        return None

    best: Optional[Dict[str, Any]] = None
    best_key = (-1, -1, -1, -1)

    for entry in load_catalog():
        key = _score_match(entry, display_name, publisher, version, architecture)
        if key > best_key:
            best = entry
            best_key = key
    return best


def catalog_uninstall_args(app) -> str:
    """
    Retorna uninstallArgs do catálogo para o app, ou string vazia.
    Aplica-se apenas a instaladores EXE (MSI usa msiexec /qn e ignora o catálogo).
    """
    from remoteops.utils.psinfo import InstalledApp

    if not isinstance(app, InstalledApp):
        return ""
    if app.is_msi and app.product_code:
        return ""
    entry = find_catalog_entry(
        app.display_name or "",
        app.publisher or "",
        version=app.version or "",
        architecture=app.arch or "",
    )
    if not entry:
        entry = find_catalog_entry(
            app.display_line or "",
            app.publisher or "",
            version=app.version or "",
            architecture=app.arch or "",
        )
    if not entry:
        return ""
    return str(entry.get("uninstallArgs") or "").strip()


def resolve_uninstall_extras(app, manual_extras: str = "") -> str:
    """
    Parâmetros efetivos para desinstalação:
    - se o usuário preencheu Parametros Extras, usa isso (override);
    - senão, para EXE, usa uninstallArgs do ApplicationCatalog.json quando houver match;
    - MSI não usa o catálogo (permanece com /qn /norestart + extras manuais, se houver).

    Nota: requiresElevation / requiresReboot / architecture no JSON são metadata
    documental e NÃO alteram o comando gerado nesta versão.
    """
    manual = (manual_extras or "").strip()
    if manual:
        return manual
    return catalog_uninstall_args(app)
