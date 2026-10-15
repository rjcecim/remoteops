"""Identidade enviada ao PsExec (``-u``), sem senha e sem rede.

O modo Windows atual omite ``-u`` e ``-p``: o processo usa o token da sessão.

Conta local usa ``.\\usuário``. No PsExec, o domínio ``.`` é o banco SAM do
computador de destino — inclusive quando o host digitado é um IP ou um FQDN.
``NOME-DO-PC\\usuário`` só equivale a isso quando o host é o nome NetBIOS;
por isso o nome do computador e o domínio descoberto não entram nesse modo.
"""

from __future__ import annotations

from enum import Enum


class AuthKind(str, Enum):
    """Tipo de credencial escolhido na seção Autenticação."""

    WINDOWS = "windows"
    DOMAIN = "domain"
    LOCAL = "local"


def normalize_domain_label(value: str) -> str:
    """Domínio da conta, sem barra e sem o nome do computador embutido."""
    text = (value or "").replace("/", "\\").strip()
    if "\\" in text:
        text = text.split("\\", 1)[0]
    return text.strip().strip("\\").strip()


def normalize_account_name(value: str) -> str:
    """Só o nome da conta, sem prefixo ``.\\`` ou ``DOMÍNIO\\``."""
    raw = (value or "").strip()
    if raw.startswith(".\\") or raw.startswith("./"):
        raw = raw[2:]
    raw = raw.replace("/", "\\").strip()
    if "\\" in raw:
        raw = raw.rsplit("\\", 1)[-1]
    return raw.strip()


def compose_psexec_identity(kind: AuthKind | str, account: str, domain: str = "") -> str:
    """
    Valor de ``-u``, ou vazio quando o PsExec deve herdar o token do processo.

    UPN (``usuario@dominio``) é preservado no modo Domínio, sem segundo prefixo.
    """
    try:
        selected = AuthKind(kind)
    except ValueError:
        return ""
    if selected is AuthKind.WINDOWS:
        return ""

    typed = (account or "").strip()
    if not typed:
        return ""

    if selected is AuthKind.DOMAIN and "@" in typed and "\\" not in typed and "/" not in typed:
        return typed

    name = normalize_account_name(typed)
    if not name:
        return ""
    if selected is AuthKind.LOCAL:
        return f".\\{name}"

    label = normalize_domain_label(domain)
    if label:
        return f"{label}\\{name}"
    return name
