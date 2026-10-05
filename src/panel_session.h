#pragma once
#include <cstdint>
#include <mutex>
#include <vector>
#include <algorithm>
#include <array>

// Independent of game objects and network I/O. All timestamps are monotonic ms.
class PanelSessions {
  struct Lease {
    int coaster;
    uintptr_t incarnation, owner;
    uint32_t station;
    uint16_t telemetryPort;
    bool suppress;
    uint64_t heartbeat;
    int mode;
    bool connected=true, requestedSuppression=false, ticketPending=false;
    uint64_t ticketIssued=0;
    std::array<uint8_t,16> ticket{};
  };
  std::mutex mutex;
  std::vector<Lease> leases;
  static bool active(const Lease& l, uint64_t now) { return now - l.heartbeat <= 750; }
public:
  struct Owner { int coaster; uintptr_t incarnation; uint32_t station; uintptr_t owner; int mode; };
  std::vector<Owner> owners(uint64_t now) {
    std::lock_guard<std::mutex> lock(mutex);
    std::vector<Owner> result;
    for (const auto& l : leases) if (active(l, now)) result.push_back({l.coaster,l.incarnation,l.station,l.owner,l.mode});
    return result;
  }
  bool claim(int coaster, uintptr_t incarnation, uint32_t station, uintptr_t owner,
             bool suppress, uint16_t port, uint64_t now) {
    std::lock_guard<std::mutex> lock(mutex);
    for (const auto& l : leases) {
      if (l.coaster == coaster && active(l, now) && l.owner != owner) return false;
      if (suppress && l.suppress && active(l, now) && l.telemetryPort != port) return false;
    }
    for (auto& l : leases) if (l.coaster == coaster) {
      l = {coaster, incarnation, owner, station, port, suppress, now,0};
      l.requestedSuppression=suppress; return true;
    }
    leases.push_back({coaster, incarnation, owner, station, port, suppress, now,0});
    leases.back().requestedSuppression=suppress;
    return true;
  }
  bool allowed(int coaster, uintptr_t owner, uint64_t now) {
    std::lock_guard<std::mutex> lock(mutex);
    for (const auto& l : leases)
      if (l.coaster == coaster && active(l, now)) return l.owner == owner;
    return true;
  }
  bool input(int coaster, uintptr_t incarnation, uint32_t station, uintptr_t owner, uint64_t now, int mode=-1) {
    std::lock_guard<std::mutex> lock(mutex);
    for (auto& l : leases) if (l.coaster == coaster) {
      if (l.owner != owner) return !active(l, now);
      // Changed coaster objects or station IDs require a new claim.
      if (l.incarnation != incarnation || l.station != station) return false;
      l.heartbeat = now; if (mode>=0) l.mode=mode; return true;
    }
    return true; // preserve generic Block.setState API compatibility
  }
  bool release(int coaster, uintptr_t owner) {
    std::lock_guard<std::mutex> lock(mutex);
    for (auto it = leases.begin(); it != leases.end(); ++it) if (it->coaster == coaster) {
      if (it->owner != owner) return false;
      it->suppress = false; it->requestedSuppression=false; it->ticketPending=false; return true;
    }
    return true;
  }
  void disconnect(uintptr_t owner) {
    std::lock_guard<std::mutex> lock(mutex);
    // Keep the short ownership grace period matching the in-game lease, but
    // restore messages immediately. Connection IDs are unique, not sockets.
    for (auto& l : leases) if (l.owner == owner) {l.suppress = false; l.connected=false;}
  }
  bool prepareResume(int coaster, uintptr_t owner, uint64_t now, const std::array<uint8_t,16>& ticket) {
    std::lock_guard<std::mutex> lock(mutex);
    for (auto& l:leases) if (l.coaster==coaster && l.owner==owner && l.connected && active(l,now)) {
      l.ticket=ticket;l.ticketIssued=now;l.ticketPending=true;return true;
    }
    return false;
  }
  bool resume(int coaster, uintptr_t incarnation, uint32_t station, uintptr_t owner,
              uint64_t now, const std::array<uint8_t,16>& ticket, uintptr_t& oldOwner) {
    std::lock_guard<std::mutex> lock(mutex);
    for (auto& l:leases) if (l.coaster==coaster) {
      if (l.connected || !active(l,now) || !l.ticketPending || now-l.ticketIssued>500 ||
          l.incarnation!=incarnation || l.station!=station || l.owner==owner || l.ticket!=ticket) return false;
      for (const auto& other:leases)
        if (other.coaster!=coaster && other.suppress && active(other,now) &&
            l.requestedSuppression && other.telemetryPort!=l.telemetryPort) return false;
      oldOwner=l.owner;l.owner=owner;l.connected=true;l.ticketPending=false;
      l.suppress=l.requestedSuppression;return true;
    }
    return false;
  }
  struct Suppression { bool requested; uint16_t port; };
  Suppression suppression(uint64_t now) {
    std::lock_guard<std::mutex> lock(mutex);
    for (const auto& l : leases) if (l.suppress && active(l, now)) return {true, l.telemetryPort};
    return {false, 15151};
  }
};
