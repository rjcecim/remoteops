from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from PyQt6 import sip
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from remoteops.ui.inventory.nav import (
    InventorySidebar,
    all_sections,
    section_icon,
    section_label,
)
from remoteops.ui.inventory.widgets import (
    IdentitySectionPanel,
    OverviewMetricData,
    OverviewPanel,
    SectionHeader,
    SecurityPanel,
    SystemPanel,
    FirmwarePanel,
    HardwarePanel,
    MemoryPanel,
    NetworkPanel,
    VideoPanel,
    StoragePanel,
    UpdatesPanel,
    muted_label,
    scroll_content,
    section_error_widget,
    section_loading_widget,
    value_label,
    wrap_section_page,
)
from remoteops.ui.style import (
    COLOR_BORDER,
    SPACE_SM,
    make_icon_button,
)
from remoteops.utils.inventory.cache import normalize_inventory_host
from remoteops.utils.inventory.models import (
    HardwareData,
    IdentityData,
    InventorySection,
    FirmwareData,
    MemorySummary,
    NetworkData,
    OverviewData,
    QueryContext,
    QueryStatus,
    SectionResult,
    SecurityData,
    StorageData,
    SystemData,
    UpdatesData,
    VideoData,
)
from remoteops.utils.inventory.formatters import is_invalid_psinfo_uptime
from remoteops.utils.inventory.service import InventoryService
from remoteops.utils.pstools import get_pstools_dir

PSINFO_TIMEOUT_SECONDS = 90.0

_SECTION_SUBTITLES: dict[InventorySection, str] = {
    InventorySection.OVERVIEW: "Identificação e diagnóstico rápido do equipamento",
    InventorySection.SYSTEM: "Sistema operacional, build, idioma e ativação",
    InventorySection.HARDWARE: "Fabricante, processador e placa-mãe",
    InventorySection.MEMORY: "Módulos instalados e slots",
    InventorySection.STORAGE: "Discos físicos e volumes",
    InventorySection.NETWORK: "Adaptadores de rede ativos",
    InventorySection.VIDEO: "Placas de vídeo e drivers",
    InventorySection.FIRMWARE: "BIOS, Secure Boot e TPM",
    InventorySection.SECURITY: "BitLocker, Defender, firewall e UAC",
    InventorySection.IDENTITY: "SID do computador e usuário no console",
    InventorySection.UPDATES: "Hotfixes instalados",
}


class _InventoryWorker(QThread):
    finished_ok = pyqtSignal(object)
    finished_err = pyqtSignal(object)

    def __init__(
        self,
        service: InventoryService,
        section: InventorySection,
        host: str,
        query: QueryContext,
        user: str = "",
        password: str = "",
        pstools_dir: str = "",
        force: bool = False,
    ):
        super().__init__()
        self.service = service
        self.section = section
        self.host = host
        self.query = query
        self.user = user or ""
        self.password = password or ""
        self.pstools_dir = pstools_dir
        self.force = force
        self._abort = False

    def abort(self) -> None:
        self._abort = True

    def run(self) -> None:
        try:
            result = self.service.collect_section(
                self.section,
                self.host,
                user=self.user,
                password=self.password,
                pstools_dir=self.pstools_dir,
                force=self.force,
                should_abort=lambda: self._abort,
                query=self.query,
            )
            if self._abort:
                return
            self.finished_ok.emit(result)
        except Exception as exc:
            if not self._abort:
                self.finished_err.emit(
                    SectionResult(
                        section=self.section,
                        status=QueryStatus.ERROR,
                        error=str(exc),
                        query=self.query,
                    )
                )
        finally:
            self.password = ""


