"""Validação de arquivo MSI e argumentos do msiexec (sem shell)."""

from __future__ import annotations

import os
import re
from typing import Iterable, List, Sequence, Tuple


def _extract_flag_value(combo_text: str) -> str:
    if not combo_text or combo_text == "Nenhum":
        return ""
    if "(" in combo_text:
        return combo_text.split("(")[0].strip()
    return combo_text.strip()


def _split_extra_args(extra: str) -> List[str]:
    s = (extra or "").strip()
    if not s:
        return []
    parts: List[str] = []
    buf: List[str] = []
    in_quote = False
    quote_char = ""
    for ch in s:
        if in_quote:
            if ch == quote_char:
                in_quote = False
            else:
                buf.append(ch)
            continue
        if ch in ('"', "'"):
            in_quote = True
            quote_char = ch
            continue
        if ch.isspace():
            if buf:
                parts.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts

# Compound File Binary (OLE) — assinatura de pacotes MSI clássicos.
MSI_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAX_MSI_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
ALLOWED_REPAIR_CHARS = frozenset("poedcaumsv")
_PROP_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_UNSAFE = re.compile(r"[&|<>^\r\n`]")
_ALLOWED_MSIEXEC_FLAGS = frozenset(
    {
        "/i",
        "/x",
        "/a",
        "/jm",
        "/ju",
        "/quiet",
        "/passive",
        "/qn",
        "/qb",
        "/qr",
        "/qf",
        "/norestart",
        "/promptrestart",
        "/forcerestart",
    }
)


def validate_msi_file(path: str) -> List[str]:
    """Valida extensão, existência, tamanho e assinatura CFB do MSI."""
    errors: List[str] = []
    raw = (path or "").strip()
    if not raw:
        return ["Arquivo MSI não selecionado."]
    if not raw.lower().endswith(".msi"):
        errors.append("Extensão inválida: selecione um arquivo .msi.")
    if not os.path.isfile(raw):
        errors.append("Arquivo MSI não encontrado.")
        return errors
    try:
        size = os.path.getsize(raw)
    except OSError as exc:
        errors.append(f"Não foi possível obter o tamanho do MSI: {exc}")
        return errors
    if size <= 0:
        errors.append("Arquivo MSI vazio.")
    elif size > MAX_MSI_BYTES:
        errors.append("Arquivo MSI excede o tamanho máximo permitido (2 GB).")
    try:
        with open(raw, "rb") as handle:
            magic = handle.read(len(MSI_CFB_MAGIC))
    except OSError as exc:
        errors.append(f"Não foi possível ler o arquivo MSI: {exc}")
        return errors
    if magic != MSI_CFB_MAGIC:
        errors.append("Conteúdo inválido: o arquivo não tem a assinatura de um pacote MSI.")
    return errors


def _has_unsafe(text: str) -> bool:
    return bool(_UNSAFE.search(text or ""))


def validate_msi_log_path(path: str) -> List[str]:
    text = (path or "").strip()
    if not text:
        return ["Caminho de log MSI vazio."]
    if _has_unsafe(text):
        return ["Caminho de log MSI contém caracteres não permitidos."]
    if text.startswith("-") or text.startswith("/"):
        return ["Caminho de log MSI inválido."]
    return []


def validate_msi_repair(flags: str) -> List[str]:
    text = (flags or "").strip()
    if not text:
        return []
    if any(ch.lower() not in ALLOWED_REPAIR_CHARS for ch in text if not ch.isspace()):
        return ["Opções de reparo MSI inválidas (use apenas p,o,e,d,c,a,u,m,s,v)."]
    return []


def split_msi_property_tokens(text: str) -> List[str]:
    return _split_extra_args(text or "")


