# Documentação técnica — RemoteOps 2.0.0

**Versão:** `remoteops.core.version.__version__`  
**Nome exibido:** RemoteOps — Operações remotas (`APP_DISPLAY_NAME`)  
**Pacote:** `remoteops` (layout moderno sob a raiz do repositório)

Este projeto é a evolução limpa do PSExecGUI, com a mesma arquitetura lógica e divisão mais clara de responsabilidades.

---

## Arquitetura

```
UI (MainWindow, tabs, widgets)
        ↓
services (casos de uso)
        ↓
core (CommandSpec, Builder, Executor, ConPTY, win_cmd)
        ↓
utils (redaction, settings, hosts, catalog, psinfo, …)
```

| Camada | Papel |
|--------|--------|
| `remoteops.bootstrap` | Entry Qt limpo (`run()`) |
| `remoteops.ui.main_window` | Orquestração da UI |
| `remoteops.services.ops` | Execução, uninstall, RustDesk |
| `remoteops.core.*` | Domínio + infraestrutura de processo |
| `remoteops.utils.*` | Config, inventário, segurança de logs |
| `remoteops.paths` | Caminhos portáteis (dev / exe) |

---

## Segurança de credenciais

Módulo central: `remoteops.utils.redaction`.

- Preview e builder **não** guardam senha bruta
- Materialização só na execução (`CredentialContext`)
- Log UI / arquivos passam por redação
- Desinstalação PsInfo/Pesquisa: terminal externo (sem ConPTY)

---

## Features preservadas

PsExec, MSI, Robocopy, PowerShell, CMD, PsInfo, Pesquisa multi-host, Configurações, RustDesk, ConPTY no Executar, catálogo de uninstall, hosts.json.

---

## Empacotamento

`RemoteOps.spec` → `dist/RemoteOps.exe` (sem console), com `assets/`, `config/`, `hosts.example.json`.
