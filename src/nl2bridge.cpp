// NL2Bridge - external control/readback API for NoLimits 2
// Loaded into nolimits2stm.exe (via injector.exe). Opens a TCP server (port 15152, or the number in
// <dll name>.port next to the DLL; log goes to <dll name>.log) that speaks the telemetry framing:
//   'N' | u16 msgId | u32 requestId | u16 dataSize | data... | 'L'     (all big-endian)
// Replies use the telemetry reply ids: 1 OK, 2 Error(utf8), 8 Int(i32), 10 String(utf8)
// plus bridge-specific ids >= 1000 (see README / PROTOCOL section).
//
// Thread model: each request is executed on the server thread while holding the game's
// global sim/script lock - the exact same lock NL2's own script natives take - so we
// never touch the block system while the simulation is stepping it.

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <psapi.h>
#include <cstdio>
#include <cstdarg>
#include <cstring>
#include <string>
#include <vector>
#include <atomic>
#include <algorithm>
#include "offsets.h"
#undef R_OK   // io.h access() flag, clashes with the reply id below

using namespace nl2;

// ---------------------------------------------------------------- logging
static FILE* g_log = nullptr;
static void Log(const char* fmt, ...) {
  if (!g_log) return;
  SYSTEMTIME st; GetLocalTime(&st);
  fprintf(g_log, "[%02d:%02d:%02d.%03d] ", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
  va_list ap; va_start(ap, fmt); vfprintf(g_log, fmt, ap); va_end(ap);
  fputc('\n', g_log); fflush(g_log);
}

// ---------------------------------------------------------------- resolved game symbols
typedef void  (*FnLock)(void*);
typedef bool  (*FnSectionSet)(void*, uint32_t, int, int);
typedef bool  (*FnSectionGet)(void*, uint32_t, int, SectionGetResult*);
typedef bool  (*FnStationOp)(void*, int);
typedef int   (*FnStationCan)(void*, int);
typedef void* (*FnStationAt)(void*, int);
typedef void  (*FnSwitchSet)(void*, int, char);
typedef void  (*FnSetBlockMode)(void*, char);
typedef bool  (*FnCanAutoOrManual)(void*, void*, char, char);
typedef bool  (*FnCanFullManual)(void*, void*, char);
typedef void  (*FnSetEStop)(void*, char);
typedef bool  (*FnDeviceActive)(void*);

static struct {
  uint8_t* base = nullptr; size_t size = 0;
  FnLock Lock = nullptr, Unlock = nullptr;
  FnSectionSet SectionSet = nullptr; FnSectionGet SectionGet = nullptr;
  FnStationOp StationOp = nullptr; FnStationCan StationCan = nullptr; FnStationAt StationAt = nullptr;
  FnSwitchSet SwitchSet = nullptr; FnSetBlockMode SetBlockMode = nullptr;
  FnCanAutoOrManual CanAutoOrManual = nullptr; FnCanFullManual CanFullManual = nullptr;
  FnSetEStop SetEStop = nullptr;
  FnDeviceActive DeviceActive = nullptr;   // optional
  volatile char* resetPending = nullptr;   // optional, g_ResetPending
  uint8_t* sensorSyms[5] = {};             // optional, see kSensorSigs
  void*** ppSim = nullptr;   // g_pSim
  void* lockObj = nullptr;   // g_Lock
  bool ok = false;
} G;

static bool ParsePattern(const char* p, std::vector<int>& out) {
  out.clear();
  while (*p) {
    while (*p == ' ') ++p;
    if (!*p) break;
    if (p[0] == '?') { out.push_back(-1); p += (p[1] == '?') ? 2 : 1; continue; }
    unsigned v; if (sscanf(p, "%2x", &v) != 1) return false;
    out.push_back((int)v); p += 2;
  }
  return !out.empty();
}
static bool MatchAt(const uint8_t* at, const std::vector<int>& pat) {
  for (size_t i = 0; i < pat.size(); ++i) if (pat[i] >= 0 && at[i] != (uint8_t)pat[i]) return false;
  return true;
}
static bool IsBridgeHook(const uint8_t* p) {   // jmp [rip+0] + abs64, as written by InstallHook (earlier test build)
  return p[0] == 0xFF && p[1] == 0x25 && p[2] == 0 && p[3] == 0 && p[4] == 0 && p[5] == 0;
}
static uint8_t* Resolve(const Sig& s) {
  std::vector<int> pat; if (!ParsePattern(s.pattern, pat)) return nullptr;
  uint8_t* guess = G.base + s.rva;
  if (s.rva + pat.size() < G.size && MatchAt(guess, pat)) return guess;
  if (s.rva + 14 < G.size && IsBridgeHook(guess)) { Log("  %s: already hooked by another bridge build", s.name); return guess; }
  // game updated? scan the image
  for (size_t off = 0x1000; off + pat.size() < G.size; ++off)
    if (MatchAt(G.base + off, pat)) { Log("  %s: moved 0x%x -> 0x%zx", s.name, s.rva, off); return G.base + off; }
  return nullptr;
}
static uint8_t* RipTarget(uint8_t* fn, uint32_t dispOff) {
  int32_t d; memcpy(&d, fn + dispOff, 4); return fn + dispOff + 4 + d;
}

static bool ResolveAll() {
  HMODULE h = GetModuleHandleW(nullptr);
  MODULEINFO mi{}; GetModuleInformation(GetCurrentProcess(), h, &mi, sizeof(mi));
  G.base = (uint8_t*)mi.lpBaseOfDll; G.size = mi.SizeOfImage;
  Log("module base=%p size=0x%zx", G.base, G.size);
  uint8_t* r[sizeof(kSigs)/sizeof(kSigs[0])] = {};
  bool all = true;
  for (size_t i = 0; i < sizeof(kSigs)/sizeof(kSigs[0]); ++i) {
    r[i] = Resolve(kSigs[i]);
    Log("  %-16s %s %p", kSigs[i].name, r[i] ? "OK  " : "FAIL", r[i]);
    if (!r[i]) all = false;
  }
  G.Lock = (FnLock)r[0]; G.Unlock = (FnLock)r[1];
  G.SectionSet = (FnSectionSet)r[2]; G.SectionGet = (FnSectionGet)r[3];
  G.StationOp = (FnStationOp)r[4]; G.StationCan = (FnStationCan)r[5]; G.StationAt = (FnStationAt)r[6];
  G.SwitchSet = (FnSwitchSet)r[7]; G.SetBlockMode = (FnSetBlockMode)r[8];
  G.CanAutoOrManual = (FnCanAutoOrManual)r[9]; G.CanFullManual = (FnCanFullManual)r[10];
  G.SetEStop = (FnSetEStop)r[11];
  G.ppSim   = (void***)(r[12] ? RipTarget(r[12], ANCHOR_SIM_DISP_OFF)  : G.base + RVA_g_pSim);
  G.lockObj =          (r[13] ? RipTarget(r[13], ANCHOR_LOCK_DISP_OFF) : G.base + RVA_g_Lock);
  Log("  g_pSim=%p g_Lock=%p", G.ppSim, G.lockObj);
  for (size_t i = 0; i < sizeof(kSensorSigs)/sizeof(kSensorSigs[0]); ++i) {
    G.sensorSyms[i] = Resolve(kSensorSigs[i]);
    Log("  %-16s %s %p (optional)", kSensorSigs[i].name, G.sensorSyms[i] ? "OK  " : "FAIL", G.sensorSyms[i]);
  }
  G.DeviceActive = (FnDeviceActive)Resolve(kExtraSigs[0]);
  Log("  %-16s %s %p (optional)", kExtraSigs[0].name, G.DeviceActive ? "OK  " : "FAIL", G.DeviceActive);
  if (uint8_t* nr = Resolve(kExtraSigs[1])) G.resetPending = (volatile char*)RipTarget(nr, ANCHOR_RESET_DISP_OFF);
  Log("  %-16s %s %p (optional)", kExtraSigs[1].name, G.resetPending ? "OK  " : "FAIL", G.resetPending);
  G.ok = all;
  return all;
}

// ---------------------------------------------------------------- game object helpers
template <class T> static T Rd(const void* p, int off) { T v; memcpy(&v, (const uint8_t*)p + off, sizeof(T)); return v; }
static std::string ReadStdString(const void* p) {             // MSVC std::string
  size_t size = Rd<size_t>(p, 0x10), cap = Rd<size_t>(p, 0x18);
  if (size > 4096) return "";
  const char* data = cap > 15 ? Rd<const char*>(p, 0) : (const char*)p;
  return data ? std::string(data, size) : std::string();
}
template <class F> static auto VCall(void* obj, int slotOff) { return (F)((*(void***)obj)[slotOff / 8]); }

struct Locked { Locked() { G.Lock(G.lockObj); } ~Locked() { G.Unlock(G.lockObj); } };

static void* GetPark(std::string* err) {       // requires lock
  void** sim = *G.ppSim;
  if (!sim) { if (err) { *err = "Not in play mode"; } return nullptr; }
  void* park = *sim;
  if (!park || !Rd<uint8_t>(sim, sim::PlayModeA) || !Rd<uint8_t>(park, park::PlayModeB)) {
    if (err) { *err = "Not in play mode"; } return nullptr;
  }
  return park;
}
static int CoasterCount(void* park) {
  return (int)((Rd<uintptr_t>(park, park::CoastersEnd) - Rd<uintptr_t>(park, park::CoastersBegin)) / 8);
}
static uint8_t* GetCoaster(int idx, std::string* err) {
  void* park = GetPark(err); if (!park) return nullptr;
  if (idx < 0 || idx >= CoasterCount(park)) { if (err) { *err = "Invalid coaster index"; } return nullptr; }
  return Rd<uint8_t**>(park, park::CoastersBegin)[idx];
}
static SectionEntry* Entries(uint8_t* c, size_t* n) {
  void* bs = Rd<void*>(c, coaster::BlockSystem);
  if (!bs) { *n = 0; return nullptr; }
  *n = Rd<size_t>(bs, blocksys::EntryCount);
  return Rd<SectionEntry*>(bs, blocksys::Entries);
}
static SectionEntry* FindEntry(uint8_t* c, uint32_t id) {
  size_t n; SectionEntry* e = Entries(c, &n);
  for (size_t i = 0; i < n; ++i) if (e[i].id == id) return &e[i];
  return nullptr;
}
static int SpecialCount(uint8_t* c) {
  return (int)((Rd<uintptr_t>(c, coaster::SpecialEnd) - Rd<uintptr_t>(c, coaster::SpecialBegin)) / 8);
}
static int StationCount(uint8_t* c) {
  return (int)((Rd<uintptr_t>(c, coaster::StationsEnd) - Rd<uintptr_t>(c, coaster::StationsBegin)) / 0x18);
}
static int TrainCount(uint8_t* c) {
  return (int)((Rd<uintptr_t>(c, coaster::TrainsEnd) - Rd<uintptr_t>(c, coaster::TrainsBegin)) / 8);
}
static int TrackIndex(uint8_t* c, const void* trk) {
  void** b = Rd<void**>(c, trig::CoasterTracksBegin), **e = Rd<void**>(c, trig::CoasterTracksEnd);
  for (void** p = b; trk && p < e; ++p) if (*p == trk) return (int)(p - b);
  return -1;
}
static bool IsScriptedNode(void* nd) { return nd && VCall<bool(*)(void*)>(nd, node::VF_IsScripted)(nd); }
// Semi-manual "can advance" - normal and scripted nodes number their ops differently (see offsets.h)
static bool CanAdvance(void* nd, bool bwd) {
  if (!nd) return false;
  auto semi = VCall<int(*)(void*, int)>(nd, node::VF_SemiManual);
  return semi(nd, IsScriptedNode(nd) ? (bwd ? 3 : 2) : (bwd ? 4 : 1)) != 0;
}
static bool SGet(uint8_t* c, uint32_t id, int q, SectionGetResult* r) {
  void* bs = Rd<void*>(c, coaster::BlockSystem);
  return bs && G.SectionGet(bs, id, q, r);
}
static int SGetI(uint8_t* c, uint32_t id, int q, int dflt = 0) {
  SectionGetResult r{}; return SGet(c, id, q, &r) ? r.i : dflt;
}
static double SGetD(uint8_t* c, uint32_t id, int q, double in = 0) {
  SectionGetResult r{}; r.d = in; return SGet(c, id, q, &r) ? r.d : 0.0;
}
static std::string JsonEsc(const std::string& s) {
  std::string o; for (char ch : s) { if (ch == '"' || ch == '\\') { o += '\\'; o += ch; }
    else if ((unsigned char)ch < 0x20) { char b[8]; snprintf(b, 8, "\\u%04x", ch); o += b; } else o += ch; }
  return o;
}

// MSVC RTTI: vtable[-1] -> CompleteObjectLocator {.., +0x0C TypeDescriptor RVA, .., +0x14 self RVA}
static std::string RttiName(const void* obj) {
  if (!obj) return "";
  const uint8_t* vt = Rd<const uint8_t*>(obj, 0);
  if (!vt) return "";
  const uint8_t* col = Rd<const uint8_t*>(vt, -8);
  if (!col || Rd<uint32_t>(col, 0) != 1) return "";
  const uint8_t* base = col - Rd<uint32_t>(col, 0x14);
  std::string s((const char*)(base + Rd<uint32_t>(col, 0x0c) + 0x10));
  if (s.rfind(".?AV", 0) == 0) s = s.substr(4);
  size_t at = s.find('@'); if (at != std::string::npos) s.resize(at);
  return s;
}

// Lets the game write into a std::string (MSVC layout). Heap buffers are released with the game's CRT free.
struct GameString {
  alignas(8) uint8_t raw[32] = {};
  GameString() { raw[0x18] = 15; }
  ~GameString() {
    if (Rd<size_t>(raw, 0x18) > 15) {
      static auto crtFree = (void(*)(void*))GetProcAddress(GetModuleHandleW(L"ucrtbase.dll"), "free");
      if (crtFree) crtFree(Rd<void*>(raw, 0));      // < 4 KB strings come straight from malloc
    }
  }
  std::string str() const { return ReadStdString(raw); }
};

static std::string SpecialType(const std::string& cls) {
  if (cls.find("TransferTable") != std::string::npos) return "transferTable";
  if (cls.find("Fork") != std::string::npos) return "switchFork";
  if (cls.find("Merge") != std::string::npos) return "switchMerge";
  if (cls.find("Switch") != std::string::npos) return "switch";
  return "special";
}

// ---- station status (works in every dispatch mode; the game's own StationCan position queries
// only answer in manual dispatch, so positions are read straight from the devices/train)
enum : uint32_t {
  SF_ESTOP = 1u << 0, SF_MANUAL = 1u << 1, SF_CAN_DISPATCH = 1u << 2,
  SF_CAN_CLOSE_GATES = 1u << 3, SF_CAN_OPEN_GATES = 1u << 4, SF_CAN_CLOSE_HARNESS = 1u << 5, SF_CAN_OPEN_HARNESS = 1u << 6,
  SF_CAN_RAISE_PLATFORM = 1u << 7, SF_CAN_LOWER_PLATFORM = 1u << 8, SF_CAN_LOCK_FLYER = 1u << 9, SF_CAN_UNLOCK_FLYER = 1u << 10,
  SF_TRAIN_READY = 1u << 11,
  SF_GATES_OPEN = 1u << 12, SF_GATES_CLOSED = 1u << 13, SF_HARNESS_OPEN = 1u << 14, SF_HARNESS_CLOSED = 1u << 15,
  SF_PLATFORM_RAISED = 1u << 16, SF_PLATFORM_LOWERED = 1u << 17, SF_FLYER_UNLOCKED = 1u << 18, SF_FLYER_LOCKED = 1u << 19,
  SF_HAS_GATES = 1u << 20, SF_HAS_PLATFORM = 1u << 21, SF_HAS_FLYER = 1u << 22, SF_HAS_TRAIN = 1u << 23,
  SF_ROWS_OPEN = 1u << 24,   // rows opened individually (restraints.inc): harness not closed, dispatch blocked
  SF_CUSTOM_TRAIN = 1u << 25,   // train in station has no NL2 car model (drawn by a park script): no seats / rows
};
static int RowsNotClosed(uint8_t* tr);   // restraints.inc
struct TrainSeats { int seats = 0, cars = 0, maxPerCar = 0; bool custom = false; };   // cars = cars that carry seats;
                                                                                    // custom = no NL2 car model (script-drawn train)
static TrainSeats GetTrainSeats(uint8_t* tr);   // restraints.inc
struct StationStatus {
  uint32_t flags = 0; uint8_t state = 0;
  double gates = -1, platform = -1; float harness = -1, flyer = -1;   // positions, -1 = n/a
  bool gatesOpening = false, gatesClosing = false, platformMoving = false, harnessMoving = false, flyerMoving = false;
  int rowsOpen = 0;
  TrainSeats seats;   // capacity of the train currently in the station (0 when empty)
};
static StationStatus GetStationStatus(uint8_t* c, uint8_t* st) {   // requires lock
  StationStatus s;
  uint8_t* sec = Rd<uint8_t*>(st, station::Section);
  uint8_t* tr = Rd<uint8_t*>(st, station::Train);
  s.state = Rd<uint8_t>(st, station::State);
  if (Rd<uint8_t>(c, coaster::EStop)) s.flags |= SF_ESTOP;
  if (!Rd<uint8_t>(st, station::AutoDispatch)) s.flags |= SF_MANUAL;
  static const struct { int q; uint32_t bit; } can[] = {
    {SQ_CAN_DISPATCH, SF_CAN_DISPATCH}, {SQ_CAN_CLOSE_GATES, SF_CAN_CLOSE_GATES}, {SQ_CAN_OPEN_GATES, SF_CAN_OPEN_GATES},
    {SQ_CAN_CLOSE_HARNESS, SF_CAN_CLOSE_HARNESS}, {SQ_CAN_OPEN_HARNESS, SF_CAN_OPEN_HARNESS},
    {SQ_CAN_RAISE_PLATFORM, SF_CAN_RAISE_PLATFORM}, {SQ_CAN_LOWER_PLATFORM, SF_CAN_LOWER_PLATFORM},
    {SQ_CAN_LOCK_FLYER, SF_CAN_LOCK_FLYER}, {SQ_CAN_UNLOCK_FLYER, SF_CAN_UNLOCK_FLYER}};
  for (auto& q : can) if (G.StationCan(st, q.q)) s.flags |= q.bit;
  if (tr) s.flags |= SF_HAS_TRAIN;
  if (tr && s.state >= 0x15 && s.state <= 0x17) s.flags |= SF_TRAIN_READY;
  if (uint8_t* g = sec ? Rd<uint8_t*>(sec, section::GatesDev) : nullptr) {
    s.flags |= SF_HAS_GATES;
    s.gates = Rd<double>(g, device::Pos);
    s.gatesOpening = Rd<uint8_t>(g, device::GateOpening); s.gatesClosing = Rd<uint8_t>(g, device::GateClosing);
    if (s.gates == 1.0 && !s.gatesOpening && !s.gatesClosing) s.flags |= SF_GATES_OPEN;
    if (s.gates == 0.0 && !s.gatesOpening && !s.gatesClosing) s.flags |= SF_GATES_CLOSED;
  }
  if (uint8_t* p = sec ? Rd<uint8_t*>(sec, section::PlatformDev) : nullptr) {
    if (G.DeviceActive ? G.DeviceActive(p) : Rd<double>(p, device::Travel) > 0) {
      s.flags |= SF_HAS_PLATFORM;
      s.platform = Rd<double>(p, device::Pos);
      uint8_t m = Rd<uint8_t>(p, device::PlatformMotion);
      s.platformMoving = m == 1 || m == 2;
      if (s.platform == 1.0 && m != 1) s.flags |= SF_PLATFORM_RAISED;
      if (s.platform == 0.0 && m != 2) s.flags |= SF_PLATFORM_LOWERED;
    }
  }
  if (uint8_t* f = sec ? Rd<uint8_t*>(sec, section::FlyerDev) : nullptr)
    if (Rd<uint8_t>(st, station::HasFlyer) && (!G.DeviceActive || G.DeviceActive(f))) s.flags |= SF_HAS_FLYER;
  if (tr) {
    s.seats = GetTrainSeats(tr);
    if (s.seats.custom) s.flags |= SF_CUSTOM_TRAIN;
    s.harness = Rd<float>(tr, train::HarnessPos);
    uint8_t hm = Rd<uint8_t>(tr, train::HarnessMotion);
    s.harnessMoving = hm == 1 || hm == 2;
    if (s.harness == 1.0f && hm != 1) s.flags |= SF_HARNESS_OPEN;
    if (s.harness == 0.0f && hm != 2) s.flags |= SF_HARNESS_CLOSED;
    if ((s.rowsOpen = RowsNotClosed(tr)) > 0) {
      s.flags &= ~(SF_HARNESS_CLOSED | SF_CAN_DISPATCH);
      s.flags |= SF_ROWS_OPEN | SF_CAN_CLOSE_HARNESS;
    }
    if (s.flags & SF_HAS_FLYER) {
      s.flyer = Rd<float>(tr, train::FlyerPos);
      uint8_t fm = Rd<uint8_t>(tr, train::FlyerMotion);
      s.flyerMoving = fm == 1 || fm == 2;
      if (s.flyer == 0.0f && fm != 1) s.flags |= SF_FLYER_UNLOCKED;
      if (s.flyer == 1.0f && fm != 2) s.flags |= SF_FLYER_LOCKED;
    }
  }
  return s;
}

// ---- JSON builders (require lock)
static std::string CoasterJson(void* park, int i) {
  uint8_t* c = Rd<uint8_t**>(park, park::CoastersBegin)[i];
  size_t ne; Entries(c, &ne);
  char buf[640];
  snprintf(buf, sizeof buf, "{\"index\":%d,\"name\":\"%s\",\"operationMode\":%u,\"scripted\":%d,\"blockMode\":%u,\"estop\":%u,"
           "\"ready\":%u,\"trains\":%d,\"sections\":%zu,\"stations\":%d,\"specialTracks\":%d}",
           i, JsonEsc(ReadStdString(c + coaster::Name)).c_str(),
           Rd<uint8_t>(c, coaster::OperationMode), Rd<uint8_t>(c, coaster::OperationMode) == 2,
           Rd<uint8_t>(c, coaster::BlockMode), Rd<uint8_t>(c, coaster::EStop), Rd<uint8_t>(c, coaster::Ready),
           (int)((Rd<uintptr_t>(c, coaster::TrainsEnd) - Rd<uintptr_t>(c, coaster::TrainsBegin)) / 8),
           ne, StationCount(c), SpecialCount(c));
  return buf;
}

static std::string SectionsJson(uint8_t* c) {
  size_t n; SectionEntry* e = Entries(c, &n);
  std::string j = "[";
  for (size_t i = 0; i < n; ++i) {
    uint8_t* s = e[i].section; void* nd = e[i].node;
    std::string name = s ? ReadStdString(s + section::Name) : "";
    bool scripted = nd ? VCall<bool(*)(void*)>(nd, node::VF_IsScripted)(nd) : false;
    bool isStation = nd ? VCall<void*(*)(void*)>(nd, node::VF_GetStation)(nd) != nullptr : false;
    uint8_t st = nd ? Rd<uint8_t>(nd, node::State) : 0, lamp = 0;
    std::string text;
    if (nd) { GameString gs; VCall<void(*)(void*, void*, uint8_t*)>(nd, node::VF_GetStateText)(nd, gs.raw, &lamp); text = gs.str(); }
    int adv = (CanAdvance(nd, false) ? 1 : 0) | (CanAdvance(nd, true) ? 2 : 0);
    uint32_t trainMask = (uint32_t)SGetI(c, e[i].id, SG_TRAIN_MASK);
    int userState = scripted ? Rd<int32_t>(nd, node::UserState) : -1;
    char buf[1600];
    snprintf(buf, sizeof buf, "%s{\"id\":%u,\"name\":\"%s\",\"hasNode\":%d,\"isBlock\":%d,\"scripted\":%d,\"station\":%d,"
             "\"nodeType\":\"%s\",\"state\":%u,\"stateName\":\"%s\",\"stateText\":\"%s\",\"lamp\":%u,\"trains\":%u,\"trainMask\":%u,"
             "\"canAdvanceFwd\":%d,\"canAdvanceBwd\":%d,\"hasBrake\":%d,\"hasLift\":%d,\"hasTransport\":%d,"
             "\"brakeMode\":%d,\"liftMode\":%d,\"transportMode\":%d,\"userState\":%d,"
             "\"advFwdVisible\":%d,\"advFwdEnabled\":%d,\"advBwdVisible\":%d,\"advBwdEnabled\":%d,"
             "\"track\":%d,\"trackStart\":%.3f,\"trackEnd\":%.3f}",
             i ? "," : "", e[i].id, JsonEsc(name).c_str(), nd != nullptr, nd ? Rd<uint8_t>(nd, node::IsBlock) : 0,
             scripted, isStation, JsonEsc(RttiName(nd)).c_str(), st, BlockStateName(st), JsonEsc(text).c_str(), lamp,
             nd ? Rd<uint8_t>(nd, node::TrainCount) : 0, trainMask, adv & 1, (adv >> 1) & 1,
             s && Rd<void*>(s, section::BrakeDev) != nullptr, s && Rd<void*>(s, section::LiftDev) != nullptr,
             s && Rd<void*>(s, section::TransportDev) != nullptr,
             s ? Rd<uint8_t>(s, section::BrakeMode) : -1, s ? Rd<uint8_t>(s, section::LiftMode) : -1,
             s ? Rd<uint8_t>(s, section::TransportMode) : -1, userState,
             scripted ? Rd<uint8_t>(nd, node::FwdVisible) : 0, scripted ? Rd<uint8_t>(nd, node::FwdEnabled) : 0,
             scripted ? Rd<uint8_t>(nd, node::BwdVisible) : 0, scripted ? Rd<uint8_t>(nd, node::BwdEnabled) : 0,
             s ? TrackIndex(c, Rd<void*>(s, section::Track)) : -1,
             s ? Rd<double>(s, section::TrackStart) : 0.0, s ? Rd<double>(s, section::TrackEnd) : 0.0);
    j += buf;
  }
  return j + "]";
}

// Where every train is and what it is doing (requires lock)
static std::string TrainsJson(uint8_t* c) {
  size_t ne; SectionEntry* e = Entries(c, &ne);
  std::vector<uint32_t> masks(ne);
  for (size_t i = 0; i < ne; ++i) masks[i] = (uint32_t)SGetI(c, e[i].id, SG_TRAIN_MASK);
  StationEntry* se = Rd<StationEntry*>(c, coaster::StationsBegin);
  int nst = StationCount(c);
  uint8_t** tb = Rd<uint8_t**>(c, coaster::TrainsBegin);
  std::string j = "[";
  for (int i = 0, n = TrainCount(c); i < n; ++i) {
    uint8_t* t = tb[i];
    int idx = Rd<uint8_t>(t, train::Index);
    void* hn = Rd<void*>(t, train::HoldingNode);
    uint8_t* hs = hn ? Rd<uint8_t*>(hn, node::Section) : nullptr;
    int station = -1;
    for (int k = 0; k < nst; ++k) if (se[k].station && Rd<uint8_t*>(se[k].station, station::Train) == t) station = k;
    std::string secs;
    for (size_t k = 0; k < ne; ++k) if (idx < 32 && (masks[k] >> idx) & 1) { if (!secs.empty()) secs += ","; secs += std::to_string(e[k].id); }
    TrainSeats ts = GetTrainSeats(t);
    char buf[1024];
    snprintf(buf, sizeof buf, "%s{\"index\":%d,\"blockId\":%u,\"blockName\":\"%s\",\"sections\":[%s],\"station\":%d,"
             "\"seats\":%d,\"seatedCars\":%d,\"seatsPerCar\":%d,\"customTrain\":%d,"
             "\"speed\":%.3f,\"accel\":%.3f,\"harness\":%.3f,\"flyer\":%.3f,\"lashed\":%d,"
             "\"front\":{\"track\":%d,\"pos\":%.3f},\"center\":{\"track\":%d,\"pos\":%.3f},\"rear\":{\"track\":%d,\"pos\":%.3f}}",
             i ? "," : "", idx, hs ? Rd<uint32_t>(hs, section::Id) : 0, JsonEsc(hs ? ReadStdString(hs + section::Name) : "").c_str(),
             secs.c_str(), station, ts.seats, ts.cars, ts.maxPerCar, ts.custom, Rd<double>(t, train::Speed), Rd<double>(t, train::Accel),
             (double)Rd<float>(t, train::HarnessPos), (double)Rd<float>(t, train::FlyerPos), Rd<uint8_t>(t, train::Lashed),
             TrackIndex(c, Rd<void*>(t, train::FrontTrack)), Rd<double>(t, train::FrontPos),
             TrackIndex(c, Rd<void*>(t, train::CenterTrack)), Rd<double>(t, train::CenterPos),
             TrackIndex(c, Rd<void*>(t, train::RearTrack)), Rd<double>(t, train::RearPos));
    j += buf;
  }
  return j + "]";
}

static std::string TrackLinkJson(uint8_t* c, uint8_t* t, int linkOff, int portOff) {
  void* o = Rd<void*>(t, linkOff);
  if (!o) return "null";
  char buf[96];
  for (int i = 0, n = SpecialCount(c); i < n; ++i)
    if (Rd<void**>(c, coaster::SpecialBegin)[i] == o) {
      snprintf(buf, sizeof buf, "{\"special\":%d,\"port\":%d}", i, Rd<int32_t>(t, portOff));
      return buf;
    }
  int ti = TrackIndex(c, o);
  if (ti >= 0) { snprintf(buf, sizeof buf, "{\"track\":%d}", ti); return buf; }
  return "null";
}

static std::string TracksJson(uint8_t* c) {
  std::string j = "[";
  void** b = Rd<void**>(c, trig::CoasterTracksBegin), **e = Rd<void**>(c, trig::CoasterTracksEnd);
  for (void** p = b; p < e; ++p) {
    uint8_t* t = (uint8_t*)*p;
    std::string cls = RttiName(t);
    bool custom = cls == "NLCustomTrack";
    char buf[128];
    snprintf(buf, sizeof buf, "%s{\"index\":%d,\"class\":\"%s\",", p == b ? "" : ",", (int)(p - b), JsonEsc(cls).c_str());
    j += buf;
    j += "\"start\":" + (custom ? TrackLinkJson(c, t, track::StartLink, track::StartPort) : std::string("null"));
    j += ",\"end\":" + (custom ? TrackLinkJson(c, t, track::EndLink, track::EndPort) : std::string("null")) + "}";
  }
  return j + "]";
}

static std::string SwitchesJson(uint8_t* c) {
  std::string j = "[";
  for (int i = 0, n = SpecialCount(c); i < n; ++i) {
    uint8_t* t = Rd<uint8_t**>(c, coaster::SpecialBegin)[i];
    int dirs = VCall<int(*)(void*)>(t, special::VF_DirCount)(t);
    std::string cls = RttiName(t);
    int cur = Rd<int32_t>(t, special::CurrentDir);
    char buf[512];
    snprintf(buf, sizeof buf, "%s{\"index\":%d,\"name\":\"%s\",\"type\":\"%s\",\"class\":\"%s\",\"directions\":%d,\"current\":%d,"
             "\"target\":%d,\"moving\":%d,\"switchable\":%d,\"manualAllowed\":%d}",
             i ? "," : "", i, JsonEsc(ReadStdString(t + special::Name)).c_str(), SpecialType(cls).c_str(), JsonEsc(cls).c_str(),
             dirs, cur, Rd<int32_t>(t, special::TargetDir), cur < 0,
             Rd<uint8_t>(t, special::Switchable1) || Rd<uint8_t>(t, special::Switchable2), Rd<uint8_t>(t, special::ManualAllowed));
    j += buf;
  }
  return j + "]";
}

static std::string StationsJson(uint8_t* c) {
  std::string j = "[";
  StationEntry* se = Rd<StationEntry*>(c, coaster::StationsBegin);
  for (int i = 0, n = StationCount(c); i < n; ++i) {
    uint8_t* st = se[i].station; uint8_t* sec = se[i].section;
    if (!st) continue;
    StationStatus s = GetStationStatus(c, st);
    auto b = [&](uint32_t f) { return (s.flags & f) ? 1 : 0; };
    char buf[1536];
    snprintf(buf, sizeof buf,
      "%s{\"index\":%d,\"name\":\"%s\",\"sectionId\":%u,\"flags\":%u,\"state\":%u,\"manualDispatch\":%d,\"hasTrain\":%d,"
      "\"train\":%d,\"seats\":%d,\"seatedCars\":%d,\"seatsPerCar\":%d,\"customTrain\":%d,"
      "\"trainReady\":%d,\"canDispatch\":%d,\"waitingForClearBlock\":%d,\"waitingForAdvance\":%d,"
      "\"gates\":{\"present\":%d,\"position\":%.3f,\"open\":%d,\"closed\":%d,\"opening\":%d,\"closing\":%d,\"canOpen\":%d,\"canClose\":%d},"
      "\"harness\":{\"position\":%.3f,\"open\":%d,\"closed\":%d,\"moving\":%d,\"canOpen\":%d,\"canClose\":%d,\"rowsOpen\":%d},"
      "\"platform\":{\"present\":%d,\"position\":%.3f,\"raised\":%d,\"lowered\":%d,\"moving\":%d,\"canRaise\":%d,\"canLower\":%d},"
      "\"flyer\":{\"present\":%d,\"position\":%.3f,\"locked\":%d,\"unlocked\":%d,\"moving\":%d,\"canLock\":%d,\"canUnlock\":%d}}",
      i ? "," : "", i, JsonEsc(sec ? ReadStdString(sec + section::Name) : "").c_str(), sec ? Rd<uint32_t>(sec, section::Id) : 0,
      s.flags, s.state, b(SF_MANUAL), b(SF_HAS_TRAIN),
      se[i].station && Rd<uint8_t*>(st, station::Train) ? (int)Rd<uint8_t>(Rd<uint8_t*>(st, station::Train), train::Index) : -1,
      s.seats.seats, s.seats.cars, s.seats.maxPerCar, b(SF_CUSTOM_TRAIN), b(SF_TRAIN_READY), b(SF_CAN_DISPATCH),
      s.state == 0x12 || s.state == 0x15, s.state == 0x14 || s.state == 0x17,
      b(SF_HAS_GATES), s.gates, b(SF_GATES_OPEN), b(SF_GATES_CLOSED), s.gatesOpening, s.gatesClosing, b(SF_CAN_OPEN_GATES), b(SF_CAN_CLOSE_GATES),
      (double)s.harness, b(SF_HARNESS_OPEN), b(SF_HARNESS_CLOSED), s.harnessMoving, b(SF_CAN_OPEN_HARNESS), b(SF_CAN_CLOSE_HARNESS), s.rowsOpen,
      b(SF_HAS_PLATFORM), s.platform, b(SF_PLATFORM_RAISED), b(SF_PLATFORM_LOWERED), s.platformMoving, b(SF_CAN_RAISE_PLATFORM), b(SF_CAN_LOWER_PLATFORM),
      b(SF_HAS_FLYER), (double)s.flyer, b(SF_FLYER_LOCKED), b(SF_FLYER_UNLOCKED), s.flyerMoving, b(SF_CAN_LOCK_FLYER), b(SF_CAN_UNLOCK_FLYER));
    j += buf;
  }
  return j + "]";
}

// Why a station op would be refused (empty = allowed). Mirrors the checks in StationOp/StationCan.
static std::string StationOpRefusal(uint8_t* c, uint8_t* st, int op) {
  static const int need[] = {-1, -1, SQ_CAN_DISPATCH, SQ_CAN_OPEN_GATES, SQ_CAN_CLOSE_GATES, SQ_CAN_OPEN_HARNESS,
                             SQ_CAN_CLOSE_HARNESS, SQ_CAN_RAISE_PLATFORM, SQ_CAN_LOWER_PLATFORM, SQ_CAN_UNLOCK_FLYER, SQ_CAN_LOCK_FLYER};
  if (op < 0 || op > 10) return "Invalid station op";
  if (op <= 1) return "";
  uint8_t* tr = Rd<uint8_t*>(st, station::Train);
  if (op == ST_DISPATCH && RowsNotClosed(tr)) return "Row restraints are open - close them before dispatch";
  if (G.StationCan(st, need[op])) return "";
  StationStatus s = GetStationStatus(c, st);
  if (s.flags & SF_ESTOP) return "E-stop is active";
  if (op == ST_DISPATCH) {
    if (!(s.flags & SF_MANUAL)) return "Station is in automatic dispatch mode";
    if (Rd<uint8_t>(c, coaster::BlockMode) == 3) return "Dispatch not available in full manual block mode";
    return "Station not ready to dispatch (gates/harness/floor/next block)";
  }
  if (!(s.flags & SF_MANUAL)) return "Station must be in manual dispatch mode (op 0) for gates/harness/floor/flyer control";
  if (!(s.flags & SF_TRAIN_READY)) return "No train stopped in the station";
  if ((op == ST_PLATFORM_RAISE || op == ST_PLATFORM_LOWER) && !(s.flags & SF_HAS_PLATFORM)) return "Station has no platform/floor";
  if ((op == ST_FLYER_UNLOCK || op == ST_FLYER_LOCK) && !(s.flags & SF_HAS_FLYER)) return "Coaster has no flyer seats";
  if ((op == ST_GATES_OPEN || op == ST_GATES_CLOSE) && !(s.flags & SF_HAS_GATES)) return "Station has no gates";
  return "Already in that position, moving, or blocked by another device (e.g. harness vs. gates/floor)";
}

// ---------------------------------------------------------------- protocol
enum : uint16_t {
  R_OK = 1, R_ERROR = 2, R_INT = 8, R_STRING = 10,
  Q_PING = 1000,
  Q_BRIDGE_INFO = 1001,         // -> String (JSON {name, api, build}) - lets clients require a minimum bridge API
  Q_GET_COASTERS_JSON = 1200,   // -> String (JSON)
  Q_GET_SECTIONS_JSON = 1201,   // i32 coaster -> String (JSON)
  Q_GET_SWITCHES_JSON = 1202,   // i32 coaster -> String (JSON)
  Q_GET_STATIONS_JSON = 1203,   // i32 coaster -> String (JSON)
  Q_GET_COASTER_INFO  = 1204,   // i32 coaster -> String (JSON: coaster, sections, stations, specialTracks)
  Q_GET_TRAINS_JSON   = 1205,   // i32 coaster -> String (JSON)
  Q_GET_BLOCK_STATES  = 1210,   // i32 coaster -> R_BLOCK_STATES
  R_BLOCK_STATES      = 1211,   // u8 mode, u8 estop, u16 count, then per section 16 bytes (see README)
  Q_GET_STATION_STATES= 1212,   // i32 coaster -> R_STATION_STATES
  R_STATION_STATES    = 1213,   // u16 count, then per station: u32 flags, u8 state, u8 rowsOpen, u16 seats (train in station)
  Q_GET_SWITCH_STATES = 1214,   // i32 coaster -> R_SWITCH_STATES
  R_SWITCH_STATES     = 1215,   // u16 count, then per special track: i8 current, i8 target, u8 directions, u8 flags
  Q_GET_SECTION_DETAIL= 1216,   // i32 coaster -> R_SECTION_DETAIL (train-position flags for block logic)
  R_SECTION_DETAIL    = 1217,   // u8 opMode, u8 blockMode, u8 estop, u8 0, u16 count, then per section 20 bytes (see README)
  Q_SECTION_SET       = 1100,   // i32 coaster, i32 sectionId, i32 cmd, i32 param -> Int (1/0)
  Q_SECTION_GET       = 1101,   // i32 coaster, i32 sectionId, i32 cmd, f64 in -> R_SECTION_GET
  R_SECTION_GET       = 1102,   // i32 ok, i32 ival, f64 dval
  Q_BLOCK_ADVANCE     = 1110,   // i32 coaster, i32 sectionId, i32 dir (1 fwd, 2 bwd) -> Int
  Q_BLOCK_SET         = 1111,   // i32 coaster, i32 sectionId, i32 cmd, i32 param -> Int (raw block node SetState)
  Q_REGISTER_STATE    = 1112,   // i32 coaster, i32 sectionId, i32 state, i32 lamp, utf8 text -> OK   (scripted: Block.registerState)
  Q_SET_USER_STATE    = 1113,   // i32 coaster, i32 sectionId, i32 state -> OK                       (scripted: Block.setState)
  Q_SET_SWITCH        = 1120,   // i32 coaster, i32 specialTrackIndex, i32 dir -> Int
  Q_SET_BLOCK_MODE    = 1130,   // i32 coaster, i32 mode (0 auto, 1 manual block, 2 full manual) -> Int
  Q_SET_ESTOP         = 1131,   // i32 coaster, u8 on -> OK
  Q_RESET_COASTER     = 1132,   // i32 coaster -> OK   (Coaster.requestReset: trains back to start positions)
  Q_DEVICE_SET        = 1150,   // i32 coaster, i32 sectionId, i32 device (0 brake,1 lift,2 transport), i32 value -> Int (full manual only)
  Q_DEVICE_GET        = 1207,   // i32 coaster, i32 sectionId -> String (JSON: brake/lift/transport device state)
  Q_DEVICE_PARAMS_GET = 1208,   // i32 coaster, i32 sectionId -> String (JSON: lift/transport speed, accel, decel, current)
  Q_DEVICE_PARAMS_SET = 1152,   // i32 coaster, i32 sectionId, i32 device (1 lift, 2 transport), f64 speed, f64 accel,
                                //   f64 decel (m/s, m/s^2; < 0 = unchanged) -> OK. Any block mode.
  Q_TRAIN_LASH        = 1151,   // i32 coaster, i32 train index, i32 lashed (1/0) -> OK   (Train.setLashedToTrack)
  Q_STATION_OP        = 1140,   // i32 coaster, i32 station, i32 op -> Int
  Q_STATION_OP_CHECKED= 1141,   // i32 coaster, i32 station, i32 op -> OK, or Error with the reason it was refused
  Q_ROW_RESTRAINT     = 1142,   // i32 coaster, i32 station, i32 row (1-based, 0 = all rows), i32 open (1/0) -> OK or Error
  Q_GET_ROWS_JSON     = 1206,   // i32 coaster, i32 station -> String (JSON: per-row restraint state of the station's train)
  Q_GET_SENSORS_JSON  = 1300,   // i32 coaster (-1 = all) -> String (JSON)
  Q_GET_SENSOR_STATES = 1301,   // i32 coaster (-1 = all) -> R_SENSOR_STATES
  R_SENSOR_STATES     = 1302,   // u16 count, then per sensor: u32 key, u8 active, u8 lastTrain, u16 0, u32 trainMask, u32 passes
  Q_GET_EVENTS        = 1303,   // u32 sinceSeq -> String (JSON {seq, events:[...]}) - sensor edges, block mode changes, advance buttons
};

struct Reader {
  const uint8_t* p; size_t n, i = 0; bool bad = false;
  std::string rest() { std::string s((const char*)p + i, n - i); i = n; return s; }
  uint32_t u32() { if (i + 4 > n) { bad = true; return 0; } uint32_t v = (p[i]<<24)|(p[i+1]<<16)|(p[i+2]<<8)|p[i+3]; i += 4; return v; }
  int32_t  i32() { return (int32_t)u32(); }
  uint8_t  u8()  { if (i + 1 > n) { bad = true; return 0; } return p[i++]; }
  double   f64() { uint64_t v = ((uint64_t)u32() << 32); v |= u32(); double d; memcpy(&d, &v, 8); return d; }
};
struct Writer {
  std::vector<uint8_t> b;
  void u8(uint8_t v) { b.push_back(v); }
  void u16(uint16_t v) { u8(v >> 8); u8(v & 0xff); }
  void u32(uint32_t v) { u16(v >> 16); u16(v & 0xffff); }
  void i32(int32_t v) { u32((uint32_t)v); }
  void f64(double d) { uint64_t v; memcpy(&v, &d, 8); u32((uint32_t)(v >> 32)); u32((uint32_t)v); }
  void str(const std::string& s) { b.insert(b.end(), s.begin(), s.end()); }
};

static void Frame(std::vector<uint8_t>& out, uint16_t id, uint32_t req, const std::vector<uint8_t>& data) {
  Writer w; w.u8('N'); w.u16(id); w.u32(req); w.u16((uint16_t)data.size()); w.b.insert(w.b.end(), data.begin(), data.end()); w.u8('L');
  out.insert(out.end(), w.b.begin(), w.b.end());
}

#include "sensors.inc"
#include "restraints.inc"

// Row-restraint side of a station op. Returns true when the op was fully handled here (reply set in *ok/*why).
static bool RowsStationOp(uint8_t* st, int op, bool* ok, std::string* why) {
  uint8_t* tr = Rd<uint8_t*>(st, station::Train);
  if (!tr || !RowsNotClosed(tr)) return false;
  if (op == ST_DISPATCH) { *ok = false; *why = "Row restraints are open - close them before dispatch"; return true; }
  if (op == ST_HARNESS_CLOSE) {
    RowsCloseAll(tr);
    if (!G.StationCan(st, SQ_CAN_CLOSE_HARNESS)) { *ok = true; return true; }  // train value already closed: only rows move
  }
  return false;
}

static uint16_t Handle(uint16_t id, Reader& r, Writer& w) {
  if (!G.ok) { w.str("NL2Bridge: game functions not resolved (see NL2Bridge.log)"); return R_ERROR; }
  std::string err;
  auto fail = [&](const std::string& e) { w.b.clear(); w.str(e); return (uint16_t)R_ERROR; };
  auto intReply = [&](int v) { w.i32(v); return (uint16_t)R_INT; };

  switch (id) {
  case Q_PING: return R_OK;

  case Q_BRIDGE_INFO:
    w.str("{\"name\":\"NL2Bridge\",\"api\":8,\"build\":\"1.2.0\"}");
    return R_STRING;

  case Q_DEVICE_PARAMS_GET: {
    int ci = r.i32(), sid = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    SectionEntry* e = FindEntry(c, (uint32_t)sid); if (!e || !e->section) return fail("Unknown section");
    std::string j = "{";
    const char* nm[2] = {"lift", "transport"};
    for (int k = 0; k < 2; ++k) {
      uint8_t* dev = Rd<uint8_t*>(e->section, section::LiftDev + 8 * k);
      uint8_t* chain = (dev && !k) ? Rd<uint8_t*>(dev, device::LiftChain) : nullptr;
      char b[256];
      if (!dev) snprintf(b, sizeof b, "%s\"%s\":null", k ? "," : "", nm[k]);
      else snprintf(b, sizeof b, "%s\"%s\":{\"speed\":%.4f,\"accel\":%.4f,\"decel\":%.4f,\"idleMode\":%d,\"current\":%.4f}",
                    k ? "," : "", nm[k], Rd<double>(dev, device::Speed), Rd<double>(dev, device::Accel),
                    Rd<double>(dev, device::Decel), chain ? Rd<uint8_t>(chain, device::ChainIdleFlag) : 0,
                    VCall<double(*)(void*)>(dev, device::VF_CurrentSpeed)(dev));
      j += b;
    }
    w.str(j + "}"); return R_STRING;
  }

  case Q_DEVICE_PARAMS_SET: {
    int ci = r.i32(), sid = r.i32(), d = r.i32();
    double sp = r.f64(), ac = r.f64(), de = r.f64(); if (r.bad) return fail("Invalid message");
    if (d != 1 && d != 2) return fail("Invalid device (1 lift, 2 transport)");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    SectionEntry* e = FindEntry(c, (uint32_t)sid); if (!e || !e->section) return fail("Unknown section");
    uint8_t* dev = Rd<uint8_t*>(e->section, section::BrakeDev + 8 * d);
    if (!dev) return fail("Section has no such device");
    auto keep = [](double v, double old) { return (v >= 0 && v < 1000) ? v : old; };
    sp = keep(sp, Rd<double>(dev, device::Speed));
    ac = keep(ac, Rd<double>(dev, device::Accel));
    de = keep(de, Rd<double>(dev, device::Decel));
    if (ac <= 0 || de <= 0) return fail("Acceleration and deceleration must be > 0");
    VCall<void(*)(void*, double, double, double)>(dev, device::VF_SetParams)(dev, sp, ac, de);
    return R_OK;
  }

  case Q_GET_COASTERS_JSON: {
    Locked L; void* park = GetPark(&err); if (!park) return fail(err);
    std::string j = "[";
    for (int i = 0, n = CoasterCount(park); i < n; ++i) { if (i) j += ","; j += CoasterJson(park, i); }
    j += "]"; w.str(j); return R_STRING;
  }

  case Q_GET_SECTIONS_JSON: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    w.str(SectionsJson(c)); return R_STRING;
  }

  case Q_GET_SWITCHES_JSON: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    w.str(SwitchesJson(c)); return R_STRING;
  }

  case Q_GET_STATIONS_JSON: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    w.str(StationsJson(c)); return R_STRING;
  }

  case Q_GET_COASTER_INFO: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    w.str("{\"coaster\":" + CoasterJson(GetPark(nullptr), ci) + ",\"sections\":" + SectionsJson(c) +
          ",\"stations\":" + StationsJson(c) + ",\"specialTracks\":" + SwitchesJson(c) + ",\"tracks\":" + TracksJson(c) + "}");
    return R_STRING;
  }

  case Q_GET_STATION_STATES: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    StationEntry* se = Rd<StationEntry*>(c, coaster::StationsBegin);
    int n = StationCount(c);
    RowsPrune();
    w.u16((uint16_t)n);
    for (int i = 0; i < n; ++i) {
      StationStatus s; if (se[i].station) s = GetStationStatus(c, se[i].station);
      w.u32(s.flags); w.u8(s.state); w.u8((uint8_t)s.rowsOpen); w.u16((uint16_t)s.seats.seats);
    }
    return R_STATION_STATES;
  }

  case Q_GET_SWITCH_STATES: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    int n = SpecialCount(c);
    w.u16((uint16_t)n);
    for (int i = 0; i < n; ++i) {
      uint8_t* t = Rd<uint8_t**>(c, coaster::SpecialBegin)[i];
      int cur = Rd<int32_t>(t, special::CurrentDir);
      w.u8((uint8_t)(int8_t)cur); w.u8((uint8_t)(int8_t)Rd<int32_t>(t, special::TargetDir));
      w.u8((uint8_t)VCall<int(*)(void*)>(t, special::VF_DirCount)(t));
      w.u8((cur < 0 ? 1 : 0) | ((Rd<uint8_t>(t, special::Switchable1) || Rd<uint8_t>(t, special::Switchable2)) ? 2 : 0) |
           (Rd<uint8_t>(t, special::ManualAllowed) ? 4 : 0) |
           (SpecialType(RttiName(t)) == "transferTable" ? 8 : 0));
    }
    return R_SWITCH_STATES;
  }

  case Q_GET_SENSORS_JSON: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; if (!GetPark(&err)) return fail(err);
    w.str(SensorsJson(ci)); return R_STRING;
  }

  case Q_GET_SENSOR_STATES: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; if (!GetPark(&err)) return fail(err);
    SensorStates(ci, w); return R_SENSOR_STATES;
  }

  case Q_BLOCK_SET: {
    int ci = r.i32(), sid = r.i32(), cmd = r.i32(), prm = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    SectionEntry* e = FindEntry(c, (uint32_t)sid); if (!e || !e->node) return fail("Unknown block");
    bool ok = VCall<bool(*)(void*, int, unsigned)>(e->node, node::VF_SetState)(e->node, cmd, (unsigned)prm);
    Log("BlockSet c=%d id=%d cmd=%d p=%d -> %d", ci, sid, cmd, prm, ok);
    return intReply(ok);
  }

  case Q_STATION_OP_CHECKED: {
    int ci = r.i32(), si = r.i32(), op = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    if (si < 0 || si >= StationCount(c)) return fail("Invalid station index");
    uint8_t* st = (uint8_t*)G.StationAt(c, si);
    if (!st) return fail("Invalid station");
    RowsPrune();
    { bool rok; std::string rwhy;
      if (RowsStationOp(st, op, &rok, &rwhy)) { Log("StationOp c=%d st=%d op=%d -> rows %d", ci, si, op, rok); if (!rok) return fail(rwhy); return R_OK; } }
    std::string why = StationOpRefusal(c, st, op);
    if (!why.empty()) return fail(why);
    bool ok = G.StationOp(st, op);
    Log("StationOp c=%d st=%d op=%d -> %d", ci, si, op, ok);
    if (!ok && op > 1) return fail("Refused by the game");
    return R_OK;
  }

  case Q_GET_BLOCK_STATES: {   // compact poll for PLCs
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    size_t n; SectionEntry* e = Entries(c, &n);
    w.u8(Rd<uint8_t>(c, coaster::BlockMode)); w.u8(Rd<uint8_t>(c, coaster::EStop));
    w.u16((uint16_t)n);
    for (size_t i = 0; i < n; ++i) {
      uint8_t* s = e[i].section; void* nd = e[i].node;
      uint8_t flags = 0, lamp = 0;
      if (nd) {
        VCall<void(*)(void*, void*, uint8_t*)>(nd, node::VF_GetStateText)(nd, nullptr, &lamp);
        if (CanAdvance(nd, false)) flags |= 0x01;
        if (CanAdvance(nd, true))  flags |= 0x02;
        if (Rd<uint8_t>(nd, node::IsBlock)) flags |= 0x04;
        if (IsScriptedNode(nd)) flags |= 0x08;
      }
      if (s && Rd<void*>(s, section::BrakeDev))     flags |= 0x10;
      if (s && Rd<void*>(s, section::LiftDev))      flags |= 0x20;
      if (s && Rd<void*>(s, section::TransportDev)) flags |= 0x40;
      uint32_t trainMask = (uint32_t)SGetI(c, e[i].id, SG_TRAIN_MASK);
      w.u32(e[i].id);
      w.u8(nd ? Rd<uint8_t>(nd, node::State) : 0xff);
      w.u8(nd ? Rd<uint8_t>(nd, node::TrainCount) : 0);
      w.u8(lamp);
      w.u8(flags);
      w.u8(s ? Rd<uint8_t>(s, section::BrakeMode) : 0xff);
      w.u8(s ? Rd<uint8_t>(s, section::LiftMode) : 0xff);
      w.u8(s ? Rd<uint8_t>(s, section::TransportMode) : 0xff);
      w.u8(0);
      w.u32(trainMask);
    }
    return R_BLOCK_STATES;
  }

  case Q_SECTION_SET: {
    int ci = r.i32(), sid = r.i32(), cmd = r.i32(), prm = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    void* bs = Rd<void*>(c, coaster::BlockSystem); if (!bs) return fail("No block system");
    bool ok = G.SectionSet(bs, (uint32_t)sid, cmd, prm);
    Log("SectionSet c=%d id=%d cmd=%d p=%d -> %d", ci, sid, cmd, prm, ok);
    return intReply(ok);
  }

  case Q_SECTION_GET: {
    int ci = r.i32(), sid = r.i32(), cmd = r.i32(); double in = r.f64(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    void* bs = Rd<void*>(c, coaster::BlockSystem); if (!bs) return fail("No block system");
    SectionGetResult res{}; res.d = in;
    bool ok = G.SectionGet(bs, (uint32_t)sid, cmd, &res);
    w.i32(ok); w.i32(res.i); w.f64(res.d);
    return R_SECTION_GET;
  }

  case Q_BLOCK_ADVANCE: {
    int ci = r.i32(), sid = r.i32(), dir = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    SectionEntry* e = FindEntry(c, (uint32_t)sid); if (!e || !e->node) return fail("Unknown block");
    bool ok;
    if (IsScriptedNode(e->node))   // scripted: same as clicking the panel's Advance button -> event 6/7 for the controller
      ok = VCall<int(*)(void*, int)>(e->node, node::VF_SemiManual)(e->node, dir == 2 ? 5 : 4) != 0;
    else
      ok = VCall<bool(*)(void*, int, unsigned)>(e->node, node::VF_SetState)(e->node, 7, dir == 2 ? 2u : 1u);
    Log("BlockAdvance c=%d id=%d dir=%d -> %d", ci, sid, dir, ok);
    return intReply(ok);
  }

  case Q_DEVICE_GET: {
    int ci = r.i32(), sid = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    SectionEntry* e = FindEntry(c, (uint32_t)sid); if (!e || !e->section) return fail("Unknown section");
    std::string j = "{";
    const char* nm[3] = {"brake", "lift", "transport"};
    for (int d = 0; d < 3; ++d) {
      uint8_t* dev = Rd<uint8_t*>(e->section, section::BrakeDev + 8 * d);
      char b[160];
      snprintf(b, sizeof b, "%s\"%s\":{\"present\":%d,\"state\":%d,\"type\":\"%s\"}", d ? "," : "", nm[d], dev != nullptr,
               dev ? Rd<uint8_t>(dev, device::State) : -1, dev ? JsonEsc(RttiName(dev)).c_str() : "");
      j += b;
    }
    w.str(j + "}"); return R_STRING;
  }

  case Q_DEVICE_SET: {
    int ci = r.i32(), sid = r.i32(), d = r.i32(), val = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    SectionEntry* e = FindEntry(c, (uint32_t)sid); if (!e || !e->section) return fail("Unknown section");
    if (d < 0 || d > 2) return fail("Invalid device (0 brake, 1 lift, 2 transport)");
    // Same guards as the game's own full-manual control panel (NLDeviceOnOffControl / NLDeviceBwdOffFwdControl)
    if (Rd<uint8_t>(c, coaster::Busy)) return fail("Pause is activated");
    if (Rd<uint8_t>(c, coaster::EStop)) return fail("Emergency Stop is activated");
    if (!Rd<uint8_t>(c, coaster::Ready)) return fail("Coaster needs to be reset");
    if (Rd<uint8_t>(c, coaster::BlockMode) != 3) return fail("Coaster needs to be in Full Manual Mode");
    uint8_t* dev = Rd<uint8_t*>(e->section, section::BrakeDev + 8 * d);
    if (!dev) return fail("Section has no such device");
    if (val < 0 || val > (d == 2 ? 2 : 1)) return fail("Invalid value");
    VCall<void(*)(void*, uint8_t)>(dev, device::VF_SetState)(dev, (uint8_t)val);
    Log("DeviceSet c=%d id=%d dev=%d val=%d -> %d", ci, sid, d, val, Rd<uint8_t>(dev, device::State));
    return intReply(Rd<uint8_t>(dev, device::State) == val);
  }

  case Q_TRAIN_LASH: {
    int ci = r.i32(), ti = r.i32(), on = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    uint8_t* t = nullptr; uint8_t** tb = Rd<uint8_t**>(c, coaster::TrainsBegin);
    for (int i = 0, n = TrainCount(c); i < n; ++i) if (Rd<uint8_t>(tb[i], train::Index) == ti) t = tb[i];
    if (!t) return fail("Invalid train index");
    // Same writes as the game's train command fn (cmd 0 = lash: also clears 'Move Train'; cmd 1 = unlash)
    if (on) { t[train::Lashed] = 1; t[train::MoveTrain] = 0; }
    else t[train::Lashed] = 0;
    Log("TrainLash c=%d t=%d on=%d", ci, ti, on);
    return R_OK;
  }

  case Q_GET_TRAINS_JSON: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    w.str(TrainsJson(c)); return R_STRING;
  }

  case Q_GET_SECTION_DETAIL: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    size_t n; SectionEntry* e = Entries(c, &n);
    w.u8(Rd<uint8_t>(c, coaster::OperationMode)); w.u8(Rd<uint8_t>(c, coaster::BlockMode));
    w.u8(Rd<uint8_t>(c, coaster::EStop)); w.u8(0);
    w.u16((uint16_t)n);
    for (size_t i = 0; i < n; ++i) {
      uint32_t id = e[i].id; void* nd = e[i].node;
      static const struct { int q; uint16_t bit; } qs[] = {
        {SG_BEFORE_CENTER, 1 << 0}, {SG_BEHIND_CENTER, 1 << 1}, {SG_BEFORE_BRAKE_TRIG, 1 << 2}, {SG_BEHIND_BRAKE_TRIG, 1 << 3},
        {SG_BEFORE_LIFT_TRIG, 1 << 4}, {SG_BEHIND_LIFT_TRIG, 1 << 5}, {SG_STATION_WAIT_CLEAR, 1 << 7},
        {SG_STATION_WAIT_ADVANCE, 1 << 8}, {SG_IS_STATION, 1 << 9}};
      uint16_t f = 0;
      for (auto& q : qs) if (SGetI(c, id, q.q)) f |= q.bit;
      if (SGetD(c, id, SG_BRAKES_ON) > 0.5) f |= 1 << 6;
      bool scr = IsScriptedNode(nd);
      if (scr) {
        if (Rd<uint8_t>(nd, node::FwdVisible)) f |= 1 << 10;
        if (Rd<uint8_t>(nd, node::BwdVisible)) f |= 1 << 12;
        f |= 1 << 14;
      }
      if (CanAdvance(nd, false)) f |= 1 << 11;
      if (CanAdvance(nd, true))  f |= 1 << 13;
      uint32_t mask = (uint32_t)SGetI(c, id, SG_TRAIN_MASK);
      if (mask) f |= 1 << 15;
      int tIdx = SGetI(c, id, SG_TRAIN_INDEX, -1);
      if (tIdx < 0 && mask) for (int b = 0; b < 32; ++b) if (mask >> b & 1) { tIdx = b; break; }
      w.u32(id); w.u16(f);
      w.u8((uint8_t)(int8_t)tIdx);
      w.u8(nd ? Rd<uint8_t>(nd, node::State) : 0xff);
      w.i32(scr ? Rd<int32_t>(nd, node::UserState) : -1);
      float ls = (float)SGetD(c, id, SG_LIFT_SPEED), ts = (float)SGetD(c, id, SG_TRANSPORT_SPEED);
      uint32_t u; memcpy(&u, &ls, 4); w.u32(u); memcpy(&u, &ts, 4); w.u32(u);
    }
    return R_SECTION_DETAIL;
  }

  case Q_REGISTER_STATE: case Q_SET_USER_STATE: {
    int ci = r.i32(), sid = r.i32(), state = r.i32(), lamp = 0; std::string text;
    if (id == Q_REGISTER_STATE) { lamp = r.i32(); text = r.rest(); }
    if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    if (Rd<uint8_t>(c, coaster::OperationMode) != 2) return fail("Coaster is not in scripted operation mode");
    SectionEntry* e = FindEntry(c, (uint32_t)sid);
    if (!e || !e->node || !Rd<uint8_t>(e->node, node::IsBlock) || !IsScriptedNode(e->node)) return fail("Not a scripted block section");
    if (id == Q_REGISTER_STATE)
      VCall<void(*)(void*, int, const char*, uint8_t)>(e->node, node::VF_RegisterState)(e->node, state, text.c_str(),
                                                                                         (uint8_t)(lamp < 0 || lamp > 2 ? 0 : lamp));
    else
      VCall<void(*)(void*, int)>(e->node, node::VF_SetUserState)(e->node, state);
    return R_OK;
  }

  case Q_GET_EVENTS: {
    uint32_t since = r.u32(); if (r.bad) return fail("Invalid message");
    Locked L; void* park = GetPark(nullptr);
    w.str(EventsJson(park, since)); return R_STRING;
  }

  case Q_SET_SWITCH: {
    int ci = r.i32(), idx = r.i32(), dir = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    if (idx < 0 || idx >= SpecialCount(c)) return fail("Invalid special track index");
    uint8_t* t = Rd<uint8_t**>(c, coaster::SpecialBegin)[idx];
    // Same guards as the script API (setSwitchDirection / setManualSwitchDirection)
    bool scripted = Rd<uint8_t>(c, coaster::OperationMode) == 2;
    uint8_t bm = Rd<uint8_t>(c, coaster::BlockMode);
    bool manualOk = !Rd<uint8_t>(c, coaster::EStop) && Rd<uint8_t>(c, coaster::Ready) && (bm == 2 || bm == 3)
                    && !Rd<uint8_t>(c, coaster::Busy) && Rd<uint8_t>(t, special::ManualAllowed);
    if (!scripted && !manualOk) return fail("Switch change not allowed (needs scripted mode, or manual/full-manual block mode with manual switching enabled)");
    if (Rd<int32_t>(t, special::CurrentDir) == dir) return intReply(1);
    if (!(Rd<uint8_t>(t, special::Switchable1) || Rd<uint8_t>(t, special::Switchable2))) return intReply(0);
    int dirs = VCall<int(*)(void*)>(t, special::VF_DirCount)(t);
    if (dir < 0 || dir >= dirs) return fail("Invalid direction");
    G.SwitchSet(t, dir, 0);
    Log("SetSwitch c=%d idx=%d dir=%d", ci, idx, dir);
    return intReply(1);
  }

  case Q_SET_BLOCK_MODE: {
    int ci = r.i32(), mode = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    bool can = false; char m = 0;
    // MSVC std::string the game can write its refusal reason into (empty, SSO, capacity 15).
    // If the game grows it onto the heap we deliberately leak it (a few bytes per refusal).
    alignas(8) uint8_t reason[32] = {}; reason[0x18] = 15;
    if (mode == 0)      { can = G.CanAutoOrManual(c, reason, 0, 1); m = 1; }
    else if (mode == 1) { can = G.CanAutoOrManual(c, reason, 1, 1); m = 2; }
    else if (mode == 2) { can = G.CanFullManual(c, reason, 1);       m = 3; }
    else return fail("Invalid mode");
    if (can) G.SetBlockMode(c, m);
    std::string why = ReadStdString(reason);
    Log("SetBlockMode c=%d mode=%d -> %d %s", ci, mode, can, why.c_str());
    if (!can && !why.empty()) return fail(why);
    return intReply(can);
  }

  case Q_SET_ESTOP: {
    int ci = r.i32(); uint8_t on = r.u8(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    G.SetEStop(c, on ? 1 : 0);
    return R_OK;
  }

  case Q_RESET_COASTER: {
    int ci = r.i32(); if (r.bad) return fail("Invalid message");
    if (!G.resetPending) return fail("Reset not available in this game version");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    c[coaster::ResetRequest] = 1;
    __atomic_exchange_n(G.resetPending, (char)1, __ATOMIC_SEQ_CST);
    Log("ResetCoaster c=%d", ci);
    return R_OK;
  }

  case Q_STATION_OP: {
    int ci = r.i32(), si = r.i32(), op = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    if (si < 0 || si >= StationCount(c)) return fail("Invalid station index");
    void* st = G.StationAt(c, si);
    RowsPrune();
    { bool rok; std::string rwhy;
      if (st && RowsStationOp((uint8_t*)st, op, &rok, &rwhy)) { Log("StationOp c=%d st=%d op=%d -> rows %d", ci, si, op, rok); return intReply(rok); } }
    bool ok = st && G.StationOp(st, op);
    Log("StationOp c=%d st=%d op=%d -> %d", ci, si, op, ok);
    return intReply(ok);
  }

  case Q_GET_ROWS_JSON: {
    int ci = r.i32(), si = r.i32(); if (r.bad) return fail("Invalid message");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    if (si < 0 || si >= StationCount(c)) return fail("Invalid station index");
    uint8_t* st = (uint8_t*)G.StationAt(c, si); if (!st) return fail("Invalid station");
    RowsPrune();
    w.str(RowsJson(c, si, st)); return R_STRING;
  }

  case Q_ROW_RESTRAINT: {
    int ci = r.i32(), si = r.i32(), row = r.i32(), open = r.i32(); if (r.bad) return fail("Invalid message");
    if (!RW.ok) return fail("Per-row restraints unavailable (hook not installed - see log)");
    Locked L; uint8_t* c = GetCoaster(ci, &err); if (!c) return fail(err);
    if (si < 0 || si >= StationCount(c)) return fail("Invalid station index");
    uint8_t* st = (uint8_t*)G.StationAt(c, si); if (!st) return fail("Invalid station");
    RowsPrune();
    uint8_t* tr = Rd<uint8_t*>(st, station::Train);
    if (!tr) return fail("No train in the station");
    if (open) {
      StationStatus s = GetStationStatus(c, st);
      if (s.flags & SF_ESTOP) return fail("E-stop is active");
      if (!(s.flags & SF_MANUAL)) return fail("Station must be in manual dispatch mode (op 0)");
      if (!(s.flags & SF_TRAIN_READY)) return fail("No train stopped in the station");
      if (s.harnessMoving) return fail("Restraints are moving");
    }
    std::vector<CarInfo> rows; TrainRowCars(tr, rows);
    if (rows.empty()) return fail("This train has no animated restraints");
    if (row < 0 || row > (int)rows.size()) return fail("Invalid row (1.." + std::to_string(rows.size()) + ", 0 = all)");
    for (int i = 0; i < (int)rows.size(); ++i) if (row == 0 || row == i + 1) RowCommand(tr, rows[i].car, open != 0);
    Log("RowRestraint c=%d st=%d row=%d open=%d (%zu rows)", ci, si, row, open, rows.size());
    return R_OK;
  }
  }
  w.str("Unknown message"); return R_ERROR;
}

