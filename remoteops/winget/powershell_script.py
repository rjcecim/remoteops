"""Geração do script PowerShell executado remotamente via PsExec.

Usamos placeholders ``@@NAME@@`` em vez de f-strings para evitar o problema
clássico de conflito de chaves ``{`` / ``}`` (f-strings exigem ``{{``/``}}``
para escapar, o que gera código PowerShell inválido na saída).
"""

from __future__ import annotations

import base64
from pathlib import Path

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


_PS_TEMPLATE = r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch {}
try { $OutputEncoding = [System.Text.UTF8Encoding]::new($false) } catch {}

# ConPTY: winget emite progresso real quando "enxerga" um terminal.
$script:ConPtyOk = $false
try {
  Add-Type -TypeDefinition @'
@@CONPTY_CS@@
'@ -ErrorAction Stop
  $script:ConPtyOk = $true
} catch {
  $script:ConPtyOk = $false
}

function J($o) {
  $tmpFile = $null
  try {
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

  # ConPTY só para execuções (install/upgrade/uninstall): faz o winget emitir o
  # progresso de download real. Em consultas (list/search) usamos o pipe, que
  # produz a tabela limpa que os parsers esperam (ConPTY reposiciona o cursor e
  # fragmenta as colunas, quebrando o parsing).
  if ($UseConPty -and $script:ConPtyOk) {
    try {
      $runner = New-Object WingetRM.ConPtyRunner
      $cmdLine = "`"$WingetPath`" $argStr"
      $code = [int]$runner.Run($cmdLine, $logPath, [int16]200, [int16]50)
      $script:WingetLastExitCode = $code
      # As linhas de progresso já foram para o log (tail em tempo real); aqui no
      # buffer/JSON final guardamos só as linhas úteis (sem barras de download).
      foreach ($ln in $runner.Lines) {
        if ($null -eq $ln -or $ln.Length -eq 0) { continue }
        $t = $ln.Trim()
        if ($t -match '^[\s\u2588\u2592\u2591\u2593]*\d{1,3}%$') { continue }
        if ($t -match '^[\s\u2588\u2592\u2591\u2593]*[\d\.,]+\s*(KB|MB|GB)\s*/\s*[\d\.,]+\s*(KB|MB|GB)$') { continue }
        [void]$buf.Add($ln)
      }
      return ,@($buf.ToArray())
    } catch {
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
  $proc.StandardOutput.BaseStream.CopyTo($ms)
  $proc.WaitForExit()
  $script:WingetLastExitCode = [int]$proc.ExitCode

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
$action          = @@ACTION@@

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
      $ok = Test-WingetSuccess $code
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
      $allOk = -not ($arr | Where-Object { -not (Test-WingetSuccess $_.ExitCode) } | Select-Object -First 1)
      J([pscustomobject]@{ Ok=[bool]$allOk; Action='upgrade'; Meta=$meta; Results=@($arr) })
      if ($allOk) { exit 0 } else { exit 10 }
    }
    'install' {
      if (-not $ids -or $ids.Count -eq 0) {
        J([pscustomobject]@{ Ok=$false; Action='install'; Meta=$meta; Error='Nenhum ID informado para instalação.' })
        exit 3
      }
      $arr = Invoke-WingetIdsAction -Verb 'install' -CommonFlags $commonExec -IdList $ids
      $allOk = -not ($arr | Where-Object { -not (Test-WingetSuccess $_.ExitCode) } | Select-Object -First 1)
      J([pscustomobject]@{ Ok=[bool]$allOk; Action='install'; Meta=$meta; Results=@($arr) })
      if ($allOk) { exit 0 } else { exit 4 }
    }
    'uninstall' {
      if (-not $ids -or $ids.Count -eq 0) {
        J([pscustomobject]@{ Ok=$false; Action='uninstall'; Meta=$meta; Error='Nenhum ID informado para desinstalação.' })
        exit 7
      }
      $arr = Invoke-WingetIdsAction -Verb 'uninstall' -CommonFlags $commonUninstall -IdList $ids
      $allOk = -not ($arr | Where-Object { -not (Test-WingetSuccess $_.ExitCode) } | Select-Object -First 1)
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
) -> str:
    """Constrói o script PowerShell final (string)."""
    replacements = {
        "@@CONPTY_CS@@": _load_conpty_cs(),
        "@@COMMON_QUERY@@": _ps_array(COMMON_QUERY_FLAGS),
        "@@COMMON_EXEC@@": _ps_array(COMMON_EXEC_FLAGS),
        "@@COMMON_UPGRADE_ALL@@": _ps_array(COMMON_UPGRADE_ALL_FLAGS),
        "@@COMMON_UNINSTALL@@": _ps_array(COMMON_UNINSTALL_FLAGS),
        "@@IDS@@": _ps_array(list(ids or [])),
        "@@QUERY@@": _ps_single_quote(query or ""),
        "@@RESULT_PATH@@": _ps_single_quote(result_path or ""),
        "@@LOG_PATH@@": _ps_single_quote(log_path or ""),
        "@@ACTION@@": _ps_single_quote((action or "").lower()),
    }
    out = _PS_TEMPLATE
    for k, v in replacements.items():
        out = out.replace(k, v)
    return out


_BOOTSTRAP_TEMPLATE = (
    "$ErrorActionPreference='Stop'\n"
    "$gz='@@GZ@@'\n"
    "$data=[Convert]::FromBase64String($gz)\n"
    "$ms=New-Object System.IO.MemoryStream(,$data)\n"
    "$gzs=New-Object System.IO.Compression.GzipStream($ms,[System.IO.Compression.CompressionMode]::Decompress)\n"
    "$sr=New-Object System.IO.StreamReader($gzs,[System.Text.UTF8Encoding]::new($false))\n"
    "$code=$sr.ReadToEnd()\n"
    "$sr.Close()\n"
    "Invoke-Expression $code\n"
)


def build_bootstrap_script(inner_script: str) -> str:
    """Envolve o script real num bootstrap que o descomprime no host.

    O script com ConPTY é grande; codificado direto em ``-EncodedCommand`` ele
    estoura o limite da linha de comando do Windows (~32 KB → WinError 206).
    Comprimir com gzip reduz ~7x e mantém tudo em um único comando.
    """
    import gzip

    gz = base64.b64encode(gzip.compress(inner_script.encode("utf-8"))).decode("ascii")
    return _BOOTSTRAP_TEMPLATE.replace("@@GZ@@", gz)


def encode_script_base64(script: str) -> str:
    """Codifica em UTF-16LE + Base64 para uso com ``powershell -EncodedCommand``."""
    return base64.b64encode(script.encode("utf-16le")).decode("ascii")
