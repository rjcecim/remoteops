"""Consulta de contas locais via PowerShell remoto (PsExec)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from remoteops.utils.host_reachability import is_stale_host_result
from remoteops.utils.inventory.remote_exec import run_remote_powershell
from remoteops.utils.local_accounts import (
    LOCAL_ACCOUNTS_QUERY_SCRIPT,
    LocalAccount,
    parse_local_accounts_payload,
    validate_local_account_host,
)
from remoteops.utils.ping import normalize_host
from remoteops.utils.pstools import get_pstools_dir


@dataclass
class LocalAccountsQueryResult:
    host: str
    accounts: List[LocalAccount] = field(default_factory=list)
    error: str = ""
    stale: bool = False


class LocalAccountsService:
    """Lista contas locais de um host remoto."""

    def query(
        self,
        host: str,
        *,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
        timeout: float = 90.0,
        current_host: str = "",
    ) -> LocalAccountsQueryResult:
        wanted = normalize_host(host)
        visible = normalize_host(current_host) or wanted
        if is_stale_host_result(wanted, wanted, visible) and current_host:
            return LocalAccountsQueryResult(
                host=wanted,
                error="O host da aba PsExec mudou durante a operação.",
                stale=True,
            )

        errors = validate_local_account_host(wanted)
        if errors:
            return LocalAccountsQueryResult(host=wanted, error=errors[0])

        data, err = run_remote_powershell(
            wanted,
            LOCAL_ACCOUNTS_QUERY_SCRIPT,
            user=user,
            password=password,
            timeout=timeout,
            pstools_dir=pstools_dir or get_pstools_dir(),
        )
        if err and data is None:
            return LocalAccountsQueryResult(host=wanted, error=err)

        accounts, parse_err = parse_local_accounts_payload(data, host=wanted)
        if parse_err:
            return LocalAccountsQueryResult(host=wanted, error=parse_err)
        return LocalAccountsQueryResult(host=wanted, accounts=accounts, error=err)