// ---------------------------------------------------------------- server
static std::atomic<bool> g_run{true};
static SOCKET g_listen = INVALID_SOCKET;

static DWORD WINAPI ClientThread(LPVOID p) {
  SOCKET s = (SOCKET)(uintptr_t)p;
  std::vector<uint8_t> in; uint8_t buf[4096];
  while (g_run) {
    int n = recv(s, (char*)buf, sizeof buf, 0);
    if (n <= 0) break;
    in.insert(in.end(), buf, buf + n);
    std::vector<uint8_t> out;
    while (in.size() >= 10) {
      if (in[0] != 'N') { in.clear(); break; }
      uint16_t id = (in[1] << 8) | in[2];
      uint32_t req = (in[3] << 24) | (in[4] << 16) | (in[5] << 8) | in[6];
      uint16_t sz = (in[7] << 8) | in[8];
      if (in.size() < (size_t)sz + 10) break;
      if (in[9 + sz] != 'L') { in.clear(); break; }
      Reader r{in.data() + 9, sz};
      Writer w;
      uint16_t rid = Handle(id, r, w);
      if (w.b.size() > 0xffff) { w.b.clear(); w.str("Reply larger than 64 KB - use the per-part queries"); rid = R_ERROR; }
      Frame(out, rid, req, w.b);
      in.erase(in.begin(), in.begin() + 10 + sz);
    }
    if (!out.empty() && send(s, (const char*)out.data(), (int)out.size(), 0) <= 0) break;
  }
  closesocket(s);
  return 0;
}

