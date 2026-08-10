from PyQt6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFileDialog,
    QFileIconProvider,
    QToolButton,
    QDialog,
    QLineEdit,
)
from PyQt6.QtGui import QFont
from PyQt6.QtCore import pyqtSignal, Qt, QFileInfo
import os

from remoteops.ui.style import ICON_FONT_PT

_MDL2_FONT = QFont("Segoe MDL2 Assets", ICON_FONT_PT)
_MDL2_BTN_STYLE = f"""
    QToolButton, QPushButton {{
        border: 1px solid palette(mid);
        border-radius: 4px;
        background: palette(button);
        font-family: "Segoe MDL2 Assets";
        font-size: {ICON_FONT_PT}pt;
        color: palette(highlight);
    }}
    QToolButton:hover, QPushButton:hover {{ background: palette(light); }}
    QToolButton:pressed, QPushButton:pressed {{ background: palette(dark); }}
    QToolButton:disabled, QPushButton:disabled {{ color: palette(mid); }}
"""


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


class FileSelectorWidget(QWidget):
    # Sinal emitido quando um arquivo ou pasta é selecionado
    fileSelected = pyqtSignal(dict)  # Emite dict: {'mode': 'file'|'folder', 'file': ..., 'folder': ...}
    appSearchRequested = pyqtSignal()  # Abre a tela de pesquisa de aplicativos nos hosts
    settingsRequested = pyqtSignal()  # Abre a aba Configurações

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_file = None
        self.selected_folder = None
        self.selection_mode = None  # 'file' ou 'folder'
        self.layout_widget = QHBoxLayout(self)
        # Compacto como no layout original
        self.layout_widget.setContentsMargins(0, 0, 0, 0)
        self.layout_widget.setSpacing(5)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(32, 32)
        self.name_label = QLabel(self.tr("Nenhum arquivo ou pasta selecionado"))

        # \uE721 = Find / Search (Segoe MDL2 Assets)
        self.search_button = QToolButton()
        self.search_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.search_button.setText("\uE721")
        self.search_button.setFont(_MDL2_FONT)
        self.search_button.setStyleSheet(_MDL2_BTN_STYLE)
        self.search_button.setToolTip(self.tr("Pesquisar aplicativos nos hosts"))
        self.search_button.setFixedSize(32, 32)
        self.search_button.clicked.connect(self.appSearchRequested.emit)

        # \uED25 = OpenFile — único botão para arquivo ou pasta
        self.browse_button = QToolButton()
        self.browse_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.browse_button.setText("\uED25")
        self.browse_button.setFont(_MDL2_FONT)
        self.browse_button.setStyleSheet(_MDL2_BTN_STYLE)
        self.browse_button.setToolTip(self.tr("Selecionar arquivo ou pasta"))
        self.browse_button.setFixedSize(32, 32)
        self.browse_button.clicked.connect(self.open_path_dialog)
        # Alias para compatibilidade com main.py (antigo file_button)
        self.file_button = self.browse_button

        # \uE946 = Info / Help (Segoe MDL2 Assets)
        self.help_button = QPushButton("\uE946")
        self.help_button.setFont(_MDL2_FONT)
        self.help_button.setStyleSheet(_MDL2_BTN_STYLE)
        self.help_button.setToolTip(self.tr("Executar arquivo com /? para ver argumentos disponíveis"))
        self.help_button.setFixedSize(32, 32)
        self.help_button.setEnabled(False)
        self.help_button.clicked.connect(self.show_help)

        # \uE713 = Setting / engrenagem (Segoe MDL2 Assets)
        self.settings_button = QToolButton()
        self.settings_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.settings_button.setText("\uE713")
        self.settings_button.setFont(_MDL2_FONT)
        self.settings_button.setStyleSheet(_MDL2_BTN_STYLE)
        self.settings_button.setToolTip(self.tr("Configurações"))
        self.settings_button.setFixedSize(32, 32)
        self.settings_button.clicked.connect(self.settingsRequested.emit)

        self.layout_widget.addWidget(self.icon_label)
        self.layout_widget.addWidget(self.name_label, 1)
        self.layout_widget.addWidget(self.search_button)
        self.layout_widget.addWidget(self.browse_button)
        self.layout_widget.addWidget(self.help_button)
        self.layout_widget.addWidget(self.settings_button)
        self.setLayout(self.layout_widget)
        self.icon_label.hide()  # só aparece quando há arquivo selecionado

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
