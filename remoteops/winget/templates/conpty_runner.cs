using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using Microsoft.Win32.SafeHandles;

namespace WingetRM {
  public class ConPtyRunner {
    const int EXTENDED_STARTUPINFO_PRESENT = 0x00080000;
    const int STARTF_USESTDHANDLES = 0x00000100;
    static readonly IntPtr PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = (IntPtr)0x00020016;

    [StructLayout(LayoutKind.Sequential)]
    struct COORD { public short X; public short Y; }

    [StructLayout(LayoutKind.Sequential)]
    struct STARTUPINFO {
      public int cb;
      public string lpReserved;
      public string lpDesktop;
      public string lpTitle;
      public int dwX; public int dwY; public int dwXSize; public int dwYSize;
      public int dwXCountChars; public int dwYCountChars; public int dwFillAttribute;
      public int dwFlags; public short wShowWindow; public short cbReserved2;
      public IntPtr lpReserved2; public IntPtr hStdInput; public IntPtr hStdOutput; public IntPtr hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct STARTUPINFOEX { public STARTUPINFO StartupInfo; public IntPtr lpAttributeList; }

    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess; public IntPtr hThread; public int dwProcessId; public int dwThreadId; }

    [DllImport("kernel32.dll", SetLastError=true)]
    static extern int CreatePseudoConsole(COORD size, SafeFileHandle hInput, SafeFileHandle hOutput, uint dwFlags, out IntPtr phPC);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern void ClosePseudoConsole(IntPtr hPC);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CreatePipe(out SafeFileHandle hReadPipe, out SafeFileHandle hWritePipe, IntPtr lpPipeAttributes, int nSize);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool InitializeProcThreadAttributeList(IntPtr lpAttributeList, int dwAttributeCount, int dwFlags, ref IntPtr lpSize);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool UpdateProcThreadAttribute(IntPtr lpAttributeList, uint dwFlags, IntPtr Attribute, IntPtr lpValue, IntPtr cbSize, IntPtr lpPreviousValue, IntPtr lpReturnSize);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern void DeleteProcThreadAttributeList(IntPtr lpAttributeList);
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    static extern bool CreateProcess(string lpApplicationName, string lpCommandLine, IntPtr lpProcessAttributes, IntPtr lpThreadAttributes, bool bInheritHandles, uint dwCreationFlags, IntPtr lpEnvironment, string lpCurrentDirectory, ref STARTUPINFOEX lpStartupInfo, out PROCESS_INFORMATION lpProcessInformation);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetExitCodeProcess(IntPtr hProcess, out uint lpExitCode);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CloseHandle(IntPtr hObject);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern IntPtr GetStdHandle(int nStdHandle);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool GetHandleInformation(IntPtr hObject, out uint lpdwFlags);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetHandleInformation(IntPtr hObject, uint dwMask, uint dwFlags);

    const int STD_INPUT_HANDLE = -10;
    const int STD_OUTPUT_HANDLE = -11;
    const int STD_ERROR_HANDLE = -12;
    const uint HANDLE_FLAG_INHERIT = 0x00000001;

    // Impede que o conhost.exe criado por CreatePseudoConsole herde os handles
    // padrão do processo pai (sob PsExec, esses handles são os pipes do PSEXESVC).
    // Sem isso, o conhost herda o pipe do PsExec e, ao ser encerrado por
    // ClosePseudoConsole, quebra a comunicação do PsExec (ERROR_INVALID_HANDLE=6).
    // Retorna os flags originais para posterior restauração.
    static uint[] ClearStdHandleInherit() {
      int[] ids = new int[] { STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE };
      uint[] saved = new uint[] { uint.MaxValue, uint.MaxValue, uint.MaxValue };
      for (int i = 0; i < ids.Length; i++) {
        try {
          IntPtr h = GetStdHandle(ids[i]);
          if (h == IntPtr.Zero || h == new IntPtr(-1)) continue;
          uint flags;
          if (GetHandleInformation(h, out flags)) {
            saved[i] = flags;
            SetHandleInformation(h, HANDLE_FLAG_INHERIT, 0);
          }
        } catch {}
      }
      return saved;
    }

