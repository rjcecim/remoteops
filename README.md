# RemoteOps

Operações remotas no Windows: execução via PsExec, inventário (PsInfo), pesquisa multi-host, desinstalação, Robocopy, MSI/PowerShell/CMD e integração RustDesk.

Evolução do antigo **PSExecGUI**, com a mesma lógica de frontend/backend e organização mais limpa (PEP 8, pacote `remoteops`).

## Requisitos

- Python 3.10+
- PyQt6
- [PSTools](https://learn.microsoft.com/sysinternals/downloads/pstools) (padrão `C:\PSTools`)

## Executar

```bash
cd C:\Users\0101093\Documents\GitHub\RemoteOps
pip install -e ".[dev]"
python main.py
# ou
python -m remoteops
```

## Estrutura

```
RemoteOps/
├── main.py                 # entry point fino
├── remoteops/
│   ├── bootstrap.py        # QApplication + MainWindow
│   ├── paths.py            # caminhos portáteis (dev/exe)
│   ├── core/               # builder, executor, ConPTY, win_cmd
│   ├── services/           # casos de uso (execução, uninstall, RustDesk)
│   ├── ui/                 # MainWindow, abas, widgets
│   └── utils/              # settings, hosts, catalog, redaction…
├── config/                 # ApplicationCatalog.json
├── assets/                 # ícones
└── RemoteOps.spec          # PyInstaller
```

## Empacotar

```bash
pip install -e ".[build]"
python -m PyInstaller --noconfirm --clean RemoteOps.spec
```

Saída: `dist\RemoteOps.exe`

## Configuração local

| Arquivo | Uso |
|---------|-----|
| `settings.ini` | Preferências (PSTools, hosts, workers, log) |
| `hosts.json` | Lista de computadores (veja `hosts.example.json`) |
| `config/ApplicationCatalog.json` | Args de desinstalação silenciosa |

## Segurança

Senhas nunca vão para preview, logs ou arquivos de config. Redação central em `remoteops.utils.redaction`.
