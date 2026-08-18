from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QFileDialog,
    QFileIconProvider,
    QDialog,
    QLineEdit,
)
from PyQt6.QtCore import pyqtSignal, Qt, QFileInfo
import os

from remoteops.ui.branding import APP_NAME, app_mark_pixmap
from remoteops.ui.widgets.card import CardWidget


class _FileOrFolderDialog(QFileDialog):
    """
    Diálogo único:
    - arquivo selecionado → modo arquivo
    - pasta selecionada / diretório atual sem arquivo → modo pasta

    Duplo clique em pasta continua navegando; "Selecionar" confirma.
    """

    def __init__(self, parent=None, caption: str = ""):
        super().__init__(parent, caption)
        self.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        self.setFileMode(QFileDialog.FileMode.ExistingFile)
        self.setLabelText(QFileDialog.DialogLabel.Accept, self.tr("Selecionar"))
        self.picked_kind: str | None = None  # 'file' | 'folder'
        self.picked_path: str | None = None

    @staticmethod
    def _looks_like_filter(text: str) -> bool:
        t = (text or "").strip()
        return (not t) or ("*" in t) or ("?" in t)

    def accept(self) -> None:
        selected = self.selectedFiles()
        path = (selected[0] if selected else "").strip()

        if path and os.path.isfile(path):
            self.picked_kind = "file"
            self.picked_path = os.path.normpath(path)
            QDialog.accept(self)
            return

        if path and os.path.isdir(path):
            self.picked_kind = "folder"
            self.picked_path = os.path.normpath(path)
            QDialog.accept(self)
            return

        line = self.findChild(QLineEdit)
        typed = (line.text() if line is not None else "").strip()
        if typed and not self._looks_like_filter(typed):
            candidate = typed
            if not os.path.isabs(candidate):
                candidate = os.path.join(self.directory().absolutePath(), typed)
            candidate = os.path.normpath(candidate)
            if os.path.isfile(candidate):
                self.picked_kind = "file"
                self.picked_path = candidate
                QDialog.accept(self)
                return
            if os.path.isdir(candidate):
                self.picked_kind = "folder"
                self.picked_path = candidate
                QDialog.accept(self)
                return
            # Nome digitado inválido: não fecha o diálogo
            return

        directory = self.directory().absolutePath()
        if directory and os.path.isdir(directory):
            self.picked_kind = "folder"
            self.picked_path = os.path.normpath(directory)
            QDialog.accept(self)


