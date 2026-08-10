<p align="center">
  <img src="https://img.shields.io/badge/version-2.0.0-0063C4?style=flat-square" alt="Version" />
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/PyQt6-GUI-41CD52?style=flat-square&logo=qt&logoColor=white" alt="PyQt6" />
  <img src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows" />
</p>

# RemoteOps 2.0.0

> Operações remotas no Windows: PsExec, inventário, pesquisa multi-host, desinstalação, Robocopy e RustDesk — evolução limpa do antigo **PSExecGUI**.

<p align="center">
  <img src="assets/app_icon.png" alt="RemoteOps" width="96" />
</p>

Interface moderna com identidade visual própria, preview em tempo real e abas dinâmicas. Execute instaladores, scripts e comandos em hosts remotos sem digitar linhas de comando — com pacote Python organizado (`remoteops`) e empacotamento portátil.

---

## Índice

- [Visão geral](#visão-geral)
- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Uso rápido](#uso-rápido)
- [Segurança de credenciais](#segurança-de-credenciais)
- [hosts.json](#hostsjson)
- [Logging](#logging)
- [Build](#build)
- [Estrutura do projeto](#estrutura-do-projeto)

---

## Visão geral

| Recurso | Descrição |
|--------|-----------|
| **Arquivos** | `.exe`, `.msi`, `.ps1`, `.bat` e outros — seleção por arquivo ou pasta |
| **Cópia remota** | Robocopy integrado para enviar arquivos/pastas ao host antes de executar |
| **Comando manual** | Digite o comando remoto (ex.: `cmd`, `powershell`) quando não houver arquivo |
| **Preview** | Comando sanitizado (senha mascarada) atualizado em tempo real |
| **Execução** | ConPTY / processo Windows sem `shell=True` |
| **Inventário** | PsInfo remoto + Remote Registry (32/64 bits) |
| **Busca multi-host** | Pesquisa de aplicativos em lista de hosts (`hosts.json`) |
| **RustDesk** | Coleta ID no host e abre conexão local |
| **Portátil** | `settings.ini`, `hosts.json` e `logs/` ao lado do exe |

Versão do aplicativo: **`2.0.0`** (fonte programática: `remoteops.core.version.__version__`).

---

## Requisitos

| Item | Observação |
|------|------------|
| **Sistema** | Windows 10 ou 11 |
| **Python** | 3.10+ |
| **PyQt6** | Interface gráfica |
| **PsExec** | PSTools — padrão `C:\PSTools\` |
| **PsInfo64** | Inventário remoto |
| **RustDesk** | Opcional — conexão remota |
| **Rede** | SMB / Remote Registry conforme o fluxo |

---

## Instalação

```bash
cd RemoteOps
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
# opcional — assets / PyInstaller:
pip install -e ".[build]"
```

Ou apenas runtime:

```bash
pip install PyQt6
```

---

## Uso rápido

```bash
python main.py
# ou
python -m remoteops
```

1. Selecione um arquivo (ou pasta) no cabeçalho.
2. Preencha **host remoto** e, se necessário, usuário/senha na aba **PsExec**.
3. Ajuste opções nas abas (MSI, PowerShell, CMD, Robocopy).
4. Confira o **preview** (senha aparece como `********`) e clique em **Executar**.

### Inventário (PsInfo)

Botão **PsInfo** ao lado do Ping — coleta sistema, aplicativos e discos.

### RustDesk

Botão **RustDesk** — obtém o ID no host e abre `rustdesk.exe --connect <ID>` localmente.

---

## Segurança de credenciais

Política central em `remoteops.utils.redaction`:

- Preview, logs e arquivos usam texto sanitizado (flag `-p` isolada; não mascara `-Path`/`-Profile`/`-Priority`).
- O builder **não** guarda a senha bruta — apenas sabe se há senha e mostra `-p ********`.
- A senha é coletada na UI no momento da execução (`CredentialContext`), injetada no argv e desreferenciada em seguida.
- Preferência: use a sessão Windows atual (sem `-u`/`-p`) quando possível.

### Limitações honestas

- Com `-u`/`-p`, a senha permanece na command line do processo Windows (limitação do PsExec) — inspecionável pelo SO.
- Strings Python não são zeroizadas criptograficamente.

---

## hosts.json

Arquivo **local** (não versionado no Git; hosts reais ficam só na máquina):

1. Copie `hosts.example.json` → `hosts.json`
2. Edite com os nomes dos computadores do seu ambiente

`hosts.json` está no `.gitignore` e **não** é rastreado.  
`hosts.example.json` permanece versionado (somente hosts fictícios).

---

## Logging

Log em arquivo (opcional, preferência em Configurações):

- Pasta `logs\` na raiz do app (dev) ou ao lado do `RemoteOps.exe`
- Arquivo único: `logs\app.log`
- Preferências em `settings.ini` (também local, não versionado)

---

## Build

```bash
pip install -e ".[build]"
python -m PyInstaller --noconfirm --clean RemoteOps.spec
```

Gera `dist/RemoteOps.exe` com `assets/`, `config/` e `hosts.example.json`. **Não** empacota `hosts.json`, credenciais ou logs.

---

## Estrutura do projeto

```
RemoteOps/
├── main.py                 # entry point fino
├── remoteops/
│   ├── bootstrap.py        # QApplication + MainWindow
│   ├── paths.py            # caminhos portáteis (dev / exe)
│   ├── core/               # builder, executor, ConPTY, win_cmd
│   ├── services/           # execução, uninstall, RustDesk
│   ├── ui/                 # MainWindow, abas, widgets
│   └── utils/              # settings, hosts, catalog, redaction…
├── config/                 # ApplicationCatalog.json
├── assets/                 # ícones e marca
├── hosts.example.json      # versionado (fictício)
├── pyproject.toml
└── RemoteOps.spec          # PyInstaller
```
