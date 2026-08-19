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
    parse_found_package,
    parse_package_header,
    parse_upgrade_listing_row,
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
        self.package_names: dict[str, str] = {}

    def begin_exec(
        self,
        action: str,
        ids: list[str],
        package_names: dict[str, str] | None = None,
    ) -> None:
        self.exec_action = action
        self.exec_ids = list(ids or [])
        self.stream_seen_ids = []
        self.stream_finished_ids = set()
        self.saw_realtime_output = False
        self.package_names = {
            str(k).strip(): str(v).strip()
            for k, v in (package_names or {}).items()
            if str(k).strip() and str(v).strip()
        }
        initial = "Iniciando…" if action == "upgrade_all" else ""
        self._progress.begin_exec(exec_ids=self.exec_ids, initial_label=initial)

    def reset(self) -> None:
        self.exec_action = None
        self.exec_ids = []
        self.stream_seen_ids = []
        self.stream_finished_ids = set()
        self.saw_realtime_output = False
        self.package_names = {}
        self._progress.reset()

    def _remember_name(self, pkg_id: str, name: str) -> None:
        pkg_id = (pkg_id or "").strip()
        name = (name or "").strip()
        if not pkg_id or not name:
            return
        existing = self.package_names.get(pkg_id, "")
        if len(name) > len(existing):
            self.package_names[pkg_id] = name

    def _display_name(self, pkg_id: str, fallback: str = "") -> str:
        mapped = (self.package_names.get(pkg_id) or "").strip()
        fb = (fallback or "").strip()
        if mapped and (not fb or len(mapped) >= len(fb)):
            return mapped
        return fb or pkg_id

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

        listing = parse_upgrade_listing_row(text)
        if listing:
            self._remember_name(listing[1], listing[0])

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
            return

        found = parse_found_package(raw)
        if found:
            name, found_id, idx, total = found
            if not self.exec_ids or found_id in self.exec_ids:
                self._remember_name(found_id, name)
                self._on_package_started(
                    found_id,
                    display=self._display_name(found_id, name),
                    idx=idx,
                    total=total,
                )
                return

        listing = parse_upgrade_listing_row(raw)
        if listing:
            self._remember_name(listing[1], listing[0])
            return

        if is_winget_item_complete(raw):
            self._on_package_finished(cleaned)
        elif is_winget_download_start(raw):
            self._progress.on_download_start()

    def _on_package_started(
        self,
        package_id: str,
        *,
        display: str | None = None,
        idx: int | None = None,
        total: int | None = None,
    ) -> None:
        if package_id not in self.stream_seen_ids:
            self.stream_seen_ids.append(package_id)
        if package_id == self._progress.current_pkg_id and self._progress.item_in_progress:
            label = self._display_name(package_id, display or "")
            if label:
                self._progress.set_current_display(label)
            return
        if (
            self._progress.item_in_progress
            and self._progress.current_pkg_id
            and self._progress.current_pkg_id not in self.stream_finished_ids
        ):
            self._finalize_package(self._progress.current_pkg_id, "")

        resolved_idx = idx if idx is not None else self.stream_seen_ids.index(package_id) + 1
        if self.exec_ids:
            resolved_total = len(self.exec_ids)
        elif total is not None and total > 0:
            resolved_total = total
        else:
            resolved_total = max(resolved_idx, self._progress.current_pkg_total)
        self._progress.on_item_started(
            resolved_idx,
            resolved_total,
            package_id,
            display=self._display_name(package_id, display or ""),
        )

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
