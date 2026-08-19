"""``QThread`` que executa ``run_remote_winget`` em background."""

from __future__ import annotations

import threading

from PyQt6.QtCore import QThread, pyqtSignal

from remoteops.winget.constants import EXEC_ACTIONS, MULTI_ITEM_EXEC_ACTIONS, result_exit_code
from remoteops.winget.remote import run_remote_winget


class WinGetWorker(QThread):
    finished_ok = pyqtSignal(object)
    finished_err = pyqtSignal(str)
    log = pyqtSignal(str)
    item_started = pyqtSignal(int, int, str)
    item_finished = pyqtSignal(int, int, str, int, str)
    item_progress = pyqtSignal(int)

    def __init__(
        self,
        *,
        psexec_path: str,
        host: str,
        username: str,
        password: str,
        action: str,
        ids: list[str] | None = None,
        query: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.psexec_path = psexec_path
        self.host = host
        self.username = username
        self.password = password
        self.action = action
        self.ids = ids or []
        self.query = query or ""
        self._cancel = threading.Event()

    def request_cancel(self) -> None:
        self._cancel.set()

    def _run_one(self, action: str, ids: list[str], query: str, progress_cb):
        return run_remote_winget(
            psexec_path=self.psexec_path,
            host=self.host,
            username=self.username,
            password=self.password,
            action=action,
            ids=ids,
            query=query,
            log_cb=self.log.emit,
            progress_cb=progress_cb,
            cancel_event=self._cancel,
        )

    def _run_single(self) -> None:
        is_exec = self.action in EXEC_ACTIONS
        is_multi = self.action in MULTI_ITEM_EXEC_ACTIONS and len(self.ids) > 1
        progress_cb = self.item_progress.emit if is_exec else None

        emit_item_signals = is_exec and self.action != "upgrade_all"
        if emit_item_signals:
            if is_multi:
                self.item_started.emit(1, len(self.ids), self.ids[0])
            elif self.ids:
                self.item_started.emit(1, 1, self.ids[0])
            else:
                self.item_started.emit(1, 1, self.action)

        if self._cancel.is_set():
            self.finished_err.emit("Operação cancelada pelo usuário.")
            return

        payload = self._run_one(self.action, self.ids, self.query, progress_cb)

        if emit_item_signals:
            results = payload.get("Results") or []
            if is_multi:
                total = len(self.ids)
                for idx, r in enumerate(results, start=1):
                    pkg_id = str(r.get("Id") or (self.ids[idx - 1] if idx <= len(self.ids) else ""))
                    exit_code = result_exit_code(r.get("ExitCode"))
                    output = str(r.get("Output", "") or "")
                    if idx > 1:
                        self.item_started.emit(idx, total, pkg_id)
                    self.item_finished.emit(idx, total, pkg_id, exit_code, output)
            else:
                r0 = results[0] if results else {}
                exit_code = result_exit_code(r0.get("ExitCode"))
                output = str(r0.get("Output", "") or "")
                item_label = self.ids[0] if self.ids else self.action
                self.item_finished.emit(1, 1, item_label, exit_code, output)

        self.finished_ok.emit(payload)

    def run(self) -> None:
        try:
            self._run_single()
        except Exception as exc:
            self.finished_err.emit(str(exc))
