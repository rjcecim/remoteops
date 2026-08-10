"""Carregamento e validação de hosts.json (configuração local)."""

from __future__ import annotations

import json
import os
from typing import List, Optional, Tuple

from remoteops.paths import project_root


def app_dir() -> str:
    return str(project_root())


def default_hosts_path() -> str:
    return os.path.join(app_dir(), "hosts.json")


def example_hosts_path() -> str:
    return os.path.join(app_dir(), "hosts.example.json")


def load_hosts_file(path: str) -> List[str]:
    """Carrega lista de hosts do JSON no formato {\"hosts\": [\"HOST1\", ...]}."""
    with open(path, "r", encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict) or "hosts" not in data:
        raise ValueError('Arquivo inválido: esperado um objeto com a chave "hosts".')
    hosts_raw = data["hosts"]
    if not isinstance(hosts_raw, list):
        raise ValueError('Arquivo inválido: "hosts" deve ser uma lista.')
    out: List[str] = []
    seen: set[str] = set()
    for item in hosts_raw:
        h = str(item or "").strip().strip("\\")
        if not h:
            continue
        # Rejeita caracteres perigosos para UNC/shell
        if any(ch in h for ch in ('&', '|', '<', '>', '^', '"', "'", '%', ' ', '\t')):
            continue
        key = h.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    if not out:
        raise ValueError("Nenhum host válido encontrado no arquivo.")
    return out


def resolve_hosts_path(preferred: Optional[str] = None) -> Tuple[Optional[str], str]:
    """
    Retorna (path, origem).
    Preferência: preferred → hosts.json local → None (usuário deve selecionar).
    Não cria hosts.json automaticamente a partir do example (evita sobrescrever).
    """
    if preferred and os.path.isfile(preferred):
        return preferred, "preferred"
    default = default_hosts_path()
    if os.path.isfile(default):
        return default, "local"
    return None, "missing"
