// restraints.inc - per-row (per-car) restraint control (included by nl2bridge.cpp after sensors.inc)
//
// NL2 keeps a single harness value per train (train::HarnessPos / HarnessMotion) and has no per-row logic.
// Each car, however, animates its own restraints in CarRestraintAnim(car), which just reads the train value.
// We hook that function: for a car with an override we swap in the car's own position/motion around the
// original call, so only that car's restraints move. The game itself still believes the train harness is
// in its train-wide state, so the bridge blocks dispatch while any row override is not closed.
//
// Row numbers are 1-based, front to back, counting only cars that have restraint animation.

typedef void (*FnCarAnim)(uint8_t* car);

struct RowOverride {
  uint8_t* car; uint8_t* train;
  float pos;          // 0 closed .. 1 open (same meaning as train::HarnessPos)
  uint8_t motion;     // 0 at rest, 1 closing, 2 opening (same as train::HarnessMotion)
  LONGLONG last;      // QPC of the last animation step
};

static struct {
  bool ok = false;
  FnCarAnim orig = nullptr;
  SRWLOCK lk = SRWLOCK_INIT;
  std::vector<RowOverride> v;
  volatile LONG count = 0;
  double qpf = 1;
} RW;

static void RowStep(RowOverride& o) {
  LARGE_INTEGER now; QueryPerformanceCounter(&now);
  double dt = (now.QuadPart - o.last) / RW.qpf; o.last = now.QuadPart;
  dt = dt < 0 ? 0 : (dt > 0.1 ? 0.1 : dt);
  // a train-wide open/close (bridge or in-game panel) drags every row along with it
  uint8_t tm = Rd<uint8_t>(o.train, train::HarnessMotion);
  if (tm == 2 && !(o.motion == 0 && o.pos >= 1.0f)) o.motion = 2;
  if (tm == 1 && !(o.motion == 0 && o.pos <= 0.0f)) o.motion = 1;
  if (o.motion == 2) {
    o.pos += (float)dt * Rd<float>(o.train, train::HarnessOpenSpeed);
    if (o.pos >= 1.0f) { o.pos = 1.0f; o.motion = 0; }
  } else if (o.motion == 1) {
    o.pos -= (float)dt * Rd<float>(o.train, train::HarnessCloseSpeed);
    if (o.pos <= 0.0f) { o.pos = 0.0f; o.motion = 0; }
  }
}

static void HookCarAnim(uint8_t* car) {
  if (RW.count == 0) { RW.orig(car); return; }
  AcquireSRWLockExclusive(&RW.lk);
  size_t i = 0;
  while (i < RW.v.size() && RW.v[i].car != car) ++i;
  if (i == RW.v.size() || Rd<uint8_t*>(car, car::Train) != RW.v[i].train) {
    ReleaseSRWLockExclusive(&RW.lk);
    RW.orig(car);
    return;
  }
  RowOverride& o = RW.v[i];
  RowStep(o);
  uint8_t* tr = o.train;
  float tp = Rd<float>(tr, train::HarnessPos); uint8_t tm = Rd<uint8_t>(tr, train::HarnessMotion);
  *(float*)(tr + train::HarnessPos) = o.pos; *(tr + train::HarnessMotion) = o.motion;
  RW.orig(car);
  *(float*)(tr + train::HarnessPos) = tp; *(tr + train::HarnessMotion) = tm;
  if (o.motion == 0 && tm == 0 && o.pos == tp) {        // back in step with the train: drop the override
    RW.v.erase(RW.v.begin() + i);
    InterlockedExchange(&RW.count, (LONG)RW.v.size());
  }
  ReleaseSRWLockExclusive(&RW.lk);
}

static void InitRows() {
  LARGE_INTEGER f; QueryPerformanceFrequency(&f); RW.qpf = (double)f.QuadPart;
  uint8_t* fn = Resolve(kRowSig);
  Log("  %-16s %s %p (optional)", kRowSig.name, fn ? "OK  " : "FAIL", fn);
  if (!fn) { Log("rows: per-row restraints disabled (signature missing)"); return; }
  RW.orig = (FnCarAnim)InstallHook(fn, HOOK_CAR_ANIM_PROLOGUE, (void*)&HookCarAnim, sizeof HOOK_CAR_ANIM_PROLOGUE);
  RW.ok = RW.orig != nullptr;
  Log("  rows: restraint hook %s", RW.ok ? "ON" : "FAILED");
}

// ---- car enumeration (caller holds the game lock) -------------------------------------------------
struct CarInfo { uint8_t* car; double offset; int nodes, groups, seats, models; bool gear; };   // gear = steering gear (+0x450 list)