static int g_port = 15152;

static DWORD WINAPI ServerThread(LPVOID) {
  WSADATA wd; WSAStartup(MAKEWORD(2, 2), &wd);
  int port = g_port;
  if (const char* e = getenv("NL2BRIDGE_PORT")) port = atoi(e);
  g_listen = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  BOOL yes = TRUE; setsockopt(g_listen, SOL_SOCKET, SO_REUSEADDR, (const char*)&yes, sizeof yes);
  sockaddr_in a{}; a.sin_family = AF_INET; a.sin_port = htons((u_short)port); a.sin_addr.s_addr = INADDR_ANY;
  if (bind(g_listen, (sockaddr*)&a, sizeof a) != 0 || listen(g_listen, 4) != 0) {
    Log("server: bind/listen on %d failed (%d)", port, WSAGetLastError()); return 0;
  }
  Log("server: listening on port %d", port);
  while (g_run) {
    SOCKET c = accept(g_listen, nullptr, nullptr);
    if (c == INVALID_SOCKET) break;
    BOOL nd = TRUE; setsockopt(c, IPPROTO_TCP, TCP_NODELAY, (const char*)&nd, sizeof nd);
    Log("server: client connected");
    CloseHandle(CreateThread(nullptr, 0, ClientThread, (LPVOID)(uintptr_t)c, 0, nullptr));
  }
  return 0;
}

