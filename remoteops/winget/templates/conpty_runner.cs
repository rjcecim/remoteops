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
    const int CREATE_SUSPENDED = 0x00000004;
    const uint WAIT_OBJECT_0 = 0;
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

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
      public long PerProcessUserTimeLimit;
      public long PerJobUserTimeLimit;
      public uint LimitFlags;
      public UIntPtr MinimumWorkingSetSize;
      public UIntPtr MaximumWorkingSetSize;
      public uint ActiveProcessLimit;
      public IntPtr Affinity;
      public uint PriorityClass;
      public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct IO_COUNTERS {
      public ulong ReadOperationCount;
      public ulong WriteOperationCount;
      public ulong OtherOperationCount;
      public ulong ReadTransferCount;
      public ulong WriteTransferCount;
      public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
      public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
      public IO_COUNTERS IoInfo;
      public UIntPtr ProcessMemoryLimit;
      public UIntPtr JobMemoryLimit;
      public UIntPtr PeakProcessMemoryUsed;
      public UIntPtr PeakJobMemoryUsed;
    }

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
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern uint ResumeThread(IntPtr hThread);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateProcess(IntPtr hProcess, uint uExitCode);
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr hJob, int JobObjectInfoClass, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);

    const int STD_INPUT_HANDLE = -10;
    const int STD_OUTPUT_HANDLE = -11;
    const int STD_ERROR_HANDLE = -12;
    const uint HANDLE_FLAG_INHERIT = 0x00000001;
    const int JobObjectExtendedLimitInformation = 9;
    const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    const uint STILL_ACTIVE = 259;

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

    static void SafeDispose(SafeFileHandle h) {
      if (h == null) return;
      try { if (!h.IsClosed) h.Dispose(); } catch {}
    }

    static void SafeClose(IntPtr h) {
      if (h == IntPtr.Zero) return;
      try { CloseHandle(h); } catch {}
    }

    static bool EnableKillOnJobClose(IntPtr hJob) {
      if (hJob == IntPtr.Zero) return false;
      JOBOBJECT_EXTENDED_LIMIT_INFORMATION info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
      info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
      int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
      IntPtr buf = Marshal.AllocHGlobal(size);
      try {
        Marshal.StructureToPtr(info, buf, false);
        return SetInformationJobObject(hJob, JobObjectExtendedLimitInformation, buf, (uint)size);
      } catch {
        return false;
      } finally {
        Marshal.FreeHGlobal(buf);
      }
    }

    static void KillJobOrProcess(IntPtr hJob, IntPtr hProcess) {
      if (hJob != IntPtr.Zero) {
        try { if (TerminateJobObject(hJob, 1)) return; } catch {}
      }
      if (hProcess != IntPtr.Zero) {
        try { TerminateProcess(hProcess, 1); } catch {}
      }
    }

    static bool CancelRequested(string cancelPath) {
      if (string.IsNullOrEmpty(cancelPath)) return false;
      try { return File.Exists(cancelPath); } catch { return false; }
    }

    readonly object _sync = new object();
    readonly List<string> _lines = new List<string>();
    public int ExitCode = 0;
    public bool ProcessStarted = false;
    public bool Cancelled = false;
    public bool TimedOut = false;

    string _logPath;
    int _lastPct = -1;
    StringBuilder _pending = new StringBuilder();
    static readonly Regex EraseRe = new Regex("\u001b\\[[0-9]*[JK]|\u001b\\[[0-9]+G|\u001b\\[[0-9]+;[0-9]+H|\u001b\\[H");
    static readonly Regex CsiRe = new Regex("\u001b\\[[0-9;?]*[ -/]*[@-~]");
    static readonly Regex OscRe = new Regex("\u001b\\][^\u0007\u001b]*(\u0007|\u001b\\\\)");
    static readonly Regex OtherEscRe = new Regex("\u001b[@-Z\\\\-_=>]");
    static readonly Regex CtrlRe = new Regex("[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]");
    static readonly Regex Osc94Re = new Regex("\u001b\\]9;4;([0-9]);([0-9]+)(?:\u0007|\u001b\\\\)");
    static readonly Regex VisualNoiseRe = new Regex(
      "^[\\s\\-\\\\|/\u2500-\u257f\u2580-\u259f\u25a0-\u25ff]*$" +
      "|[0-9][0-9.,]*\\s*(KB|MB|GB|TB|B)\\s*/\\s*[0-9][0-9.,]*\\s*(KB|MB|GB|TB|B)" +
      "|^[\\s\u2500-\u259f]*[0-9]{1,3}%$" +
      "|â[\u0080-\u00ff]{1,3}|Ã[\u0080-\u00bf]|\ufffd",
      RegexOptions.IgnoreCase);

    public List<string> GetLines() {
      lock (_sync) { return new List<string>(_lines); }
    }

    void WriteLog(string s) {
      if (!string.IsNullOrEmpty(_logPath)) {
        try { File.AppendAllText(_logPath, s + "\r\n", new UTF8Encoding(false)); } catch {}
      }
    }

    void EmitLine(string line) {
      if (line == null) return;
      string s = line.TrimEnd();
      if (s.Length > 0 && VisualNoiseRe.IsMatch(s)) return;
      lock (_sync) { _lines.Add(s); }
      WriteLog(s);
    }

    void ExtractProgress(string chunk) {
      foreach (Match m in Osc94Re.Matches(chunk)) {
        int st = int.Parse(m.Groups[1].Value);
        int pct = int.Parse(m.Groups[2].Value);
        if ((st == 1 || st == 2) && pct >= 0 && pct <= 100 && pct != _lastPct) {
          _lastPct = pct;
          WriteLog("__WINGETRM_PCT__" + pct.ToString());
        }
      }
    }

    void ProcessChunk(string chunk) {
      if (string.IsNullOrEmpty(chunk)) return;
      ExtractProgress(chunk);
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

    public int Run(string commandLine, string logPath, short width, short height, string cancelPath, int timeoutMs) {
      _logPath = logPath;
      ProcessStarted = false;
      Cancelled = false;
      TimedOut = false;
      ExitCode = 0;
      lock (_sync) { _lines.Clear(); }
      _pending.Length = 0;
      _lastPct = -1;

      SafeFileHandle inRead = null;
      SafeFileHandle inWrite = null;
      SafeFileHandle outRead = null;
      SafeFileHandle outWrite = null;
      uint[] savedInherit = null;
      bool inheritCleared = false;
      IntPtr hPC = IntPtr.Zero;
      IntPtr attrList = IntPtr.Zero;
      bool attrListInit = false;
      PROCESS_INFORMATION pi = new PROCESS_INFORMATION();
      bool piValid = false;
      IntPtr hJob = IntPtr.Zero;
      Thread readerThread = null;
      SafeFileHandle readerHandle = null;

      try {
        if (!CreatePipe(out inRead, out inWrite, IntPtr.Zero, 0))
          throw new Win32Exception(Marshal.GetLastWin32Error(), "CreatePipe(in)");
        if (!CreatePipe(out outRead, out outWrite, IntPtr.Zero, 0))
          throw new Win32Exception(Marshal.GetLastWin32Error(), "CreatePipe(out)");

        savedInherit = ClearStdHandleInherit();
        inheritCleared = true;

        COORD size;
        size.X = width;
        size.Y = height;
        int hr = CreatePseudoConsole(size, inRead, outWrite, 0, out hPC);
        if (hr != 0) throw new Win32Exception(hr, "CreatePseudoConsole");

        STARTUPINFOEX siEx = new STARTUPINFOEX();
        siEx.StartupInfo.cb = Marshal.SizeOf(typeof(STARTUPINFOEX));
        siEx.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
        siEx.StartupInfo.hStdInput = IntPtr.Zero;
        siEx.StartupInfo.hStdOutput = IntPtr.Zero;
        siEx.StartupInfo.hStdError = IntPtr.Zero;
        IntPtr lpSize = IntPtr.Zero;
        InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref lpSize);
        attrList = Marshal.AllocHGlobal(lpSize);
        siEx.lpAttributeList = attrList;
        if (!InitializeProcThreadAttributeList(siEx.lpAttributeList, 1, 0, ref lpSize))
          throw new Win32Exception(Marshal.GetLastWin32Error(), "InitializeProcThreadAttributeList");
        attrListInit = true;
        if (!UpdateProcThreadAttribute(siEx.lpAttributeList, 0, PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE, hPC, (IntPtr)IntPtr.Size, IntPtr.Zero, IntPtr.Zero))
          throw new Win32Exception(Marshal.GetLastWin32Error(), "UpdateProcThreadAttribute");

        uint flags = (uint)(EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED);
        bool ok = CreateProcess(null, commandLine, IntPtr.Zero, IntPtr.Zero, false, flags, IntPtr.Zero, null, ref siEx, out pi);
        int createErr = ok ? 0 : Marshal.GetLastWin32Error();
        if (!ok) throw new Win32Exception(createErr, "CreateProcess");
        piValid = true;
        ProcessStarted = true;

        hJob = CreateJobObject(IntPtr.Zero, null);
        if (hJob != IntPtr.Zero) {
          EnableKillOnJobClose(hJob);
          try { AssignProcessToJobObject(hJob, pi.hProcess); } catch {}
        }
        ResumeThread(pi.hThread);

        RestoreStdHandleInherit(savedInherit);
        inheritCleared = false;

        SafeDispose(outWrite);
        outWrite = null;
        SafeDispose(inRead);
        inRead = null;

        readerHandle = outRead;
        outRead = null;
        readerThread = new Thread(delegate() {
          try {
            using (FileStream fs = new FileStream(readerHandle, FileAccess.Read)) {
              byte[] buffer = new byte[4096];
              Decoder decoder = Encoding.UTF8.GetDecoder();
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
        try {
          readerThread.Start();
        } catch {
          SafeDispose(readerHandle);
          readerHandle = null;
          readerThread = null;
          throw;
        }

        uint pollMs = 250;
        long elapsed = 0;
        while (true) {
          uint wr = WaitForSingleObject(pi.hProcess, pollMs);
          if (wr == WAIT_OBJECT_0) break;
          if (CancelRequested(cancelPath)) {
            Cancelled = true;
            KillJobOrProcess(hJob, pi.hProcess);
            WaitForSingleObject(pi.hProcess, 4000);
            break;
          }
          if (timeoutMs > 0) {
            elapsed += pollMs;
            if (elapsed >= timeoutMs) {
              TimedOut = true;
              KillJobOrProcess(hJob, pi.hProcess);
              WaitForSingleObject(pi.hProcess, 4000);
              break;
            }
          }
        }

        uint code;
        if (GetExitCodeProcess(pi.hProcess, out code)) ExitCode = (int)code;
        if (Cancelled) ExitCode = unchecked((int)0xC000013A);
        else if (TimedOut) ExitCode = -2;
        return ExitCode;
      }
      finally {
        if (inheritCleared) RestoreStdHandleInherit(savedInherit);

        // Fecha o PTY para gerar EOF no pipe de saída e drenar até o canal encerrar.
        if (hPC != IntPtr.Zero) {
          try { ClosePseudoConsole(hPC); } catch {}
          hPC = IntPtr.Zero;
        }

        if (readerThread != null) {
          // Encerramento normal: drena até EOF (doc Microsoft: manter a saída
          // do pseudoconsole drenada inclusive durante o fechamento).
          // Cancel/timeout: espera mais curta e força EOF no handle de leitura.
          int drainMs = (Cancelled || TimedOut) ? 5000 : 120000;
          if (!readerThread.Join(drainMs)) {
            SafeDispose(readerHandle);
            readerHandle = null;
            int restMs = (Cancelled || TimedOut) ? 2000 : 15000;
            readerThread.Join(restMs);
          }
        }
        try {
          if (_pending.Length > 0) EmitLine(_pending.ToString());
        } catch {}

        if (attrListInit && attrList != IntPtr.Zero) {
          try { DeleteProcThreadAttributeList(attrList); } catch {}
        }
        if (attrList != IntPtr.Zero) {
          try { Marshal.FreeHGlobal(attrList); } catch {}
        }

        if (piValid) {
          uint live = 0;
          try {
            if (GetExitCodeProcess(pi.hProcess, out live) && live == STILL_ACTIVE)
              KillJobOrProcess(hJob, pi.hProcess);
          } catch {}
          SafeClose(pi.hThread);
          SafeClose(pi.hProcess);
        }
        SafeClose(hJob);

        SafeDispose(readerHandle);
        SafeDispose(inRead);
        SafeDispose(inWrite);
        SafeDispose(outRead);
        SafeDispose(outWrite);
      }
    }
  }
}