    static void RestoreStdHandleInherit(uint[] saved) {
      if (saved == null) return;
      int[] ids = new int[] { STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE };
      for (int i = 0; i < ids.Length && i < saved.Length; i++) {
        if (saved[i] == uint.MaxValue) continue;
        try {
          IntPtr h = GetStdHandle(ids[i]);
          if (h == IntPtr.Zero || h == new IntPtr(-1)) continue;
          SetHandleInformation(h, HANDLE_FLAG_INHERIT, saved[i] & HANDLE_FLAG_INHERIT);
        } catch {}
      }
    }

    public List<string> Lines = new List<string>();
    public int ExitCode = 0;

    string _logPath;
    int _lastPct = -1;
    StringBuilder _pending = new StringBuilder();
    static readonly Regex EraseRe = new Regex("\u001b\\[[0-9]*[JK]|\u001b\\[[0-9]+G|\u001b\\[[0-9]+;[0-9]+H|\u001b\\[H");
    static readonly Regex CsiRe = new Regex("\u001b\\[[0-9;?]*[ -/]*[@-~]");
    static readonly Regex OscRe = new Regex("\u001b\\][^\u0007\u001b]*(\u0007|\u001b\\\\)");
    static readonly Regex OtherEscRe = new Regex("\u001b[@-Z\\\\-_=>]");
    static readonly Regex CtrlRe = new Regex("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]");
    // OSC 9;4 = protocolo de barra de progresso (ConEmu/Windows Terminal):
    // ESC ] 9 ; 4 ; <estado> ; <pct> (BEL | ST). estado 1=normal, 2=erro, 3=indeterminado.
    // O winget emite a % EXATA do download por aqui -> fonte mais confiável que o texto.
    static readonly Regex Osc94Re = new Regex("\u001b\\]9;4;([0-9]);([0-9]+)(?:\u0007|\u001b\\\\)");
    // Linhas puramente visuais do progresso (barra de blocos, spinner, "X MB / Y MB",
    // ou só "NN%"): não vão para o log nem para o JSON — a % real vai pelo marcador.
    // Inclui mojibake típico de UTF-8 lido como Latin-1 (â–ˆ) e U+FFFD.
    static readonly Regex VisualNoiseRe = new Regex(
      "^[\\s\\-\\\\|/\u2500-\u257f\u2580-\u259f\u25a0-\u25ff]*$" +
      "|[0-9][0-9.,]*\\s*(KB|MB|GB|TB|B)\\s*/\\s*[0-9][0-9.,]*\\s*(KB|MB|GB|TB|B)" +
      "|^[\\s\u2500-\u259f]*[0-9]{1,3}%$" +
      "|â[\u0080-\u00ff]{1,3}|Ã[\u0080-\u00bf]|\ufffd",
      RegexOptions.IgnoreCase);

    void WriteLog(string s) {
      if (!string.IsNullOrEmpty(_logPath)) {
        try { File.AppendAllText(_logPath, s + "\r\n", new UTF8Encoding(false)); } catch {}
      }
    }

    void EmitLine(string line) {
      if (line == null) return;
      string s = line.TrimEnd();
      if (s.Length > 0 && VisualNoiseRe.IsMatch(s)) return;  // descarta ruído visual do progresso
      Lines.Add(s);
      WriteLog(s);
    }

    void ExtractProgress(string chunk) {
      foreach (Match m in Osc94Re.Matches(chunk)) {
        int st = int.Parse(m.Groups[1].Value);
        int pct = int.Parse(m.Groups[2].Value);
        if ((st == 1 || st == 2) && pct >= 0 && pct <= 100 && pct != _lastPct) {
          _lastPct = pct;
          // Marcador dedicado: o cliente sempre o consome para mover a barra e
          // nunca o exibe no log (não depende de heurística de "NN%").
          WriteLog("__WINGETRM_PCT__" + pct.ToString());
        }
      }
    }

    void ProcessChunk(string chunk) {
      if (string.IsNullOrEmpty(chunk)) return;
      // Extrai a % exata da sequência OSC 9;4 antes de remover os escapes.
      ExtractProgress(chunk);
      // Sequências que apagam/reposicionam a linha marcam um novo "frame" do
      // progresso -> tratamos como quebra de linha para isolar cada atualização.
      string s = EraseRe.Replace(chunk, "\n");
      s = s.Replace("\r", "\n");
      s = OscRe.Replace(s, "");
      s = CsiRe.Replace(s, "");
      s = OtherEscRe.Replace(s, "");
      s = CtrlRe.Replace(s, "");
      _pending.Append(s);
      string acc = _pending.ToString();
      int idx;
      while ((idx = acc.IndexOf('\n')) >= 0) {
        string one = acc.Substring(0, idx);
        EmitLine(one);
        acc = acc.Substring(idx + 1);
      }
      _pending.Clear();
      _pending.Append(acc);
    }