static DWORD WINAPI InitThread(LPVOID self) {
  // <dll>.log next to the DLL; optional <dll>.port containing a TCP port number (default 15152)
  wchar_t path[MAX_PATH]; GetModuleFileNameW((HMODULE)self, path, MAX_PATH);
  wchar_t* dot = wcsrchr(path, L'.'); if (!dot) dot = path + wcslen(path);
  wcscpy(dot, L".port");
  if (FILE* pf = _wfopen(path, L"r")) { int p = 0; if (fscanf(pf, "%d", &p) == 1 && p > 0 && p < 65536) g_port = p; fclose(pf); }
  wcscpy(dot, L".log");
  g_log = _wfopen(path, L"a");
  Log("NL2Bridge 1.2.0 starting (API 8), port %d", g_port);
  ResolveAll();
  Log(G.ok ? "all symbols resolved" : "WARNING: some symbols missing - requests will be refused");
  InitSensors(G.sensorSyms);
  InitRows();
  ServerThread(nullptr);
  return 0;
}

BOOL WINAPI DllMain(HINSTANCE h, DWORD reason, LPVOID) {
  if (reason == DLL_PROCESS_ATTACH) {
    DisableThreadLibraryCalls(h);
    CloseHandle(CreateThread(nullptr, 0, InitThread, h, 0, nullptr));
  } else if (reason == DLL_PROCESS_DETACH) {
    g_run = false; if (g_listen != INVALID_SOCKET) closesocket(g_listen);
  }
  return TRUE;
}