static void EnumCars(uint8_t* tr, std::vector<CarInfo>& out) {
  for (int list = 0; list < 2; ++list) {
    uint8_t* b = nullptr, *e = nullptr;
    if (!SRd(tr, list ? train::GearsBegin : train::CarsBegin, b) || !SRd(tr, list ? train::GearsEnd : train::CarsEnd, e)) continue;
    if (!b || e < b || (e - b) > 0x10 * 256) continue;
    for (uint8_t* p = b; p < e; p += 0x10) {
      uint8_t* c = nullptr; void* back = nullptr;
      if (!SRd(p, 0, c) || !c || !SRd(c, car::Train, back) || back != tr) continue;
      CarInfo ci{c, 0, 0, 0, 0, 0, list == 1};
      SRd(c, car::Offset, ci.offset);
      uint8_t* mb = nullptr, *me = nullptr;
      if (SRd(c, car::ModelsBegin, mb) && SRd(c, car::ModelsEnd, me) && mb && me > mb && me - mb < 0x38 * 256)
        ci.models = (int)((me - mb) / 0x38);
      uint8_t* sb = nullptr, *se = nullptr;
      if (SRd(c, car::SeatsBegin, sb) && SRd(c, car::SeatsEnd, se) && sb && se > sb && se - sb < 8 * 256) ci.seats = (int)((se - sb) / 8);
      uint8_t* nb = nullptr, *ne = nullptr;
      if (SRd(c, car::NodesBegin, nb) && SRd(c, car::NodesEnd, ne) && nb && ne > nb) ci.nodes = (int)((ne - nb) / 8);
      uint8_t* gb = nullptr, *ge = nullptr;
      if (SRd(c, car::AnimBegin, gb) && SRd(c, car::AnimEnd, ge) && gb && ge > gb && ge - gb < 0x28 * 256)
        for (uint8_t* g = gb; g < ge; g += 0x28) {
          uint8_t* xb = nullptr, *xe = nullptr;
          if (SRd(g, 0, xb) && SRd(g, 8, xe) && xb && xe > xb) ci.groups += (int)((xe - xb) / 8);
        }
      out.push_back(ci);
    }
  }
}

// Cars that carry restraints, front to back (row 1 = front). Same order as the game's own car accessor
// (0x140499490): the car list first, then the steering gears (which normally have no restraints).
static void TrainRowCars(uint8_t* tr, std::vector<CarInfo>& rows) {
  std::vector<CarInfo> all; EnumCars(tr, all);
  for (auto& c : all) if (c.nodes || c.groups) rows.push_back(c);
}

// Seat capacity of a train: total seats, and how many cars carry seats. custom = the train has cars but none of
// them has an NL2 car model (a park script draws the train). Caller holds the game lock.
static TrainSeats GetTrainSeats(uint8_t* tr) {
  TrainSeats s;
  if (!tr) return s;
  std::vector<CarInfo> all; EnumCars(tr, all);
  bool model = false;
  for (auto& c : all) {
    if (c.models > 0) model = true;
    if (c.seats > 0) { s.seats += c.seats; s.cars++; s.maxPerCar = std::max(s.maxPerCar, c.seats); }
  }
  s.custom = !all.empty() && !model;
  return s;
}

// Drop overrides whose train/car no longer exists (park reload, train removed). Caller holds the game lock.
static void RowsPrune() {
  if (RW.count == 0) return;
  void* park = GetPark(nullptr);
  std::vector<uint8_t*> trains;
  if (park) for (int ci = 0, nc = CoasterCount(park); ci < nc; ++ci) {
    uint8_t* c = Rd<uint8_t**>(park, park::CoastersBegin)[ci];
    for (int t = 0, n = TrainCount(c); t < n; ++t) trains.push_back(Rd<uint8_t**>(c, coaster::TrainsBegin)[t]);
  }
  AcquireSRWLockExclusive(&RW.lk);
  for (size_t i = 0; i < RW.v.size();) {
    bool keep = false;
    for (uint8_t* t : trains) if (t == RW.v[i].train) {
      std::vector<CarInfo> cars; EnumCars(t, cars);
      for (auto& c : cars) if (c.car == RW.v[i].car) keep = true;
    }
    if (keep) ++i; else RW.v.erase(RW.v.begin() + i);
  }
  InterlockedExchange(&RW.count, (LONG)RW.v.size());
  ReleaseSRWLockExclusive(&RW.lk);
}

