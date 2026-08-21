<p align="center">
  <img src="https://img.shields.io/badge/version-2.0.0-0063C4?style=flat-square" alt="Version" />
  <img src="https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/PyQt6-GUI-41CD52?style=flat-square&logo=qt&logoColor=white" alt="PyQt6" />
  <img src="https://img.shields.io/badge/Windows-10%20%7C%2011-0078D6?style=flat-square&logo=windows&logoColor=white" alt="Windows" />
</p>

# RemoteOps 2.0.0

> Operações remotas no Windows via **PsExec**: instaladores, scripts, WinGet, inventário e desinstalação — interface Fluent, preview em tempo real e empacotamento portátil.

<p align="center">
  <img src="assets/app_icon.png" alt="RemoteOps" width="96" />
</p>

O comando executado é o que está selecionado na aba **PsExec**. A senha nunca aparece no preview nem nos logs (`-p ********`).

Versão: **`2.0.0`** (`remoteops.core.version.__version__`).

---

## Índice

- [Visão geral](#visão-geral)
- [Abas](#abas)
- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Uso rápido](#uso-rápido)
- [Arquivo EXE](#arquivo-exe)
- [Segurança de credenciais](#segurança-de-credenciais)
- [hosts.json e faixa de IP](#hostsjson-e-faixa-de-ip)
- [Logging](#logging)
- [Build](#build)
- [Estrutura do projeto](#estrutura-do-projeto)

---

## Visão geral

| Recurso | Descrição |
|--------|-----------|
| **Arquivos** | `.exe`, `.msi`, `.ps1`, `.bat` — arquivo ou pasta no seletor do cabeçalho |
| **Abas dinâmicas** | MSI, PowerShell, CMD, Robocopy e Instalação em Lote só aparecem quando o tipo de arquivo pede |
| **PsExec** | Fonte de verdade das flags (`-s`, `-c`, `-f`, `-accepteula`…). O preview e a execução leem a UI |
| **Host** | Status Online/Offline; **Executar** só fica disponível com o host online |
| **Cópia** | Robocopy para `.msi`/`.ps1`/`.bat`/pasta; `.exe` usa a cópia do próprio PsExec (`-c`) |
| **Lote** | Instala o EXE selecionado em vários hosts (faixa de IP ou `hosts.json`) |
| **WinGet** | Listar, buscar, instalar, atualizar e desinstalar pacotes no host remoto |
| **Inventário** | PsInfo (sistema, hotfix, discos) e aplicativos via Remote Registry |
| **Pesquisa** | Aplicativos em vários hosts, com desinstalação quando houver `UninstallString` |
| **RustDesk** | Coleta o ID no remoto e abre `rustdesk.exe --connect <ID>` localmente |
| **UI** | Fluent / PyQt6, tooltips em card, tabelas em uma linha com elipse |
| **Portátil** | `settings.ini`, `hosts.json` e `logs/` ao lado do exe (ou na raiz do repo em dev) |

---

## Abas

Aba **PsExec** está sempre visível. As demais abrem sob demanda.

| Aba | Quando aparece | Função |
|-----|----------------|--------|
| **PsExec** | Sempre | Host, autenticação, privilégios, flags e args do programa remoto |
| **Instalação em Lote** | Arquivo `.exe` | Varre a rede ou `hosts.json` e instala o EXE (versão desejada opcional) |
| **MSI** | Arquivo `.msi` | Ação, interface, reinício e propriedades do `msiexec` |
| **PowerShell** | `.ps1` ou comando `powershell` | `-Command`, `-File`, `-EncodedCommand`, política de execução |
| **CMD** | `.bat` ou comando `cmd` | `/C` ou `/K` (checkboxes exclusivos), `/D`, `/Q` e cadeia |
| **Robocopy** | Arquivo/pasta que não seja `.exe` | Destino em `C$` e switches da cópia |
| **PsInfo** | Botão na aba PsExec | Inventário remoto (host preenchido) |
| **Aplicativos** | Botão na aba PsExec | Lista instalados no host; desinstalação |
| **WinGet** | Botão na aba PsExec | `winget` remoto (host preenchido) |
| **Pesquisa de Aplicativos** | Ícone de busca no cabeçalho | Multi-host (faixa de IP ou `hosts.json`) |
| **Configurações** | Ícone de engrenagem no cabeçalho | Pasta PSTools, RustDesk, logs, Remote Registry, faixa de IP |

---

## Requisitos

| Item | Observação |
|------|------------|
| **Sistema** | Windows 10 ou 11 |
| **Python** | 3.10+ (desenvolvimento) |
| **PyQt6** | Interface |
| **PSTools** | Pasta com PsExec e PsInfo — padrão `C:\PSTools\` (ajustável em Configurações) |
| **WinGet** | No host remoto, para a aba WinGet |
| **RustDesk** | Opcional, no host e na máquina local |
| **Rede** | Ping, SMB (`C$`) e Remote Registry conforme o fluxo |

---

## Instalação

```bash
cd RemoteOps
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
# opcional — gerar ícones / empacotar:
pip install -e ".[build]"
```

Runtime mínimo:

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

1. Informe o **host remoto** na aba PsExec e aguarde o status **Online**.
2. Autenticação só se a sessão atual não bastar (`-u` / `-p`).
3. Selecione um arquivo no cabeçalho, **ou** digite o comando remoto (ex.: `cmd`, `powershell`).
4. Ajuste as abas que surgirem e as flags do PsExec.
5. Confira o **preview** e clique em **Executar**. A saída vai para o console (ConPTY).

Duplo clique em uma célula de tabela copia o texto visível.

---

## Arquivo EXE

Ao escolher um `.exe`:

1. A aba **Instalação em Lote** aparece.
2. Na aba PsExec são **pré-marcados** `-accepteula`, `-nobanner`, `-s`, `-c` e `-f`.
3. Depois disso você pode marcar ou desmarcar qualquer opção; o comando segue a UI.
4. Não há lista oculta de flags de instalador — lote e execução única usam o estado atual do PsExec (o lote só troca o host de cada alvo).

Argumentos do instalador (`/S`, `/quiet`, …) vão em **Args do programa** na aba PsExec.

---

## Segurança de credenciais

Política em `remoteops.utils.redaction`:

- Preview, logs e arquivos usam texto sanitizado (`-p` isolada; não mascara `-Path` / `-Profile` / `-Priority`).
- O builder **não** guarda a senha — só sabe se há senha e mostra `-p ********`.
- A senha é lida na UI na hora de executar (`CredentialContext`), injetada no argv e descartada em seguida.
- Prefira a sessão Windows atual (sem `-u`/`-p`) quando possível.

### Limitações

- Com `-u`/`-p`, a senha fica na command line do processo (limitação do PsExec).
- Strings Python não são zeroizadas criptograficamente.

---

## hosts.json e faixa de IP

**Lista de hosts** (Pesquisa e Lote, quando a faixa de IP está desligada):

1. Copie `hosts.example.json` → `hosts.json` (raiz do repo ou pasta do exe).
2. Edite com os nomes do seu ambiente.

`hosts.json` está no `.gitignore`. `hosts.example.json` permanece versionado (hosts fictícios).

**Faixa de IP** (Configurações): início/fim IPv4, octetos ignorados e threads de varredura. Com a faixa ativa, Lote e Pesquisa varrem a rede em vez de usar `hosts.json`.

---

## Logging

Preferência **Salvar log em arquivo** em Configurações:

- Pasta `logs\` na raiz do repo (dev) ou ao lado do `RemoteOps.exe`
- Arquivo: `logs\app.log`
- Demais preferências em `settings.ini` (local, não versionado): pasta PSTools, workers, timeout do Remote Registry, faixa de IP

---

## Build

```bash
pip install -e ".[build]"
python -m PyInstaller --noconfirm --clean RemoteOps.spec
```

Gera `dist/RemoteOps.exe` (sem console) e copia `dist/config/` (`ApplicationCatalog.json`). Assets e templates WinGet entram no exe. **Não** empacota `hosts.json`, `settings.ini`, credenciais nem logs.

---

## Estrutura do projeto

```
RemoteOps/
├── main.py                      # entry point
├── remoteops/
│   ├── bootstrap.py             # QApplication, Fluent, MainWindow
│   ├── paths.py                 # caminhos portáteis (dev / exe)
│   ├── core/                    # builder, executor, ConPTY, opções PsExec/CMD/PS
│   ├── services/                # execução, lote, uninstall, RustDesk
│   ├── ui/                      # janela, abas, widgets, estilo Fluent
│   ├── utils/                   # settings, hosts, catálogo, rede, redação
│   └── winget/                  # execução remota do winget
├── config/                      # ApplicationCatalog.json
├── assets/                      # ícones e marca
├── hosts.example.json
├── pyproject.toml
└── RemoteOps.spec               # PyInstaller → dist/RemoteOps.exe
```