    public int Run(string commandLine, string logPath, short width, short height) {
      _logPath = logPath;
      SafeFileHandle inRead, inWrite, outRead, outWrite;
      if (!CreatePipe(out inRead, out inWrite, IntPtr.Zero, 0)) throw new Win32Exception(Marshal.GetLastWin32Error(), "CreatePipe(in)");
      if (!CreatePipe(out outRead, out outWrite, IntPtr.Zero, 0)) throw new Win32Exception(Marshal.GetLastWin32Error(), "CreatePipe(out)");

      // Evita que o conhost do pseudo-console herde os pipes do PsExec.
      uint[] savedInherit = ClearStdHandleInherit();

      COORD size; size.X = width; size.Y = height;
      IntPtr hPC;
      int hr = CreatePseudoConsole(size, inRead, outWrite, 0, out hPC);
      if (hr != 0) { RestoreStdHandleInherit(savedInherit); throw new Win32Exception(hr, "CreatePseudoConsole"); }

      var siEx = new STARTUPINFOEX();
      siEx.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
      // CRÍTICO: sob PsExec o stdout do PowerShell já está redirecionado (pipe).
      // Sem STARTF_USESTDHANDLES + handles nulos, o winget herda esses handles em
      // vez de conectar ao pseudo-console -> nenhum progresso é emitido.
      siEx.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
      siEx.StartupInfo.hStdInput = IntPtr.Zero;
      siEx.StartupInfo.hStdOutput = IntPtr.Zero;
      siEx.StartupInfo.hStdError = IntPtr.Zero;
      IntPtr lpSize = IntPtr.Zero;
      InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref lpSize);
      siEx.lpAttributeList = Marshal.AllocHGlobal(lpSize);
      if (!InitializeProcThreadAttributeList(siEx.lpAttributeList, 1, 0, ref lpSize)) throw new Win32Exception(Marshal.GetLastWin32Error(), "InitializeProcThreadAttributeList");
      if (!UpdateProcThreadAttribute(siEx.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hPC, (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero)) throw new Win32Exception(Marshal.GetLastWin32Error(), "UpdateProcThreadAttribute");

      PROCESS_INFORMATION pi;
      bool ok = CreateProcess(null, commandLine, IntPtr.Zero, IntPtr.Zero, false, EXTENDED_STARTUPINFO_PRESENT, IntPtr.Zero, null, ref siEx, out pi);
      int createErr = ok ? 0 : Marshal.GetLastWin32Error();

      // Já lançamos o winget (bInheritHandles=false) e o conhost já foi criado por
      // CreatePseudoConsole: podemos devolver a herança dos handles do pai.
      RestoreStdHandleInherit(savedInherit);

      if (!ok) throw new Win32Exception(createErr, "CreateProcess");

      // Fecha os lados que pertencem ao ConPTY/filho.
      outWrite.Dispose();
      inRead.Dispose();

      var readerThread = new Thread(delegate() {
        try {
          using (var fs = new FileStream(outRead, FileAccess.Read)) {
            byte[] buffer = new byte[4096];
            var decoder = Encoding.UTF8.GetDecoder();
            char[] chars = new char[8192];
            int read;
            while ((read = fs.Read(buffer, 0, buffer.Length)) > 0) {
              int cc = decoder.GetChars(buffer, 0, read, chars, 0);
              if (cc > 0) ProcessChunk(new string(chars, 0, cc));
            }
          }
        } catch {}
      });
      readerThread.IsBackground = true;
      readerThread.Start();

      WaitForSingleObject(pi.hProcess, 0xFFFFFFFF);
      uint code;
      if (GetExitCodeProcess(pi.hProcess, out code)) ExitCode = (int)code;

      // Fecha o ConPTY: libera o write side interno -> EOF na thread de leitura.
      ClosePseudoConsole(hPC);
      readerThread.Join(5000);
      if (_pending.Length > 0) EmitLine(_pending.ToString());

      DeleteProcThreadAttributeList(siEx.lpAttributeList);
      Marshal.FreeHGlobal(siEx.lpAttributeList);
      CloseHandle(pi.hThread);
      CloseHandle(pi.hProcess);
      try { inWrite.Dispose(); } catch {}
      return ExitCode;
    }
  }
}