class FileSelectorWidget(CardWidget):
    # Sinal emitido quando um arquivo ou pasta é selecionado
    fileSelected = pyqtSignal(dict)  # Emite dict: {'mode': 'file'|'folder', 'file': ..., 'folder': ...}
    fileCleared = pyqtSignal()  # Seleção removida (reset do card)
    appSearchRequested = pyqtSignal()  # Abre a tela de pesquisa de aplicativos nos hosts
    settingsRequested = pyqtSignal()  # Abre a aba Configurações

    def __init__(self, parent=None):
        super().__init__("\uE80F", APP_NAME, parent)
        self._title_label.setText(
            f"{APP_NAME} — {self.tr('Instalação e comandos remotos via PsExec')}"
        )
        mark = app_mark_pixmap(16)
        if not mark.isNull():
            self._icon_label.setPixmap(mark)
            self._icon_label.setText("")
        self.selected_file = None
        self.selected_folder = None
        self.selection_mode = None  # 'file' ou 'folder'

        self.search_button = self.make_header_button(
            "\uE721", self.tr("Pesquisar aplicativos nos hosts")
        )
        self.search_button.clicked.connect(self.appSearchRequested.emit)
        self.add_header_button(self.search_button)

        self.browse_button = self.make_header_button(
            "\uED25", self.tr("Selecionar arquivo ou pasta")
        )
        self.browse_button.clicked.connect(self.open_path_dialog)
        self.add_header_button(self.browse_button)
        # Alias para compatibilidade com main.py (antigo file_button)
        self.file_button = self.browse_button

        self.help_button = self.make_header_button(
            "\uE946",
            self.tr("Executar arquivo com /? para ver argumentos disponíveis"),
        )
        self.help_button.setEnabled(False)
        self.help_button.clicked.connect(self.show_help)
        self.add_header_button(self.help_button)

        self.settings_button = self.make_header_button(
            "\uE713", self.tr("Configurações")
        )
        self.settings_button.clicked.connect(self.settingsRequested.emit)
        self.add_header_button(self.settings_button)

        self.set_resettable(True, self.tr("Limpar arquivo ou pasta selecionado"))
        self.resetRequested.connect(self.clear_selection)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(32, 32)
        self.name_label = QLabel(self.tr("Nenhum arquivo ou pasta selecionado"))
        self.name_label.setObjectName("fieldLabel")
        row.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.name_label, 1, Qt.AlignmentFlag.AlignVCenter)
        wrap = QWidget()
        wrap.setLayout(row)
        self.content_layout.addWidget(wrap)
        self.icon_label.hide()

    def open_path_dialog(self):
        dialog = _FileOrFolderDialog(self, self.tr("Selecionar arquivo ou pasta"))
        dialog.setNameFilter(self.tr("Arquivos executáveis (*.exe *.msi *.bat *.ps1)"))
        if not dialog.exec():
            return

        kind = dialog.picked_kind
        path = dialog.picked_path
        if not kind or not path:
            return

        if kind == "file":
            self.set_file(path)
            self.selection_mode = "file"
            self.selected_folder = None
            self.fileSelected.emit({"mode": "file", "file": path, "folder": None})
            return

        # Pasta: mantém o fluxo de escolher o arquivo a executar dentro dela
        self.selected_folder = path
        self.selection_mode = "folder"
        file_path = self._choose_file_in_folder(path)
        if file_path:
            self.set_file(file_path, path)
            self.fileSelected.emit({"mode": "folder", "file": file_path, "folder": path})

    def _choose_file_in_folder(self, folder):
        file_dialog = QFileDialog(self, self.tr("Escolher arquivo a executar nesta pasta"))
        file_dialog.setFileMode(QFileDialog.FileMode.ExistingFile)
        file_dialog.setDirectory(folder)
        file_dialog.setNameFilter(self.tr("Arquivos executáveis (*.exe *.msi *.bat *.ps1)"))
        file_dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        if file_dialog.exec():
            return file_dialog.selectedFiles()[0]
        return None

    def set_file(self, file_path, folder=None):
        self.selected_file = file_path
        self.selected_folder = folder
        if folder:
            display = f"{os.path.basename(folder)}: {os.path.relpath(file_path, folder)}"
        else:
            display = os.path.basename(file_path)
        self.name_label.setText(display)
        icon_provider = QFileIconProvider()
        icon = icon_provider.icon(QFileIconProvider.IconType.File)
        if os.path.exists(file_path):
            file_info = QFileInfo(file_path)
            icon = icon_provider.icon(file_info)
        pixmap = icon.pixmap(32, 32)
        self.icon_label.setPixmap(pixmap)
        self.icon_label.show()

        # Habilitar botão de ajuda apenas se for arquivo .exe
        is_exe = file_path.lower().endswith('.exe')
        self.help_button.setEnabled(is_exe)

    def clear_selection(self):
        """Remove o arquivo/pasta escolhido e volta ao estado inicial do card."""
        if not self.selected_file and not self.selected_folder:
            return
        self.selected_file = None
        self.selected_folder = None
        self.selection_mode = None
        self.name_label.setText(self.tr("Nenhum arquivo ou pasta selecionado"))
        self.icon_label.hide()
        self.icon_label.clear()
        self.help_button.setEnabled(False)
        self.fileCleared.emit()

    def show_help(self):
        """Executa o arquivo com /? para mostrar argumentos disponíveis"""
        if not self.selected_file or not self.selected_file.lower().endswith('.exe'):
            return

        try:
            from remoteops.core.win_cmd import open_external_cmd_k_argv
            exe_path = self.selected_file.replace('"', '')
            open_external_cmd_k_argv([exe_path, "/?"])
        except Exception as e:
            print(f"Erro ao executar {self.selected_file} /?: {e}")