class InventarioTab(QWidget):
    """Painel de inventário remoto com navegação lateral."""

    openContasLocaisRequested = pyqtSignal(str)

    def __init__(
        self,
        parent=None,
        log_output=None,
        host_source: Optional[QLineEdit] = None,
        creds_provider: Optional[Callable[[], Tuple[str, str]]] = None,
        service: Optional[InventoryService] = None,
    ):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.log_output = log_output
        self._host_source = host_source
        self._creds_provider = creds_provider
        self._service = service or InventoryService()
        self._worker: Optional[_InventoryWorker] = None
        self._stale_workers: List[_InventoryWorker] = []
        self._active_query: Optional[QueryContext] = None
        self._closed = False
        self._current_section = InventorySection.OVERVIEW
        self._section_pages: Dict[InventorySection, QWidget] = {}
        self._loading_overlay: Optional[QWidget] = None

        root = QVBoxLayout(self)
        root.setContentsMargins(2, 2, 2, 2)
        root.setSpacing(4)

        # Toolbar compacta
        toolbar = QFrame()
        toolbar.setFixedHeight(40)
        toolbar.setStyleSheet(
            f"QFrame {{ background: palette(base); border: 1px solid {COLOR_BORDER}; border-radius: 6px; }}"
        )
        tb_lay = QHBoxLayout(toolbar)
        tb_lay.setContentsMargins(8, 4, 4, 4)
        tb_lay.setSpacing(6)

        self._host_chip = QLabel("")
        self._host_chip.setStyleSheet(
            "font-family: Consolas, monospace; color: palette(windowText); opacity: 0.8; font-size: 8.5pt;"
        )
        self._status_lbl = muted_label("")
        self.refresh_btn = make_icon_button("\uE72C", self.tr("Atualizar seção atual"), size=26)
        self.refresh_all_btn = make_icon_button("\uE895", self.tr("Atualizar inventário completo"), size=26)
        self.refresh_btn.clicked.connect(self.refresh_current)
        self.refresh_all_btn.clicked.connect(self.refresh_all)

        tb_lay.addWidget(value_label(self.tr("Inventário"), bold=True, size=10))
        tb_lay.addWidget(self._host_chip, 1)
        tb_lay.addWidget(self._status_lbl)
        tb_lay.addWidget(self.refresh_btn)
        tb_lay.addWidget(self.refresh_all_btn)
        root.addWidget(toolbar)

        # Corpo: nav + conteúdo
        body = QHBoxLayout()
        body.setSpacing(6)

        self._sidebar = InventorySidebar()
        self._sidebar.sectionSelected.connect(self._on_section_selected)
        body.addWidget(self._sidebar)

        content_wrap = QFrame()
        content_wrap.setObjectName("inventoryContent")
        content_wrap.setStyleSheet(
            "QFrame#inventoryContent { background: palette(base); border: 1px solid palette(mid); border-radius: 10px; }"
        )
        content_lay = QVBoxLayout(content_wrap)
        content_lay.setContentsMargins(SPACE_SM, SPACE_SM, SPACE_SM, SPACE_SM)

        self._stack = QStackedWidget()
        for section in all_sections():
            page = QWidget()
            page_lay = QVBoxLayout(page)
            page_lay.setContentsMargins(0, 0, 0, 0)
            page_lay.addWidget(section_loading_widget())
            self._section_pages[section] = page
            self._stack.addWidget(page)
        content_lay.addWidget(self._stack, 1)
        body.addWidget(content_wrap, 1)
        root.addLayout(body, 1)

        if host_source is not None:
            host_source.textChanged.connect(self._on_host_changed)

        self.destroyed.connect(self._abort_worker)
        self._update_host_chip()

    def _ui_alive(self) -> bool:
        return not self._closed and not sip.isdeleted(self)

    def _is_active_query(self, query: Optional[QueryContext]) -> bool:
        if not self._ui_alive() or query is None or self._active_query is None:
            return False
        if query.host != normalize_inventory_host(self._get_host()):
            return False
        return query.matches(self._active_query)

    def _idle_section_body(self) -> QWidget:
        body = muted_label(self.tr("Nenhum dado para este host."))
        body.setObjectName("inventoryIdleState")
        return body

    def _reset_section_page(self, section: InventorySection) -> None:
        if not self._ui_alive():
            return
        self._present_section(section, self._idle_section_body())

    def _reset_all_section_pages(self) -> None:
        for section in list(self._section_pages):
            self._reset_section_page(section)

    def _get_host(self) -> str:
        if self._host_source is None:
            return ""
        return (self._host_source.text() or "").strip()

    def _get_creds(self) -> Tuple[str, str]:
        if self._creds_provider is None:
            return "", ""
        try:
            return self._creds_provider()
        except Exception:
            return "", ""

    def _update_host_chip(self) -> None:
        host = self._get_host()
        full = f"\\\\{host}" if host else ""
        self._host_chip.setVisible(bool(host))
        self._host_chip.setToolTip(full)
        if host:
            fm = self._host_chip.fontMetrics()
            self._host_chip.setText(fm.elidedText(full, Qt.TextElideMode.ElideMiddle, 220))
        else:
            self._host_chip.setText("")

    def _section_index(self, section: InventorySection) -> int:
        return all_sections().index(section)

    def _make_header(self, section: InventorySection) -> SectionHeader:
        return SectionHeader(
            section_icon(section),
            section_label(section),
            _SECTION_SUBTITLES.get(section, ""),
            compact=True,
        )

    def _present_section(self, section: InventorySection, body: QWidget, *, align_top: bool = False) -> None:
        page = wrap_section_page(
            self._make_header(section),
            scroll_content(body, align_top=align_top),
        )
        self._set_page_content(section, page)

    def _present_overview(self, panel: OverviewPanel) -> None:
        """Visão geral: conteúdo compacto alinhado ao topo."""
        self._present_section(InventorySection.OVERVIEW, panel, align_top=True)

    def _on_host_changed(self, _text: str) -> None:
        self._retire_worker()
        self._active_query = None
        self._service.bind_host(self._get_host())
        self._service.invalidate_all()
        self._reset_all_section_pages()
        self._set_loading(False)
        self._status_lbl.setText("")
        self._update_host_chip()

    def _on_section_selected(self, section: InventorySection) -> None:
        self._current_section = section
        idx = self._section_index(section)
        self._stack.setCurrentIndex(idx)
        cached = self._service.cache.get(section)
        if cached is not None:
            self._render_section(section, cached)
        else:
            self._load_section(section)

    def _log(self, msg: str) -> None:
        if self.log_output:
            self.log_output.append_log(self.tr(f"[INVENTÁRIO] {msg}"))

    def _disconnect_worker(self, w: _InventoryWorker) -> None:
        for sig, slot in (
            (w.finished_ok, self._on_collect_ok),
            (w.finished_err, self._on_collect_err),
            (w.finished, self._on_worker_finished),
        ):
            try:
                sig.disconnect(slot)
            except TypeError:
                pass

    def _retire_worker(self) -> Optional[InventorySection]:
        w = self._worker
        if w is None:
            return None
        self._worker = None
        old_section = w.section
        w.abort()
        self._stale_workers.append(w)
        return old_section

    def _cleanup_worker(self, w: Optional[_InventoryWorker]) -> None:
        if w is None:
            return
        if w is self._worker:
            self._worker = None
        if w in self._stale_workers:
            self._stale_workers.remove(w)
        self._disconnect_worker(w)
        w.deleteLater()

    def _iter_workers(self) -> List[_InventoryWorker]:
        workers: List[_InventoryWorker] = []
        if self._worker is not None:
            workers.append(self._worker)
        workers.extend(self._stale_workers)
        return workers

    def _abort_worker(self, _obj=None) -> None:
        self._active_query = None
        workers = self._iter_workers()
        self._worker = None
        self._stale_workers = []
        for w in workers:
            try:
                w.abort()
            except Exception:
                pass
            self._disconnect_worker(w)
            if w.isRunning():
                w.wait(max(3000, int(PSINFO_TIMEOUT_SECONDS * 1000)))
            w.deleteLater()

    def shutdown(self, wait_ms: int = 8000) -> None:
        self._closed = True
        self._active_query = None
        workers = self._iter_workers()
        self._worker = None
        self._stale_workers = []
        for w in workers:
            try:
                w.abort()
            except Exception:
                pass
            self._disconnect_worker(w)
        for w in workers:
            if w.isRunning():
                w.wait(max(0, int(wait_ms)))
            w.deleteLater()

    def _set_loading(self, loading: bool, message: str = "") -> None:
        if not self._ui_alive():
            return
        self.refresh_btn.setEnabled(not loading)
        self.refresh_all_btn.setEnabled(not loading)
        self._sidebar.set_enabled_nav(not loading)
        if loading:
            self._status_lbl.setText(message or self.tr("Coletando..."))
            page = self._section_pages.get(self._current_section)
            if page is not None:
                lay = page.layout()
                if lay is not None:
                    while lay.count():
                        item = lay.takeAt(0)
                        w = item.widget()
                        if w:
                            w.hide()
                            w.setParent(None)
                            w.deleteLater()
                    lay.addWidget(section_loading_widget(message or self.tr("Coletando...")))
        else:
            self._update_host_chip()
            host = self._get_host()
            self._status_lbl.setText(self.tr("Atualizado") if host else "")

    def run_inventory(self) -> None:
        """Abre/coleta a seção atual (Visão geral na primeira abertura)."""
        if not self._get_host():
            self._log("Preencha o Host remoto na aba PsExec.")
            return
        self._load_section(self._current_section)

    def refresh_current(self) -> None:
        if not self._get_host():
            return
        self._service.invalidate_section(self._current_section)
        self._load_section(self._current_section, force=True)

    def refresh_all(self) -> None:
        if not self._get_host():
            return
        self._service.invalidate_all()
        self._load_section(self._current_section, force=True)

    def _load_section(self, section: InventorySection, *, force: bool = False) -> None:
        host = self._get_host()
        if not host:
            return

        retired = self._retire_worker()
        if retired is not None and retired != section:
            self._reset_section_page(retired)

        query = self._service.begin_query(host, section)
        self._active_query = query

        labels = {s: section_label(s) for s in all_sections()}
        self._set_loading(True, self.tr(f"Coletando {labels.get(section, '')}..."))
        user, password = self._get_creds()

        self._worker = _InventoryWorker(
            self._service,
            section,
            host,
            query=query,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
            force=force,
        )
        self._worker.finished_ok.connect(self._on_collect_ok)
        self._worker.finished_err.connect(self._on_collect_err)
        self._worker.finished.connect(self._on_worker_finished)
        self._worker.start()
        self._log(f"Coletando {labels.get(section, section.value)} de {host}...")

    def _on_worker_finished(self, worker: Optional[_InventoryWorker] = None) -> None:
        w = worker if worker is not None else self.sender()
        if w is not self._worker:
            if isinstance(w, _InventoryWorker):
                self._cleanup_worker(w)
            return
        self._set_loading(False)
        self._cleanup_worker(w)

    def _on_collect_err(self, result_obj: object) -> None:
        if not self._ui_alive():
            return
        query: Optional[QueryContext] = None
        section = self._current_section
        msg = ""
        if isinstance(result_obj, SectionResult):
            query = result_obj.query
            section = result_obj.section
            msg = result_obj.error
        elif isinstance(result_obj, str):
            msg = result_obj
        if not self._is_active_query(query):
            return
        self._set_loading(False)
        self._log(msg)
        err_page = wrap_section_page(
            self._make_header(section),
            section_error_widget(msg),
        )
        self._set_page_content(section, err_page)

    def _on_collect_ok(self, result_obj: object) -> None:
        if not self._ui_alive():
            return
        if not isinstance(result_obj, SectionResult):
            return
        if not self._is_active_query(result_obj.query):
            return
        self._set_loading(False)
        payload = result_obj.payload
        if payload is not None:
            self._render_section(result_obj.section, payload)
        elif result_obj.error:
            err_page = wrap_section_page(
                self._make_header(result_obj.section),
                section_error_widget(result_obj.error),
            )
            self._set_page_content(result_obj.section, err_page)

    def _set_page_content(self, section: InventorySection, widget: QWidget) -> None:
        page = self._section_pages.get(section)
        if page is None:
            return
        lay = page.layout()
        if lay is None:
            return
        while lay.count():
            item = lay.takeAt(0)
            w = item.widget()
            if w:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        lay.addWidget(widget, 1)

    def _render_section(self, section: InventorySection, payload: object) -> None:
        renderers = {
            InventorySection.OVERVIEW: self._render_overview,
            InventorySection.SYSTEM: self._render_system,
            InventorySection.HARDWARE: self._render_hardware,
            InventorySection.MEMORY: self._render_memory,
            InventorySection.STORAGE: self._render_storage,
            InventorySection.NETWORK: self._render_network,
            InventorySection.VIDEO: self._render_video,
            InventorySection.FIRMWARE: self._render_firmware,
            InventorySection.SECURITY: self._render_security,
            InventorySection.IDENTITY: self._render_identity,
            InventorySection.UPDATES: self._render_updates,
        }
        fn = renderers.get(section)
        if fn:
            fn(payload)

    def _render_overview(self, data: OverviewData) -> None:
        device_line = " ".join(
            p for p in [data.manufacturer, data.model] if p and p != "—"
        )
        meta_parts: List[str] = []
        if data.domain and data.domain != "—":
            meta_parts.append(f"Domínio: {data.domain}")
        if data.uptime and data.uptime != "—" and not is_invalid_psinfo_uptime(data.uptime):
            meta_parts.append(f"Uptime: {data.uptime}")
        meta_line = "  ·  ".join(meta_parts)

        metrics = [
            OverviewMetricData("\uE950", "Processador", data.cpu_summary, data.cpu_detail),
            OverviewMetricData("\uE8F1", "Memória", data.memory_summary, data.memory_detail),
            OverviewMetricData("\uE7B8", "Armazenamento", data.storage_summary, data.storage_detail),
            OverviewMetricData("\uE968", "Rede", data.network_summary, data.network_detail),
            OverviewMetricData(
                "\uE72E",
                "Segurança",
                data.security_summary or self.tr("Ver seção"),
                data.security_detail,
            ),
            OverviewMetricData(
                "\uE895",
                "Atualizações",
                data.updates_summary or self.tr("Ver seção"),
                data.updates_detail,
            ),
        ]

        panel = OverviewPanel(
            data.hostname or self._get_host(),
            device_line,
            data.os_summary,
            meta_line,
            metrics,
        )
        self._present_overview(panel)

    def _render_system(self, data: SystemData) -> None:
        if data.status == QueryStatus.ERROR:
            self._present_section(InventorySection.SYSTEM, section_error_widget(data.error))
            return
        self._present_section(
            InventorySection.SYSTEM,
            SystemPanel(data.rows),
            align_top=True,
        )

    def _render_hardware(self, data: HardwareData) -> None:
        if data.status == QueryStatus.ERROR:
            self._present_section(InventorySection.HARDWARE, section_error_widget(data.error))
            return
        self._present_section(
            InventorySection.HARDWARE,
            HardwarePanel(data.fields),
            align_top=True,
        )

    def _render_memory(self, data: MemorySummary) -> None:
        if data.status == QueryStatus.ERROR:
            self._present_section(InventorySection.MEMORY, section_error_widget(data.error))
            return
        self._present_section(
            InventorySection.MEMORY,
            MemoryPanel(data),
            align_top=True,
        )

    def _render_storage(self, data: StorageData) -> None:
        if data.status == QueryStatus.ERROR and not data.volumes and not data.physical_disks:
            self._present_section(InventorySection.STORAGE, section_error_widget(data.error))
            return
        self._present_section(
            InventorySection.STORAGE,
            StoragePanel(data),
            align_top=True,
        )

    def _render_network(self, data: NetworkData) -> None:
        if data.status == QueryStatus.ERROR:
            self._present_section(InventorySection.NETWORK, section_error_widget(data.error))
            return
        self._present_section(
            InventorySection.NETWORK,
            NetworkPanel(data),
            align_top=True,
        )

    def _render_video(self, data: VideoData) -> None:
        if data.status == QueryStatus.ERROR:
            self._present_section(InventorySection.VIDEO, section_error_widget(data.error))
            return
        self._present_section(
            InventorySection.VIDEO,
            VideoPanel(data),
            align_top=True,
        )

    def _render_firmware(self, data: FirmwareData) -> None:
        if data.status == QueryStatus.ERROR:
            self._present_section(InventorySection.FIRMWARE, section_error_widget(data.error))
            return
        self._present_section(
            InventorySection.FIRMWARE,
            FirmwarePanel(data),
            align_top=True,
        )

    def _render_security(self, data: SecurityData) -> None:
        if data.status == QueryStatus.ERROR and not data.items:
            self._present_section(InventorySection.SECURITY, section_error_widget(data.error))
            return
        self._present_section(
            InventorySection.SECURITY,
            SecurityPanel(data),
            align_top=True,
        )

    def _render_identity(self, data: IdentityData) -> None:
        if data.status == QueryStatus.ERROR and not data.computer_sid and not data.user_sid:
            self._present_section(InventorySection.IDENTITY, section_error_widget(data.error))
            return
        panel = IdentitySectionPanel(data)
        panel.copy_requested.connect(self._copy_clipboard)
        panel.sid_query_requested.connect(self._query_sid)
        panel.open_contas_locais_requested.connect(self.openContasLocaisRequested.emit)
        self._identity_panel = panel
        self._present_section(
            InventorySection.IDENTITY,
            panel,
            align_top=True,
        )

    def _query_sid(self, account: str) -> None:
        account = (account or "").strip()
        if not account:
            return
        host = self._get_host()
        user, password = self._get_creds()
        from remoteops.utils.inventory.psget_sid import run_psgetsid

        acct, sid, err = run_psgetsid(
            host,
            account=account,
            user=user,
            password=password,
            pstools_dir=get_pstools_dir(),
        )
        panel = getattr(self, "_identity_panel", None)
        if panel is None:
            return
        if sid:
            panel.set_sid_query_result(f"{acct}: {sid}" if acct else sid)
        else:
            panel.set_sid_query_result(err or "Não foi possível consultar.")

    @staticmethod
    def _copy_clipboard(text: str) -> None:
        if text and text != "—":
            QApplication.clipboard().setText(text)

    def _render_updates(self, data: UpdatesData) -> None:
        if data.status == QueryStatus.ERROR and not data.hotfixes:
            self._present_section(InventorySection.UPDATES, section_error_widget(data.error))
            return
        self._present_section(
            InventorySection.UPDATES,
            UpdatesPanel(data),
            align_top=True,
        )


# Compatibilidade com código que ainda referencia PsInfoTab
PsInfoTab = InventarioTab