// Row overrides on this train that are not fully closed
static int RowsNotClosed(uint8_t* tr) {
  if (!tr || RW.count == 0) return 0;
  int n = 0;
  AcquireSRWLockShared(&RW.lk);
  for (auto& o : RW.v) if (o.train == tr && (o.motion != 0 || o.pos > 0.0f)) ++n;
  ReleaseSRWLockShared(&RW.lk);
  return n;
}

// Effective restraint position of a car (override if any, else the train value)
static void RowState(uint8_t* tr, uint8_t* c, float& pos, uint8_t& motion, bool& over) {
  pos = Rd<float>(tr, train::HarnessPos); motion = Rd<uint8_t>(tr, train::HarnessMotion); over = false;
  AcquireSRWLockShared(&RW.lk);
  for (auto& o : RW.v) if (o.car == c) { pos = o.pos; motion = o.motion; over = true; break; }
  ReleaseSRWLockShared(&RW.lk);
}

// Start one car's restraints opening/closing
static void RowCommand(uint8_t* tr, uint8_t* c, bool open) {
  LARGE_INTEGER now; QueryPerformanceCounter(&now);
  AcquireSRWLockExclusive(&RW.lk);
  RowOverride* o = nullptr;
  for (auto& x : RW.v) if (x.car == c) o = &x;
  if (!o) {
    RW.v.push_back({c, tr, Rd<float>(tr, train::HarnessPos), Rd<uint8_t>(tr, train::HarnessMotion), now.QuadPart});
    o = &RW.v.back();
  }
  if (open && !(o->motion == 0 && o->pos >= 1.0f)) o->motion = 2;
  if (!open && !(o->motion == 0 && o->pos <= 0.0f)) o->motion = 1;
  o->last = now.QuadPart;
  InterlockedExchange(&RW.count, (LONG)RW.v.size());
  ReleaseSRWLockExclusive(&RW.lk);
}

// Close every open row of a train (used by the train-wide "close harness" station op)
static int RowsCloseAll(uint8_t* tr) {
  int n = 0;
  LARGE_INTEGER now; QueryPerformanceCounter(&now);
  AcquireSRWLockExclusive(&RW.lk);
  for (auto& o : RW.v) if (o.train == tr && (o.motion == 2 || o.pos > 0.0f)) { o.motion = 1; o.last = now.QuadPart; ++n; }
  ReleaseSRWLockExclusive(&RW.lk);
  return n;
}

static std::string RowsJson(uint8_t* c, int si, uint8_t* st) {
  uint8_t* tr = Rd<uint8_t*>(st, station::Train);
  char buf[512];
  int ti = tr ? Rd<uint8_t>(tr, train::Index) : -1;
  TrainSeats ts = GetTrainSeats(tr);
  snprintf(buf, sizeof buf, "{\"enabled\":%d,\"station\":%d,\"hasTrain\":%d,\"train\":%d,\"trainHarness\":%.3f,\"trainMotion\":%d,"
           "\"rowsOpen\":%d,\"seats\":%d,\"seatedCars\":%d,\"customTrain\":%d,\"rows\":[", RW.ok ? 1 : 0, si, tr != nullptr, ti,
           tr ? (double)Rd<float>(tr, train::HarnessPos) : -1.0, tr ? Rd<uint8_t>(tr, train::HarnessMotion) : 0, RowsNotClosed(tr),
           ts.seats, ts.cars, ts.custom);
  std::string j = buf;
  std::vector<CarInfo> rows, all;
  if (tr) { TrainRowCars(tr, rows); EnumCars(tr, all); }
  for (size_t i = 0; i < rows.size(); ++i) {
    float pos; uint8_t m; bool over; RowState(tr, rows[i].car, pos, m, over);
    snprintf(buf, sizeof buf, "%s{\"row\":%zu,\"position\":%.3f,\"open\":%d,\"closed\":%d,\"opening\":%d,\"closing\":%d,\"individual\":%d,\"seats\":%d}",
             i ? "," : "", i + 1, (double)pos, pos >= 1.0f && m != 1, pos <= 0.0f && m != 2, m == 2, m == 1, over, rows[i].seats);
    j += buf;
  }
  j += "],\"cars\":[";
  for (size_t i = 0; i < all.size(); ++i) {
    snprintf(buf, sizeof buf, "%s{\"type\":\"%s\",\"offset\":%.3f,\"nodes\":%d,\"groups\":%d,\"seats\":%d,\"models\":%d,\"class\":\"%s\"}", i ? "," : "", all[i].gear ? "gear" : "car",
             all[i].gear ? -all[i].offset : 0.0, all[i].nodes, all[i].groups, all[i].seats, all[i].models, JsonEsc(RttiName(all[i].car)).c_str());
    j += buf;
  }
  return j + "]}";
}
