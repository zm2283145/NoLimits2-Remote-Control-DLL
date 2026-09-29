// sensors.inc - track trigger / sensor support for NL2Bridge (included by nl2bridge.cpp)
//
// Every sensor (Red's Track Sensors pack, any other scripted sensor, or a trigger placed in the editor)
// is an engine NLTrackTrigger sitting on a coaster track. Two small hooks give us everything:
//
//  1. EventsConsumed (per coaster, once per frame): the coaster keeps a list of this frame's trigger
//     events {trigger, train, ?, type}. Right before the game erases the ones it has handed to scripts
//     we read them, so we see every "train entered / left trigger" without missing any.
//  2. NatAddTrigger (script native, runs when a sensor script starts): we look up the scene object
//     that owns the script and remember its Name ("Lift Enter") for the trigger it creates.
//
// Hooks are plain 14-byte absolute jumps; both prologues are position-independent (checked below).
#include <map>

typedef void   (*FnEventsConsumed)(uint8_t* coaster);
typedef void   (*FnNatAddTrigger)(void* vm);
typedef void** (*FnVmStorageGet)(void** vmHolder, void** out, const void* key);
typedef uint8_t* (*FnEntityObjForId)(uint32_t entityId, void* vm);
typedef void*  (*FnRTDynamicCast)(void*, long, void*, void*, int);

struct SensorInfo {
  std::string name;
  uint32_t id = 0;
  void* coaster = nullptr;
  uint32_t trainMask = 0;      // trains currently over the sensor (bit n = train n)
  int lastTrain = -1;
  uint32_t enters = 0, leaves = 0, otherEvents = 0;
  ULONGLONG lastChangeMs = 0;
};

// Coaster event as seen by our hook (type 1/2 sensor enter/leave, 3/4/5 block mode -> auto/manual/full manual,
// 6/7 Advance FWD/BWD pressed on a scripted block, a = sectionId)
struct BridgeEvent { uint32_t seq; void* coaster; void* trigger; uint32_t type, a; int train; ULONGLONG ms; };
#include <deque>

static struct {
  bool ok = false;
  FnVmStorageGet VmGet = nullptr; FnEntityObjForId EntObj = nullptr; FnRTDynamicCast DynCast = nullptr;
  const void* keyTriggers = nullptr; const void* keyParent = nullptr;
  void* tdRefObject = nullptr; void* tdScriptEntity = nullptr; void* tdScriptTriggers = nullptr;
  void* vtTriggerPoint = nullptr; void* vtTrigger = nullptr;
  FnEventsConsumed origEvents = nullptr; FnNatAddTrigger origAdd = nullptr;
  CRITICAL_SECTION cs;
  std::map<void*, SensorInfo> byTrigger;     // NLTrackTrigger* -> live state
  std::map<void*, std::string> names;        // NLTrackTrigger* -> owning scene object name
  std::deque<BridgeEvent> events;            // last 2000 coaster events
  uint32_t evSeq = 0;
  int loggedEvents = 0;
} S;

static void* FindTypeDescriptor(const char* mangled) {
  size_t n = strlen(mangled) + 1;
  for (size_t off = 0x1000; off + n < G.size; ++off)
    if (memcmp(G.base + off, mangled, n) == 0) return G.base + off - 0x10;
  return nullptr;
}

// Primary vtable of the class whose RTTI type descriptor is td (COL: sig=1, offset=0, pTD, ..., pSelf)
static void* FindVtable(void* td) {
  if (!td) return nullptr;
  uint32_t tdRva = (uint32_t)((uint8_t*)td - G.base);
  for (size_t col = 0x1000; col + 24 < G.size; col += 4) {
    if (Rd<uint32_t>(G.base, (int)col) != 1 || Rd<uint32_t>(G.base, (int)col + 4) != 0 ||
        Rd<uint32_t>(G.base, (int)col + 12) != tdRva || Rd<uint32_t>(G.base, (int)col + 20) != (uint32_t)col) continue;
    uint64_t colVa = (uint64_t)(G.base + col);
    for (size_t q = 0x1000; q + 16 < G.size; q += 8)
      if (Rd<uint64_t>(G.base, (int)q) == colVa) return G.base + q + 8;
  }
  return nullptr;
}

