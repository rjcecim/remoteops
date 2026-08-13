"""Geração do script PowerShell executado remotamente via PsExec.

Usamos placeholders ``@@NAME@@`` em vez de f-strings para evitar o problema
clássico de conflito de chaves ``{`` / ``}`` (f-strings exigem ``{{``/``}}``
para escapar, o que gera código PowerShell inválido na saída).
"""

from __future__ import annotations

import base64
from pathlib import Path

from .constants import EXEC_ACTIONS, PSEXEC_ACTION_TIMEOUT_S
from .winget_flags import (
    COMMON_EXEC_FLAGS,
    COMMON_QUERY_FLAGS,
    COMMON_UNINSTALL_FLAGS,
    COMMON_UPGRADE_ALL_FLAGS,
)

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _ps_single_quote(value: str) -> str:
    """Quota um valor como string literal do PowerShell."""
    return "'" + str(value).replace("'", "''") + "'"


def _ps_array(values: list[str]) -> str:
    return "@(" + ",".join(_ps_single_quote(v) for v in values) + ")"


def _load_conpty_cs() -> str:
    return (_TEMPLATES_DIR / "conpty_runner.cs").read_text(encoding="utf-8")


def _minify_cs(src: str) -> str:
    """Remove comentários // e linhas em branco do C# embutido."""
    lines: list[str] = []
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines) + "\n"


def _minify_ps(src: str) -> str:
    """Remove comentários de linha e vazios extras, preservando here-strings."""
    out: list[str] = []
    in_here = False
    here_close = ""
    prev_blank = False
    for line in src.splitlines():
        if in_here:
            out.append(line.rstrip())
            if line.strip().startswith(here_close):
                in_here = False
            continue
        stripped = line.strip()
        if stripped.endswith("@'"):
            in_here = True
            here_close = "'@"
            out.append(line.rstrip())
            prev_blank = False
            continue
        if stripped.endswith('@"'):
            in_here = True
            here_close = '"@'
            out.append(line.rstrip())
            prev_blank = False
            continue
        if not stripped:
            if not prev_blank:
                out.append("")
            prev_blank = True
            continue
        if stripped.startswith("#"):
            continue
        code = line.rstrip()
        if "'" not in code and '"' not in code:
            hash_pos = code.find(" #")
            if hash_pos >= 0:
                code = code[:hash_pos].rstrip()
        if not code:
            continue
        prev_blank = False
        out.append(code)
    return "\n".join(out).strip() + "\n"


def _conpty_init(include: bool) -> str:
    if not include:
        return "$script:ConPtyOk = $false\n"
    cs = _minify_cs(_load_conpty_cs())
    return (
        "$script:ConPtyOk = $false\n"
        "try {\n"
        "  Add-Type -TypeDefinition @'\n"
        f"{cs}"
        "'@ -ErrorAction Stop\n"
        "  $script:ConPtyOk = $true\n"
        "} catch {\n"
        "  $script:ConPtyOk = $false\n"
        "}\n"
    )


_PS_TEMPLATE = r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch {}
try { $OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}

@@CONPTY_INIT@@
$script:WingetCancelled = $false
$script:WingetTimedOut = $false
$script:WingetTimeoutS = 0
$script:WingetLastExitCode = 0

function Test-RemoteCancel {
  if (-not $cancelPath) { return $false }
  try { return [System.IO.File]::Exists($cancelPath) } catch { return $false }
}