def validate_msi_property_tokens(text: str) -> Tuple[List[str], List[str]]:
    """Aceita somente ``NOME=VALOR``; rejeita injeção e comandos arbitrários."""
    valid: List[str] = []
    errors: List[str] = []
    for token in split_msi_property_tokens(text):
        if not token:
            continue
        if token.lower() in _ALLOWED_MSIEXEC_FLAGS or token.lower().startswith("/l"):
            errors.append(f"Flag msiexec extra não permitida neste campo: {token}")
            continue
        if "=" not in token:
            errors.append(f"Propriedade MSI inválida (use NOME=VALOR): {token}")
            continue
        name, value = token.split("=", 1)
        if not _PROP_NAME.match(name):
            errors.append(f"Nome de propriedade MSI inválido: {name}")
            continue
        if _has_unsafe(name) or _has_unsafe(value):
            errors.append(f"Propriedade MSI contém caracteres não permitidos: {name}")
            continue
        valid.append(f"{name}={value}")
    return valid, errors


def validate_msi_action(action: str) -> List[str]:
    value = _extract_flag_value(action)
    if not value or value == "Nenhum":
        return []
    if value.lower() not in {flag.lower() for flag in ("/i", "/x", "/a", "/jm", "/ju")}:
        return [f"Ação MSI não permitida: {action}"]
    return []


def validate_msi_interface(interface: str) -> List[str]:
    value = _extract_flag_value(interface)
    if not value or value == "Nenhum":
        return []
    allowed = {"/quiet", "/passive", "/qn", "/qb", "/qr", "/qf"}
    if value.lower() not in allowed:
        return [f"Modo de interface MSI não permitido: {interface}"]
    return []


def validate_msi_restart(restart: str) -> List[str]:
    value = _extract_flag_value(restart)
    if not value or value == "Nenhum":
        return []
    allowed = {"/norestart", "/promptrestart", "/forcerestart"}
    if value.lower() not in allowed:
        return [f"Política de reinício MSI não permitida: {restart}"]
    return []


def validate_msi_params(params: dict) -> List[str]:
    """Valida o dicionário da aba MSI sem alterar as escolhas do usuário."""
    data = params or {}
    errors: List[str] = []
    errors.extend(validate_msi_action(str(data.get("action") or "")))
    errors.extend(validate_msi_interface(str(data.get("interface") or "")))
    errors.extend(validate_msi_restart(str(data.get("restart") or "")))
    if data.get("log"):
        errors.extend(validate_msi_log_path(str(data.get("log_file") or "")))
    errors.extend(validate_msi_repair(str(data.get("repair") or "")))
    _valid, prop_errors = validate_msi_property_tokens(str(data.get("update") or ""))
    errors.extend(prop_errors)
    return errors


def sanitize_repair_flags(flags: str) -> str:
    return "".join(ch for ch in (flags or "") if ch.lower() in ALLOWED_REPAIR_CHARS)


def collect_validation_errors(
    *,
    msi_path: str,
    msi_params: dict,
    extra_checks: Iterable[str] = (),
) -> List[str]:
    errors = validate_msi_file(msi_path)
    errors.extend(validate_msi_params(msi_params))
    errors.extend(item for item in extra_checks if item)
    return errors


def format_validation_errors(errors: Sequence[str]) -> str:
    return "; ".join(str(item) for item in errors if item)


def filter_msiexec_extra_tokens(text: str) -> Tuple[List[str], List[str]]:
    """Permite só flags msiexec conhecidas ou ``NOME=VALOR``; bloqueia comandos."""
    kept: List[str] = []
    errors: List[str] = []
    for token in _split_extra_args(text):
        if not token:
            continue
        low = token.lower()
        if low in _ALLOWED_MSIEXEC_FLAGS or re.fullmatch(r"/l[a-z*!]*", low):
            kept.append(token)
            continue
        if "=" in token:
            valid, prop_errors = validate_msi_property_tokens(token)
            kept.extend(valid)
            errors.extend(prop_errors)
            continue
        errors.append(f"Argumento msiexec não permitido: {token}")
    return kept, errors
