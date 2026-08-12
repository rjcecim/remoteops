"""Roteamento de linhas de log em tempo real durante execuções winget."""

from __future__ import annotations

from remoteops.ui.winget.controllers.progress_controller import ProgressController
from remoteops.winget.clixml import is_clixml_log_noise
from remoteops.winget.constants import (
    EXEC_ACTIONS,
    MARKER_PCT_RE,
    REALTIME_LOG_PREFIX,
    SPINNER_LINES,
)
from remoteops.winget.winget_output import (
    is_winget_download_progress,
    is_winget_download_start,
    is_winget_install_start,
    is_winget_item_complete,
    is_winget_table_chrome,
    normalize_winget_line,
    parse_package_header,
)


class ExecLogRouter:
    """Filtra ruído do winget e atualiza progresso/stream por pacote."""

    def __init__(self, progress: ProgressController) -> None:
        self._progress = progress
        self.exec_action: str | None = None
        self.exec_ids: list[str] = []
        self.stream_seen_ids: list[str] = []
        self.stream_finished_ids: set[str] = set()
        self.saw_realtime_output = False

    def begin_exec(self, action: str, ids: list[str]) -> None:
        self.exec_action = action
        self.exec_ids = list(ids or [])
        self.stream_seen_ids = []
        self.stream_finished_ids = set()
        self.saw_realtime_output = False
        self._progress.begin_exec(exec_ids=self.exec_ids)

    def reset(self) -> None:
        self.exec_action = None
        self.exec_ids = []
        self.stream_seen_ids = []
        self.stream_finished_ids = set()
        self.saw_realtime_output = False
        self._progress.reset()

    def strip_realtime_prefix(self, text: str) -> tuple[str, bool]:
        if (text or "").startswith(REALTIME_LOG_PREFIX):
            return (text or "")[len(REALTIME_LOG_PREFIX) :], True
        return text or "", False

    def process(self, text: str, *, realtime: bool) -> str | None:
        """Devolve a linha para o painel de log, ou ``None`` se consumida/filtrada."""
        pct_m = MARKER_PCT_RE.match((text or "").strip())
        if pct_m is not None:
            try:
                self._progress.on_percent(int(pct_m.group("pct")))
            except Exception:
                pass
            return None

        if is_clixml_log_noise(text):
            return None

        if is_winget_download_progress(text) or is_winget_table_chrome(text):
            return None

        cleaned = normalize_winget_line(text)
        if cleaned in SPINNER_LINES:
            return None

        if realtime and cleaned:
            self.saw_realtime_output = True
            self._handle_stream_event(text, cleaned)

        if self._progress.item_in_progress and cleaned and is_winget_install_start(text):
            self._progress.on_install_start()

        if not cleaned:
            return ""
        return text

    def _handle_stream_event(self, raw: str, cleaned: str) -> None:
        if self.exec_action not in EXEC_ACTIONS:
            return

        pkg_id = parse_package_header(raw)
        if pkg_id and (not self.exec_ids or pkg_id in self.exec_ids):
            self._on_package_started(pkg_id)
        elif is_winget_item_complete(raw):
            self._on_package_finished(cleaned)
        elif is_winget_download_start(raw):
            self._progress.on_download_start()

    def _on_package_started(self, package_id: str) -> None:
        if package_id not in self.stream_seen_ids:
            self.stream_seen_ids.append(package_id)
        if package_id == self._progress.current_pkg_id and self._progress.item_in_progress:
            return
        if (
            self._progress.item_in_progress
            and self._progress.current_pkg_id
            and self._progress.current_pkg_id not in self.stream_finished_ids
        ):
            self._finalize_package(self._progress.current_pkg_id, "")

        idx = self.stream_seen_ids.index(package_id) + 1
        total = len(self.exec_ids) if self.exec_ids else max(idx, self._progress.current_pkg_total)
        self._progress.on_item_started(idx, total, package_id)

    def _on_package_finished(self, hint: str) -> None:
        if not self._progress.item_in_progress or not self._progress.current_pkg_id:
            return
        if self._progress.current_pkg_id in self.stream_finished_ids:
            return
        self._finalize_package(self._progress.current_pkg_id, hint)

    def _finalize_package(self, package_id: str, hint: str) -> None:
        self.stream_finished_ids.add(package_id)
        self._progress.finalize_stream_item(package_id, hint)

    def should_skip_item_started(self, package_id: str) -> bool:
        return package_id in self.stream_finished_ids or (
            package_id == self._progress.current_pkg_id and self._progress.item_in_progress
        )

    def is_stream_finished(self, package_id: str) -> bool:
        return package_id in self.stream_finished_ids