function J($o) {
  $tmpFile = $null
  try {
    try {
      if ($null -ne $o.PSObject.Properties['Meta'] -and $null -ne $o.Meta) {
        $o.Meta | Add-Member -NotePropertyName Cancelled -NotePropertyValue ([bool]$script:WingetCancelled) -Force
        $o.Meta | Add-Member -NotePropertyName TimedOut -NotePropertyValue ([bool]$script:WingetTimedOut) -Force
      }
      $o | Add-Member -NotePropertyName Cancelled -NotePropertyValue ([bool]$script:WingetCancelled) -Force
      $o | Add-Member -NotePropertyName TimedOut -NotePropertyValue ([bool]$script:WingetTimedOut) -Force
      if ($script:WingetCancelled -or $script:WingetTimedOut) {
        $o | Add-Member -NotePropertyName Ok -NotePropertyValue $false -Force
        $hasErr = $false
        try { $hasErr = [bool]$o.Error } catch { $hasErr = $false }
        if (-not $hasErr) {
          $stopMsg = 'Execução remota excedeu o tempo limite.'
          if ($script:WingetCancelled) { $stopMsg = 'Operação cancelada pelo usuário.' }
          $o | Add-Member -NotePropertyName Error -NotePropertyValue $stopMsg -Force
        }
      }
    } catch {}
    $tmpFile = [System.IO.Path]::GetTempFileName()
    $json = $o | ConvertTo-Json -Depth 6
    if ($json -is [array]) { $json = ($json -join [Environment]::NewLine) }
    [System.IO.File]::WriteAllText($tmpFile, $json, [System.Text.UTF8Encoding]::new($false))

    if ($resultPath) {
      try {
        [System.IO.File]::WriteAllText($resultPath, $json, [System.Text.UTF8Encoding]::new($false))
        $fi = Get-Item -LiteralPath $resultPath -ErrorAction SilentlyContinue
        if ($fi) {
          [Console]::Error.WriteLine("__WINGETRM_FILE__ ok=1 bytes=$($fi.Length) path=$resultPath")
        } else {
          [Console]::Error.WriteLine("__WINGETRM_FILE__ ok=0 path=$resultPath")
        }
      } catch {
        [Console]::Error.WriteLine("__WINGETRM_FILE__ err=$($_.Exception.Message)")
      }
    }

    $bytes = [System.IO.File]::ReadAllBytes($tmpFile)
    $b64 = [Convert]::ToBase64String($bytes)
    $chunk = 512
    [Console]::Error.WriteLine("__WINGETRM_DBG__ json_len=$($json.Length) b64_len=$($b64.Length)")

    [Console]::Error.WriteLine('__WINGETRM_B64_BEGIN__')
    for ($i = 0; $i -lt $b64.Length; $i += $chunk) {
      $len = [Math]::Min($chunk, $b64.Length - $i)
      [Console]::Error.WriteLine($b64.Substring($i, $len))
    }
    [Console]::Error.WriteLine('__WINGETRM_B64_END__')

    [Console]::Out.WriteLine('__WINGETRM_B64_BEGIN__')
    for ($i = 0; $i -lt $b64.Length; $i += $chunk) {
      $len = [Math]::Min($chunk, $b64.Length - $i)
      [Console]::Out.WriteLine($b64.Substring($i, $len))
    }
    [Console]::Out.WriteLine('__WINGETRM_B64_END__')

    [Console]::Error.WriteLine('__WINGETRM_JSON_BEGIN__')
    foreach ($ln in ($json -split "`n")) { [Console]::Error.WriteLine($ln.TrimEnd("`r")) }
    [Console]::Error.WriteLine('__WINGETRM_JSON_END__')

    try { [Console]::Out.Flush() } catch {}
    try { [Console]::Error.Flush() } catch {}
    Start-Sleep -Milliseconds 200
  }
  finally {
    if ($tmpFile -and (Test-Path -LiteralPath $tmpFile)) {
      Remove-Item -LiteralPath $tmpFile -Force -ErrorAction SilentlyContinue
    }
  }
}

