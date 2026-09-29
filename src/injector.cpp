// NL2Bridge injector: loads NL2Bridge.dll into a running nolimits2stm.exe / nolimits2app.exe.
// Usage: NL2BridgeInjector.exe [path\to\NL2Bridge.dll]   (default: NL2Bridge.dll next to this exe)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <cstdio>
#include <cwchar>

static DWORD FindGame() {
  const wchar_t* names[] = {L"nolimits2stm.exe", L"nolimits2app.exe"};
  HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
  PROCESSENTRY32W pe{sizeof(pe)};
  DWORD pid = 0;
  for (BOOL ok = Process32FirstW(snap, &pe); ok && !pid; ok = Process32NextW(snap, &pe))
    for (auto n : names) if (_wcsicmp(pe.szExeFile, n) == 0) { pid = pe.th32ProcessID; break; }
  CloseHandle(snap);
  return pid;
}

int wmain(int argc, wchar_t** argv) {
  wchar_t dll[MAX_PATH];
  if (argc > 1) GetFullPathNameW(argv[1], MAX_PATH, dll, nullptr);
  else { GetModuleFileNameW(nullptr, dll, MAX_PATH); wcscpy(wcsrchr(dll, L'\\') + 1, L"NL2Bridge.dll"); }
  if (GetFileAttributesW(dll) == INVALID_FILE_ATTRIBUTES) { wprintf(L"DLL not found: %ls\n", dll); return 1; }

  wprintf(L"Waiting for NoLimits 2...\n");
  DWORD pid; while (!(pid = FindGame())) Sleep(500);
  Sleep(3000); // let the Steam stub unpack and the game initialise
  HANDLE p = OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION |
                         PROCESS_VM_WRITE | PROCESS_VM_READ, FALSE, pid);
  if (!p) { wprintf(L"OpenProcess failed (%lu) - try running as the same user / as admin\n", GetLastError()); return 1; }
  size_t bytes = (wcslen(dll) + 1) * sizeof(wchar_t);
  void* mem = VirtualAllocEx(p, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
  WriteProcessMemory(p, mem, dll, bytes, nullptr);
  auto ll = (LPTHREAD_START_ROUTINE)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
  HANDLE t = CreateRemoteThread(p, nullptr, 0, ll, mem, 0, nullptr);
  if (!t) { wprintf(L"CreateRemoteThread failed (%lu)\n", GetLastError()); return 1; }
  WaitForSingleObject(t, 10000);
  DWORD code = 0; GetExitCodeThread(t, &code);
  VirtualFreeEx(p, mem, 0, MEM_RELEASE);
  CloseHandle(t); CloseHandle(p);
  wprintf(code ? L"Injected into PID %lu. The bridge listens on TCP 15152, or the port in <dll name>.port\n"
                 L"(check <dll name>.log next to the DLL).\n"
               : L"LoadLibrary failed in PID %lu\n", pid);
  return code ? 0 : 1;
}