// Fault-tolerant reads: game lists can hold objects we do not fully understand, so never trust a pointer
static bool SafeRead(const void* p, void* out, size_t n) {
  SIZE_T got = 0;
  return p && ReadProcessMemory(GetCurrentProcess(), p, out, n, &got) && got == n;
}
template <class T> static bool SRd(const void* p, int off, T& v) { return p && SafeRead((const uint8_t*)p + off, &v, sizeof(T)); }
static std::string SafeStdString(const void* p) {
  size_t size = 0, cap = 0;
  if (!SRd(p, 0x10, size) || !SRd(p, 0x18, cap) || size > 256 || cap < size) return "";
  const char* data = (const char*)p;
  if (cap > 15 && !SRd(p, 0, data)) return "";
  std::string s(size, '\0');
  if (size && !SafeRead(data, &s[0], size)) return "";
  for (char ch : s) if ((unsigned char)ch < 0x20) return "";
  return s;
}

// len = number of prologue bytes to relocate (>= 14, whole position-independent instructions)
static void* InstallHook(uint8_t* target, const uint8_t* expect, void* hook, size_t len = 14) {
  if (target && IsBridgeHook(target)) {        // another bridge build (loaded earlier this session) hooked it: chain
    void* prev; memcpy(&prev, target + 6, 8);
    DWORD old; VirtualProtect(target, 14, PAGE_EXECUTE_READWRITE, &old);
    memcpy(target + 6, &hook, 8);
    VirtualProtect(target, 14, old, &old);
    FlushInstructionCache(GetCurrentProcess(), target, 14);
    Log("  hook @%p: chained after earlier bridge hook %p", target, prev);
    return prev;
  }
  if (!target || len < 14 || len > 32 || memcmp(target, expect, len) != 0) { Log("  hook @%p: prologue mismatch - not hooked", target); return nullptr; }
  uint8_t* t = (uint8_t*)VirtualAlloc(nullptr, 64, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
  if (!t) return nullptr;
  memcpy(t, target, len);                                     // relocated prologue
  t[len] = 0xFF; t[len + 1] = 0x25; memset(t + len + 2, 0, 4); // jmp [rip+0]
  uint64_t back = (uint64_t)(target + len); memcpy(t + len + 6, &back, 8);
  uint8_t patch[32]; memset(patch, 0x90, sizeof patch);
  patch[0] = 0xFF; patch[1] = 0x25; memset(patch + 2, 0, 4);
  uint64_t dst = (uint64_t)hook; memcpy(patch + 6, &dst, 8);
  DWORD old; VirtualProtect(target, len, PAGE_EXECUTE_READWRITE, &old);
  memcpy(target, patch, len);
  VirtualProtect(target, len, old, &old);
  FlushInstructionCache(GetCurrentProcess(), target, len);
  return t;
}

static void ReleaseRef(void* obj) { if (obj) VCall<void(*)(void*)>(obj, 0x18)(obj); }

// Script trigger container of a script VM: returns count and (optionally) the last NLTrackTriggerPoint*
static size_t ScriptTriggers(void* vm, void** last) {
  void* holder = vm; void* c = nullptr;
  S.VmGet(&holder, &c, S.keyTriggers);
  if (!c) return 0;
  size_t n = 0;
  void* tt = S.DynCast(c, 0, S.tdRefObject, S.tdScriptTriggers, 0);
  if (tt) {
    void** b = Rd<void**>(tt, trig::ScriptTriggersVec), **e = Rd<void**>(tt, trig::ScriptTriggersVec + 8);
    n = (size_t)(e - b);
    if (last && n) *last = e[-1];
  }
  ReleaseRef(c);
  return n;
}

static std::string OwnerName(void* vm) {
  void* holder = vm; void* obj = nullptr;
  S.VmGet(&holder, &obj, S.keyParent);
  std::string name;
  if (obj) {
    void* ent = S.DynCast(obj, 0, S.tdRefObject, S.tdScriptEntity, 0);
    if (ent) {
      uint32_t eid = Rd<uint32_t>(ent, trig::ScriptEntityId);
      uint8_t* sco = S.EntObj(eid, vm);
      if (sco) name = SafeStdString(sco + trig::SceneObjectName);
    }
    ReleaseRef(obj);
  }
  return name;
}

static void HookAddTrigger(void* vm) {
  std::string name; size_t before = 0;
  if (S.ok) { name = OwnerName(vm); before = ScriptTriggers(vm, nullptr); }
  S.origAdd(vm);
  if (!S.ok) return;
  void* lastPoint = nullptr;
  size_t after = ScriptTriggers(vm, &lastPoint);
  if (after > before && lastPoint) {
    void* trigger = nullptr; void* vt = nullptr;
    if (SRd(lastPoint, trig::PointTrigger, trigger) && SRd(trigger, 0, vt) && vt == S.vtTrigger) {
      EnterCriticalSection(&S.cs);
      S.names[trigger] = name;
      S.byTrigger.erase(trigger);            // new park / reload: start fresh
      LeaveCriticalSection(&S.cs);
      Log("sensor registered: \"%s\" trigger=%p id=%u", name.c_str(), trigger, Rd<uint32_t>(trigger, trig::TriggerId));
    }
  }
}

static void HookEvents(uint8_t* coaster) {
  size_t n = Rd<size_t>(coaster, trig::CoasterEventsConsumed);
  if (n) {
    CoasterEvent* b = Rd<CoasterEvent*>(coaster, trig::CoasterEventsBegin);
    CoasterEvent* e = Rd<CoasterEvent*>(coaster, trig::CoasterEventsEnd);
    if (b && e > b) {
      if (n > (size_t)(e - b)) n = (size_t)(e - b);
      ULONGLONG now = GetTickCount64();
      EnterCriticalSection(&S.cs);
      for (size_t i = 0; i < n; ++i) {
        const CoasterEvent& ev = b[i];
        if (ev.type >= 1 && ev.type <= 7) {
          BridgeEvent be{++S.evSeq, coaster, ev.trigger, ev.type, ev.a, -1, now};
          if (ev.train && (ev.type == 1 || ev.type == 2)) { uint8_t ti; if (SRd(ev.train, trig::TrainIndex, ti)) be.train = ti; }
          S.events.push_back(be);
          if (S.events.size() > 2000) S.events.pop_front();
        }
        if (!ev.trigger || (ev.type != 1 && ev.type != 2)) continue;
        SensorInfo& si = S.byTrigger[ev.trigger];
        si.coaster = coaster;
        si.id = Rd<uint32_t>(ev.trigger, trig::TriggerId);
        int t = ev.train ? Rd<uint8_t>(ev.train, trig::TrainIndex) : -1;
        if (ev.type == 1) { si.enters++; if (t >= 0 && t < 32) si.trainMask |= 1u << t; }
        else              { si.leaves++; if (t >= 0 && t < 32) si.trainMask &= ~(1u << t); }
        si.lastTrain = t; si.lastChangeMs = now;
        if (S.loggedEvents < 40) {
          S.loggedEvents++;
          auto nm = S.names.find(ev.trigger);
          Log("trigger event type=%u id=%u train=%d a=%u name=\"%s\"", ev.type, si.id, t, ev.a,
              nm != S.names.end() ? nm->second.c_str() : "");
        }
      }
      LeaveCriticalSection(&S.cs);
    }
  }
  S.origEvents(coaster);
}

static void InitSensors(uint8_t** r) {
  InitializeCriticalSection(&S.cs);
  // r[0]=NatAddTrigger r[1]=EventsConsumed r[2]=VmStorageGet r[3]=EntityObjForId r[4]=NatParentEntity
  if (!r[0] || !r[1] || !r[2] || !r[3] || !r[4]) { Log("sensors: disabled (signatures missing)"); return; }
  S.VmGet = (FnVmStorageGet)r[2];
  S.EntObj = (FnEntityObjForId)r[3];
  S.keyTriggers = RipTarget(r[0], ANCHOR_TRIGKEY_DISP_OFF);
  S.keyParent   = RipTarget(r[4], ANCHOR_PARENTKEY_DISP_OFF);
  HMODULE vcr = GetModuleHandleW(L"VCRUNTIME140.dll");
  S.DynCast = vcr ? (FnRTDynamicCast)GetProcAddress(vcr, "__RTDynamicCast") : nullptr;
  S.tdRefObject      = FindTypeDescriptor(".?AVRefObject@nl3d@@");
  S.tdScriptEntity   = FindTypeDescriptor(".?AVNLScriptEntity@nolimits@@");
  S.tdScriptTriggers = FindTypeDescriptor(".?AVNLScriptTrackTriggers@nolimits@@");
  Log("  sensors: keyTriggers=%p keyParent=%p dyncast=%p td=%p/%p/%p", S.keyTriggers, S.keyParent, S.DynCast,
      S.tdRefObject, S.tdScriptEntity, S.tdScriptTriggers);
  if (!S.DynCast || !S.tdRefObject || !S.tdScriptEntity || !S.tdScriptTriggers) { Log("sensors: disabled (RTTI)"); return; }
  S.vtTriggerPoint = FindVtable(FindTypeDescriptor(".?AVNLTrackTriggerPoint@nolimits@@"));
  S.vtTrigger      = FindVtable(FindTypeDescriptor(".?AVNLTrackTrigger@nolimits@@"));
  Log("  sensors: vtables point=%p trigger=%p", S.vtTriggerPoint, S.vtTrigger);
  if (!S.vtTriggerPoint || !S.vtTrigger) { Log("sensors: disabled (vtables)"); return; }
  S.ok = true;
  S.origEvents = (FnEventsConsumed)InstallHook(r[1], HOOK_EVENTS_PROLOGUE, (void*)&HookEvents);
  S.origAdd    = (FnNatAddTrigger)InstallHook(r[0], HOOK_ADD_TRIGGER_PROLOGUE, (void*)&HookAddTrigger);
  Log("  sensors: event hook %s, name hook %s", S.origEvents ? "ON" : "FAILED", S.origAdd ? "ON" : "FAILED");
}

// ---- enumeration (caller holds the game lock) --------------------------------------------------
struct TriggerRef { int coaster; int track; uint8_t* point; void* trigger; };
static void EnumTriggers(int onlyCoaster, std::vector<TriggerRef>& out) {
  if (!S.ok) return;
  void* park = GetPark(nullptr); if (!park) return;
  for (int ci = 0, nc = CoasterCount(park); ci < nc; ++ci) {
    if (onlyCoaster >= 0 && ci != onlyCoaster) continue;
    uint8_t* c = Rd<uint8_t**>(park, park::CoastersBegin)[ci];
    uint8_t** tb = nullptr, **te = nullptr;
    if (!SRd(c, trig::CoasterTracksBegin, tb) || !SRd(c, trig::CoasterTracksEnd, te) || !tb || te < tb || te - tb > 100000) continue;
    for (uint8_t** tp = tb; tp < te; ++tp) {
      uint8_t* track = nullptr; if (!SRd(tp, 0, track) || !track) continue;
      uint8_t** pb = nullptr, **pe = nullptr;
      if (!SRd(track, trig::TrackPointsBegin, pb) || !SRd(track, trig::TrackPointsEnd, pe) || !pb || pe < pb || pe - pb > 100000) continue;
      for (uint8_t** pp = pb; pp < pe; ++pp) {
        uint8_t* pt = nullptr; void* vt = nullptr; void* trigger = nullptr; void* back = nullptr;
        if (!SRd(pp, 0, pt) || !SRd(pt, 0, vt) || vt != S.vtTriggerPoint) continue;          // only real trigger points
        if (!SRd(pt, trig::PointTrigger, trigger) || !SRd(trigger, 0, vt) || vt != S.vtTrigger) continue;
        if (!SRd(trigger, trig::TriggerPoint, back) || back != pt) continue;                  // back-pointer must match
        out.push_back({ci, (int)(tp - tb), pt, trigger});
      }
    }
  }
}

static std::string SensorName(const TriggerRef& t) {
  auto it = S.names.find(t.trigger);
  if (it != S.names.end() && !it->second.empty()) return it->second;
  std::string n = SafeStdString(t.point + trig::PointName);
  if (!n.empty()) return n;
  char b[32]; snprintf(b, sizeof b, "Trigger %u", Rd<uint32_t>(t.trigger, trig::TriggerId));
  return b;
}

static std::string SensorsJson(int onlyCoaster) {
  std::vector<TriggerRef> list; EnumTriggers(onlyCoaster, list);
  std::string j = "[";
  EnterCriticalSection(&S.cs);
  for (size_t i = 0; i < list.size(); ++i) {
    const TriggerRef& t = list[i];
    SensorInfo si; auto it = S.byTrigger.find(t.trigger); if (it != S.byTrigger.end()) si = it->second;
    std::string trains; for (int b = 0; b < 32; ++b) if (si.trainMask >> b & 1) { if (!trains.empty()) trains += ","; trains += std::to_string(b); }
    char buf[640];
    snprintf(buf, sizeof buf, "%s{\"key\":%u,\"coaster\":%d,\"track\":%d,\"id\":%u,\"name\":\"%s\",\"named\":%d,\"step\":%.3f,"
             "\"active\":%d,\"trains\":[%s],\"lastTrain\":%d,\"enters\":%u,\"leaves\":%u,\"msSinceChange\":%lld}",
             i ? "," : "", (uint32_t)((uintptr_t)t.trigger >> 4), t.coaster, t.track, Rd<uint32_t>(t.trigger, trig::TriggerId),
             JsonEsc(SensorName(t)).c_str(), S.names.count(t.trigger) ? 1 : 0, Rd<double>(t.point, trig::PointStepCoord),
             si.trainMask != 0, trains.c_str(), si.lastTrain, si.enters, si.leaves,
             si.lastChangeMs ? (long long)(GetTickCount64() - si.lastChangeMs) : -1LL);
    j += buf;
  }
  LeaveCriticalSection(&S.cs);
  return j + "]";
}

static void SensorStates(int onlyCoaster, Writer& w) {
  std::vector<TriggerRef> list; EnumTriggers(onlyCoaster, list);
  w.u16((uint16_t)list.size());
  EnterCriticalSection(&S.cs);
  for (const TriggerRef& t : list) {
    SensorInfo si; auto it = S.byTrigger.find(t.trigger); if (it != S.byTrigger.end()) si = it->second;
    w.u32((uint32_t)((uintptr_t)t.trigger >> 4));
    w.u8(si.trainMask != 0); w.u8((uint8_t)si.lastTrain); w.u16(0);
    w.u32(si.trainMask); w.u32(si.leaves);
  }
  LeaveCriticalSection(&S.cs);
}

// ---- event queue (caller holds the game lock) ---------------------------------------------------
static const char* EventTypeName(uint32_t t) {
  static const char* n[] = {"?", "sensorEnter", "sensorLeave", "modeAuto", "modeManualBlock", "modeFullManual",
                            "advanceFwdPressed", "advanceBwdPressed"};
  return t < 8 ? n[t] : "?";
}
static std::string EventsJson(void* park, uint32_t since) {
  std::vector<BridgeEvent> ev;
  uint32_t seq;
  EnterCriticalSection(&S.cs);
  seq = S.evSeq;
  for (const BridgeEvent& e : S.events) if (e.seq > since && since <= seq) { ev.push_back(e); if (ev.size() >= 300) break; }
  std::map<void*, std::string> names = S.names;
  LeaveCriticalSection(&S.cs);
  if (!ev.empty()) seq = ev.back().seq;     // client continues from here (more may be pending)
  ULONGLONG now = GetTickCount64();
  char head[96]; snprintf(head, sizeof head, "{\"enabled\":%d,\"seq\":%u,\"events\":[", S.ok ? 1 : 0, seq);
  std::string j = head;
  for (size_t i = 0; i < ev.size(); ++i) {
    const BridgeEvent& e = ev[i];
    int ci = -1;
    if (park) for (int k = 0, n = CoasterCount(park); k < n; ++k) if (Rd<void**>(park, park::CoastersBegin)[k] == e.coaster) ci = k;
    std::string extra;
    if (e.type == 1 || e.type == 2) {
      uint32_t tid = 0; SRd(e.trigger, trig::TriggerId, tid);
      auto it = names.find(e.trigger);
      std::string nm = it != names.end() && !it->second.empty() ? it->second : "Trigger " + std::to_string(tid);
      char b[400]; snprintf(b, sizeof b, ",\"sensorKey\":%u,\"sensorId\":%u,\"sensorName\":\"%s\",\"train\":%d",
                            (uint32_t)((uintptr_t)e.trigger >> 4), tid, JsonEsc(nm).c_str(), e.train);
      extra = b;
    } else if (e.type == 6 || e.type == 7) {
      std::string nm;
      if (ci >= 0) if (SectionEntry* se = FindEntry((uint8_t*)e.coaster, e.a)) if (se->section) nm = ReadStdString(se->section + section::Name);
      char b[400]; snprintf(b, sizeof b, ",\"sectionId\":%u,\"sectionName\":\"%s\"", e.a, JsonEsc(nm).c_str());
      extra = b;
    }
    char b[600];
    snprintf(b, sizeof b, "%s{\"seq\":%u,\"coaster\":%d,\"type\":%u,\"typeName\":\"%s\",\"msAgo\":%llu%s}", i ? "," : "",
             e.seq, ci, e.type, EventTypeName(e.type), (unsigned long long)(now - e.ms), extra.c_str());
    j += b;
  }
  return j + "]}";
}