function Format-WingetOutput($lines) {
  if (-not $lines) { return '' }
  $spinners = @('-','\','|','/')
  $filtered = foreach ($ln in $lines) {
    $s = $ln.ToString().Trim()
    if ($spinners -contains $s) { continue }
    $s
  }
  $text = ($filtered -join [Environment]::NewLine).Trim()
  if ($text.Length -gt 262144) { $text = $text.Substring(0, 262144) }
  return $text
}

function Emit-RemoteLog([string]$Text) {
  if ($null -eq $Text) { return }
  $t = $Text.ToString()
  if ($logPath) {
    try {
      [System.IO.File]::AppendAllText(
        $logPath,
        $t + [Environment]::NewLine,
        [System.Text.UTF8Encoding]::new($false)
      )
    } catch {}
  } else {
    [Console]::Error.WriteLine('__WINGETRM_LOG__' + $t)
    try { [Console]::Error.Flush() } catch {}
  }
}

# NÃO usar $LASTEXITCODE após funções: no Windows PowerShell 5.1 ele frequentemente
# fica $null (e $null -ne 0 => "falha"), gerando resumo 0/N mesmo com sucesso real.
$script:WingetLastExitCode = 0

function Test-WingetSuccess([object]$Code) {
  if ($null -eq $Code) { return $false }
  try { $c = [int]$Code } catch { return $false }
  if ($c -eq 0) { return $true }
  # Soft-success do winget (AppInstallerErrors.h)
  if ($c -eq -1978334967) { return $true }  # REBOOT_REQUIRED_TO_FINISH
  if ($c -eq -1978334965) { return $true }  # REBOOT_INITIATED
  if ($c -eq -1978335189) { return $true }  # UPDATE_NOT_APPLICABLE
  return $false
}

function Test-RunStillSuccessful([bool]$OkSoFar) {
  if ($script:WingetCancelled -or $script:WingetTimedOut) { return $false }
  return [bool]$OkSoFar
}

function Invoke-WingetCapture {
  param(
    [Parameter(Mandatory)][string]$WingetPath,
    [Parameter(Mandatory)][string[]]$ArgumentList,
    [switch]$UseConPty
  )
  $buf = [System.Collections.Generic.List[string]]::new()
  $script:WingetLastExitCode = 0

  function Emit-WingetLine([string]$Text) {
    if ($null -eq $Text) { return }
    $t = $Text.ToString()
    if ($t.Length -eq 0) { return }
    Emit-RemoteLog $t
    [void]$buf.Add($t)
  }

  function Read-WingetStream($reader) {
    if ($null -eq $reader) { return }
    $sb = New-Object System.Text.StringBuilder
    while ($true) {
      $ch = $reader.Read()
      if ($ch -lt 0) { break }
      $c = [char]$ch
      if ($c -eq [char]13 -or $c -eq [char]10) {
        Emit-WingetLine $sb.ToString()
        [void]$sb.Clear()
        if ($c -eq [char]13) {
          try {
            if ($reader.Peek() -eq 10) { [void]$reader.Read() }
          } catch {}
        }
      } else {
        [void]$sb.Append($c)
      }
    }
    if ($sb.Length -gt 0) { Emit-WingetLine $sb.ToString() }
  }

  function Format-WingetArg([string]$Value) {
    if ($null -eq $Value) { return '""' }
    $s = [string]$Value
    if ($s -match '[\s"]') { return '"' + ($s -replace '"','\"') + '"' }
    return $s
  }

  $argStr = ($ArgumentList | ForEach-Object { Format-WingetArg $_ }) -join ' '

  function Stop-WingetProcessTree($Process) {
    if ($null -eq $Process) { return }
    try {
      $procId = [int]$Process.Id
      $tk = Join-Path $env:SystemRoot 'System32\taskkill.exe'
      Start-Process -FilePath $tk -ArgumentList @('/F','/T','/PID',"$procId") -WindowStyle Hidden -Wait -ErrorAction SilentlyContinue | Out-Null
    } catch {
      try { $Process.Kill() } catch {}
    }
  }

  # ConPTY só para execuções (install/upgrade/uninstall): faz o winget emitir o
  # progresso de download real. Em consultas (list/search) usamos o pipe, que
  # produz a tabela limpa que os parsers esperam (ConPTY reposiciona o cursor e
  # fragmenta as colunas, quebrando o parsing).
  if ($UseConPty -and $script:ConPtyOk) {
    $runner = $null
    $started = $false
    try {
      $runner = New-Object WingetRM.ConPtyRunner
      $cmdLine = "`"$WingetPath`" $argStr"
      $timeoutMs = 0
      if ($script:WingetTimeoutS -gt 0) {
        $timeoutMs = [int]([Math]::Min([double]$script:WingetTimeoutS * 1000.0, [int]::MaxValue))
      }
      $code = [int]$runner.Run($cmdLine, $logPath, [int16]200, [int16]50, $cancelPath, $timeoutMs)
      $started = [bool]$runner.ProcessStarted
      $script:WingetLastExitCode = $code
      if ($runner.Cancelled) { $script:WingetCancelled = $true }
      if ($runner.TimedOut) { $script:WingetTimedOut = $true }
      foreach ($ln in $runner.GetLines()) {
        if ($null -eq $ln -or $ln.Length -eq 0) { continue }
        $t = $ln.Trim()
        if ($t -match '^[\s\u2588\u2592\u2591\u2593]*\d{1,3}%$') { continue }
        if ($t -match '^[\s\u2588\u2592\u2591\u2593]*[\d\.,]+\s*(KB|MB|GB)\s*/\s*[\d\.,]+\s*(KB|MB|GB)$') { continue }
        [void]$buf.Add($ln)
      }
      return ,@($buf.ToArray())
    } catch {
      $started = $started -or ($null -ne $runner -and [bool]$runner.ProcessStarted)
      if ($started) {
        # CreateProcess já lançou o WinGet: nunca reexecutar por pipe.
        Emit-RemoteLog ("[wingetrm] ConPTY: falha após iniciar o WinGet (sem reexecução): " + $_.Exception.Message)
        if ($null -ne $runner) {
          if ($runner.Cancelled) { $script:WingetCancelled = $true }
          if ($runner.TimedOut) { $script:WingetTimedOut = $true }
          if ($script:WingetLastExitCode -eq 0 -and $runner.ExitCode -ne 0) {
            $script:WingetLastExitCode = [int]$runner.ExitCode
          }
          try {
            foreach ($ln in $runner.GetLines()) {
              if ($null -eq $ln -or $ln.Length -eq 0) { continue }
              [void]$buf.Add($ln)
            }
          } catch {}
        }
        if ($script:WingetLastExitCode -eq 0) { $script:WingetLastExitCode = 1 }
        return ,@($buf.ToArray())
      }
      Emit-RemoteLog ("[wingetrm] ConPTY indisponível, usando modo padrão: " + $_.Exception.Message)
    }
  }

  # Fallback: pipe redirecionado (sem % de download, mas robusto em qualquer host).
  # Lê bytes brutos e decodifica: o winget emite reticências/Unicode em UTF-8
  # (U+2026 = E2 80 A6). Com Encoding::Default (CP1252) isso vira "â€¦", o
  # parser de colunas desloca e o Id aparece como "€¦" com versões quebradas.
  $cmd = "`"$WingetPath`" $argStr 2>&1"

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $env:ComSpec
  $psi.Arguments = "/c $cmd"
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $false
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true

  $proc = New-Object System.Diagnostics.Process
  $proc.StartInfo = $psi
  [void]$proc.Start()
  $ms = New-Object System.IO.MemoryStream
  $stream = $proc.StandardOutput.BaseStream
  $readBuf = New-Object byte[] 4096
  $sw = [System.Diagnostics.Stopwatch]::StartNew()
  try {
    while ($true) {
      if (Test-RemoteCancel) {
        $script:WingetCancelled = $true
        Stop-WingetProcessTree $proc
        break
      }
      if ($script:WingetTimeoutS -gt 0 -and $sw.Elapsed.TotalSeconds -ge $script:WingetTimeoutS) {
        $script:WingetTimedOut = $true
        Stop-WingetProcessTree $proc
        break
      }
      $iar = $null
      try {
        $iar = $stream.BeginRead($readBuf, 0, $readBuf.Length, $null, $null)
      } catch { break }
      while (-not $iar.IsCompleted) {
        if (Test-RemoteCancel) {
          $script:WingetCancelled = $true
          Stop-WingetProcessTree $proc
          break
        }
        if ($script:WingetTimeoutS -gt 0 -and $sw.Elapsed.TotalSeconds -ge $script:WingetTimeoutS) {
          $script:WingetTimedOut = $true
          Stop-WingetProcessTree $proc
          break
        }
        [void]$iar.AsyncWaitHandle.WaitOne(250)
      }
      $n = 0
      try { $n = $stream.EndRead($iar) } catch { break }
      if ($script:WingetCancelled -or $script:WingetTimedOut) { break }
      if ($n -le 0) { break }
      [void]$ms.Write($readBuf, 0, $n)
    }
  } finally {
    try { [void]$proc.WaitForExit(15000) } catch {}
    if (-not $proc.HasExited) {
      try { Stop-WingetProcessTree $proc } catch {}
    }
  }
  try { $script:WingetLastExitCode = [int]$proc.ExitCode } catch { $script:WingetLastExitCode = 1 }

  $bytes = $ms.ToArray()
  $text = ''
  if ($bytes -and $bytes.Length -gt 0) {
    $utf8Strict = [System.Text.UTF8Encoding]::new($false, $true)
    try {
      $text = $utf8Strict.GetString($bytes)
    } catch {
      $utf8Loose = [System.Text.UTF8Encoding]::new($false, $false).GetString($bytes)
      $ansi = [System.Text.Encoding]::Default.GetString($bytes)
      $fffd = ([regex]::Matches($utf8Loose, [string][char]0xFFFD)).Count
      if ($fffd -gt 0) { $text = $ansi } else { $text = $utf8Loose }
    }
  }
  foreach ($ln in ($text -split "`r`n|`n|`r")) {
    Emit-WingetLine $ln
  }
  return ,@($buf.ToArray())
}

function Invoke-WingetIdsAction {
  param(
    [Parameter(Mandatory)][string]$Verb,
    [Parameter(Mandatory)][string[]]$CommonFlags,
    [Parameter(Mandatory)][string[]]$IdList
  )
  $res = New-Object System.Collections.Generic.List[object]
  foreach ($id in $IdList) {
    if ($script:WingetCancelled -or $script:WingetTimedOut) {
      Emit-RemoteLog '[wingetrm] Cancelamento/timeout: demais IDs não serão executados.'
      break
    }
    if (Test-RemoteCancel) {
      $script:WingetCancelled = $true
      Emit-RemoteLog '[wingetrm] Cancelamento/timeout: demais IDs não serão executados.'
      break
    }
    Emit-RemoteLog ''
    Emit-RemoteLog "--- $id ---"
    $out = Invoke-WingetCapture -WingetPath $winget -ArgumentList (@($Verb, '--id', $id) + $CommonFlags) -UseConPty
    $code = [int]$script:WingetLastExitCode
    $res.Add([pscustomobject]@{ Id=$id; ExitCode=$code; Output=(Format-WingetOutput $out) }) | Out-Null
  }
  return ,@($res.ToArray())
}

$commonQuery     = @@COMMON_QUERY@@
$commonExec      = @@COMMON_EXEC@@
$commonUpgradeAll = @@COMMON_UPGRADE_ALL@@
$commonUninstall = @@COMMON_UNINSTALL@@
$ids             = @@IDS@@
$query           = @@QUERY@@
$resultPath      = @@RESULT_PATH@@
$logPath         = @@LOG_PATH@@
$cancelPath      = @@CANCEL_PATH@@
$action          = @@ACTION@@
$script:WingetTimeoutS = @@TIMEOUT_S@@
$script:WingetCancelled = $false
$script:WingetTimedOut = $false

$meta = [pscustomobject]@{
  Timestamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
  Computer  = $env:COMPUTERNAME
  User      = $env:USERNAME
  Winget    = $null
  PSVersion = $PSVersionTable.PSVersion.ToString()
  ConPty    = [bool]$script:ConPtyOk
}

$winget = (Get-Command winget.exe -ErrorAction SilentlyContinue).Path
if (-not $winget) {
  $winget = (Get-Item "$env:ProgramFiles\WindowsApps\Microsoft.DesktopAppInstaller_*_x64__8wekyb3d8bbwe\winget.exe" -ErrorAction SilentlyContinue | Select-Object -First 1).FullName
}
$meta.Winget = $winget
if (-not $winget) {
  J([pscustomobject]@{ Ok=$false; Action=$action; Meta=$meta; Error='winget.exe não encontrado no host remoto.' })
  exit 2
}

try {
  switch ($action) {
    'list' {
      $txt = Invoke-WingetCapture -WingetPath $winget -ArgumentList (@('upgrade') + $commonQuery)
      J([pscustomobject]@{ Ok=$true; Action='list'; Meta=$meta; Text=$txt })
      exit 0
    }
    'search' {
      if (-not $query -or -not $query.Trim()) {
        J([pscustomobject]@{ Ok=$false; Action='search'; Meta=$meta; Error='Informe um termo para busca.' })
        exit 6
      }
      $txt = Invoke-WingetCapture -WingetPath $winget -ArgumentList (@('search', $query) + $commonQuery)
      J([pscustomobject]@{ Ok=$true; Action='search'; Meta=$meta; Query=$query; Text=$txt })
      exit 0
    }
    'installed' {
      $txt = Invoke-WingetCapture -WingetPath $winget -ArgumentList (@('list') + $commonQuery)
      J([pscustomobject]@{ Ok=$true; Action='installed'; Meta=$meta; Text=$txt })
      exit 0
    }
    'upgrade_all' {
      $out = Invoke-WingetCapture -WingetPath $winget -ArgumentList (@('upgrade', '--all') + $commonUpgradeAll) -UseConPty
      $code = [int]$script:WingetLastExitCode
      $ok = Test-RunStillSuccessful (Test-WingetSuccess $code)
      J([pscustomobject]@{
        Ok      = [bool]$ok
        Action  = 'upgrade_all'
        Meta    = $meta
        Results = @([pscustomobject]@{ Id='--all'; ExitCode=$code; Output=(Format-WingetOutput $out) })
      })
      if ($ok) { exit 0 } else { exit 11 }
    }
    'upgrade' {
      if (-not $ids -or $ids.Count -eq 0) {
        J([pscustomobject]@{ Ok=$false; Action='upgrade'; Meta=$meta; Error='Nenhum ID informado para atualização.' })
        exit 9
      }
      $arr = Invoke-WingetIdsAction -Verb 'upgrade' -CommonFlags $commonExec -IdList $ids
      $allOk = Test-RunStillSuccessful (-not ($arr | Where-Object { -not (Test-WingetSuccess $_.ExitCode) } | Select-Object -First 1))
      J([pscustomobject]@{ Ok=[bool]$allOk; Action='upgrade'; Meta=$meta; Results=@($arr) })
      if ($allOk) { exit 0 } else { exit 10 }
    }
    'install' {
      if (-not $ids -or $ids.Count -eq 0) {
        J([pscustomobject]@{ Ok=$false; Action='install'; Meta=$meta; Error='Nenhum ID informado para instalação.' })
        exit 3
      }
      $arr = Invoke-WingetIdsAction -Verb 'install' -CommonFlags $commonExec -IdList $ids
      $allOk = Test-RunStillSuccessful (-not ($arr | Where-Object { -not (Test-WingetSuccess $_.ExitCode) } | Select-Object -First 1))
      J([pscustomobject]@{ Ok=[bool]$allOk; Action='install'; Meta=$meta; Results=@($arr) })
      if ($allOk) { exit 0 } else { exit 4 }
    }
    'uninstall' {
      if (-not $ids -or $ids.Count -eq 0) {
        J([pscustomobject]@{ Ok=$false; Action='uninstall'; Meta=$meta; Error='Nenhum ID informado para desinstalação.' })
        exit 7
      }
      $arr = Invoke-WingetIdsAction -Verb 'uninstall' -CommonFlags $commonUninstall -IdList $ids
      $allOk = Test-RunStillSuccessful (-not ($arr | Where-Object { -not (Test-WingetSuccess $_.ExitCode) } | Select-Object -First 1))
      J([pscustomobject]@{ Ok=[bool]$allOk; Action='uninstall'; Meta=$meta; Results=@($arr) })
      if ($allOk) { exit 0 } else { exit 8 }
    }
    default {
      J([pscustomobject]@{ Ok=$false; Action=$action; Meta=$meta; Error='Ação inválida.' })
      exit 5
    }
  }
}
catch {
  $msg = $_.Exception.Message
  if (-not $msg) { $msg = $_.ToString() }
  J([pscustomobject]@{ Ok=$false; Action=$action; Meta=$meta; Error=$msg })
  exit 1
}
""".lstrip("\n")


def build_remote_script(
    *,
    action: str,
    ids: list[str],
    query: str,
    result_path: str,
    log_path: str,
    cancel_path: str = "",
    timeout_s: int = PSEXEC_ACTION_TIMEOUT_S,
) -> str:
    """Constrói o script PowerShell final (string)."""
    include_conpty = (action or "").lower() in EXEC_ACTIONS
    replacements = {
        "@@CONPTY_INIT@@": _conpty_init(include_conpty),
        "@@COMMON_QUERY@@": _ps_array(COMMON_QUERY_FLAGS),
        "@@COMMON_EXEC@@": _ps_array(COMMON_EXEC_FLAGS),
        "@@COMMON_UPGRADE_ALL@@": _ps_array(COMMON_UPGRADE_ALL_FLAGS),
        "@@COMMON_UNINSTALL@@": _ps_array(COMMON_UNINSTALL_FLAGS),
        "@@IDS@@": _ps_array(list(ids or [])),
        "@@QUERY@@": _ps_single_quote(query or ""),
        "@@RESULT_PATH@@": _ps_single_quote(result_path or ""),
        "@@LOG_PATH@@": _ps_single_quote(log_path or ""),
        "@@CANCEL_PATH@@": _ps_single_quote(cancel_path or ""),
        "@@TIMEOUT_S@@": str(int(timeout_s)),
        "@@ACTION@@": _ps_single_quote((action or "").lower()),
    }
    out = _PS_TEMPLATE
    for k, v in replacements.items():
        out = out.replace(k, v)
    return _minify_ps(out)


_BOOTSTRAP_TEMPLATE = (
    "$ErrorActionPreference='Stop';"
    "$gz='@@GZ@@';"
    "$data=[Convert]::FromBase64String($gz);"
    "$ms=New-Object System.IO.MemoryStream(,$data);"
    "$gzs=New-Object System.IO.Compression.GzipStream($ms,[System.IO.Compression.CompressionMode]::Decompress);"
    "$sr=New-Object System.IO.StreamReader($gzs,[System.Text.UTF8Encoding]::new($false));"
    "$code=$sr.ReadToEnd();"
    "$sr.Close();"
    "Invoke-Expression $code"
)


def build_bootstrap_script(inner_script: str) -> str:
    """Envolve o script real num bootstrap gzip de uma linha (para ``-Command``).

    ``-EncodedCommand`` reencodeia em UTF-16LE+Base64 e estoura o limite de
    32767 caracteres do CreateProcess (WinError 206). O bootstrap ASCII gzip
    cabe na linha de comando sem copiar arquivo para o host.
    """
    import gzip

    gz = base64.b64encode(gzip.compress(inner_script.encode("utf-8"))).decode("ascii")
    return _BOOTSTRAP_TEMPLATE.replace("@@GZ@@", gz)


def encode_script_base64(script: str) -> str:
    """Codifica em UTF-16LE + Base64 para uso com ``powershell -EncodedCommand``."""
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")
