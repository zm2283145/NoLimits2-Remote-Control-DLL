#pragma once
#include <cstdint>
#include <vector>

// Access only while holding NL2's game lock. The VM stops stepping on a crash,
// so this independent observer must retain the fault until an actual reset.
class PanelCrashWatch {
public:
  struct Ride {
    int coaster; uintptr_t incarnation; uint32_t station;
    bool seenOnline=false, resetting=false;
    uint64_t offlineSince=0;
    int fault=0;
  };
private:
  std::vector<Ride> rides;
public:
  void watch(int coaster, uintptr_t incarnation, uint32_t station) {
    for (auto& r : rides) if (r.coaster==coaster) {
      if (r.incarnation!=incarnation) r={coaster,incarnation,station};
      else r.station=station;
      return;
    }
    rides.push_back({coaster,incarnation,station});
  }
  std::vector<Ride>& records() { return rides; }
  int fault(int coaster, uintptr_t incarnation) const {
    for (const auto& r : rides) if (r.coaster==coaster && r.incarnation==incarnation) return r.fault;
    return 0;
  }
  void beginReset(int coaster, uintptr_t incarnation) {
    for (auto& r : rides) if (r.coaster==coaster && r.incarnation==incarnation) {
      r.resetting=true; r.seenOnline=false; r.offlineSince=0;
    }
  }
  static void observe(Ride& r, bool owned, bool ready, unsigned mode, uint64_t now) {
    bool online=ready && mode>=1 && mode<=3;
    if (r.resetting) {
      if (online) { r.resetting=false; r.fault=0; r.seenOnline=owned; }
      return;
    }
    if (r.fault) return;
    if (!owned) { r.seenOnline=false; r.offlineSince=0; return; }
    if (online) { r.seenOnline=true; r.offlineSince=0; return; }
    // Neither an E-stop nor a simulation pause is a crash. NL2's confirmed
    // crash transition is Ready=false plus Offline=0, after an online state.
    if (!ready && mode==0 && r.seenOnline) {
      if (!r.offlineSince) r.offlineSince=now;
      if (now-r.offlineSince>=250) r.fault=909;
    } else r.offlineSince=0;
  }
};
