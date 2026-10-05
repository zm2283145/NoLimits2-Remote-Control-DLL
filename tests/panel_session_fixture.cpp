#include "../src/panel_session.h"
#include <cstdio>
#include <cstdlib>
static void check(bool value, const char* why) {
  if (!value) { std::fprintf(stderr, "FAIL %s\n", why); std::exit(1); }
}
int main() {
  PanelSessions sessions;
  check(!sessions.suppression(100).requested, "no owner suppressed messages");
  check(sessions.claim(0, 1000, 34, 1, true, 15151, 100), "claim failed");
  check(sessions.suppression(100).requested, "owned suppression missing");
  check(!sessions.claim(0, 1000, 34, 2, true, 15151, 200), "second writer claimed active coaster");
  check(!sessions.allowed(0, 2, 200) && sessions.allowed(0, 1, 200), "writer gate failed");
  check(!sessions.input(0, 1000, 34, 2, 200), "spectator refreshed lease");
  check(!sessions.input(0, 2000, 34, 1, 200), "changed coaster accepted stale input");
  check(!sessions.input(0, 1000, 35, 1, 200), "changed station accepted stale input");
  check(sessions.input(0, 1000, 34, 1, 700), "owner heartbeat failed");
  check(sessions.input(0,1000,34,1,700,1),"manual mode update failed");
  check(sessions.owners(700)[0].mode==1,"manual mode missing from owner snapshot");
  check(sessions.input(0,1000,34,1,700),"configuration heartbeat failed");
  check(sessions.owners(700)[0].mode==1,"configuration packet erased manual mode");
  check(sessions.suppression(1450).requested, "lease ended early");
  check(!sessions.suppression(1451).requested, "expired lease suppressed messages");
  check(sessions.allowed(0, 2, 1451), "expired owner blocked recovery");
  check(sessions.claim(0, 1000, 34, 2, true, 15151, 1451), "new writer could not recover expired lease");
  check(!sessions.input(0, 1000, 34, 1, 1452), "old writer displaced replacement owner");
  check(!sessions.release(0, 1), "spectator released another owner");
  check(sessions.claim(1, 3000, 80, 3, true, 15151, 1452), "second coaster claim failed");
  check(!sessions.claim(2, 4000, 90, 4, true, 15153, 1453), "conflicting global endpoint accepted");
  check(sessions.release(0, 2), "owner release failed");
  check(!sessions.allowed(0, 1, 1453), "release lost in-game ownership grace period");
  check(sessions.suppression(1453).requested, "one release hid second active owner");
  sessions.disconnect(3);
  check(!sessions.suppression(1453).requested, "disconnect left suppression on");
  check(!sessions.allowed(1, 4, 1453), "disconnect lost short ownership grace period");
  check(sessions.claim(2, 4000, 90, 4, false, 15153, 1453), "ordinary session rejected different unused endpoint");
  check(!sessions.suppression(1453).requested, "ordinary session suppressed messages");
  std::puts("PASS panel ownership, heartbeat expiry, disconnect, release, endpoint and multi-coaster suppression policy");
  PanelSessions resumed;std::array<uint8_t,16> token{};token[0]=42;uintptr_t previous=0;
  check(resumed.claim(0,1000,34,1,true,15151,100),"resume initial claim");
  check(resumed.input(0,1000,34,1,110,1),"resume manual mode");
  check(!resumed.prepareResume(0,2,120,token),"spectator obtained ticket");
  check(resumed.prepareResume(0,1,120,token),"owner ticket refused");
  check(!resumed.resume(0,1000,34,2,130,token,previous),"live connection was replaced");
  resumed.disconnect(1);
  auto wrong=token;wrong[1]=1;
  check(!resumed.resume(0,1000,34,2,140,wrong,previous),"wrong ticket accepted");
  check(!resumed.resume(0,2000,34,2,140,token,previous),"changed coaster accepted");
  check(!resumed.resume(0,1000,35,2,140,token,previous),"changed station accepted");
  check(resumed.resume(0,1000,34,2,150,token,previous)&&previous==1,"valid one-use resume failed");
  check(resumed.owners(150)[0].mode==1,"manual mode lost on resume");
  check(resumed.suppression(150).requested,"suppression preference lost");
  check(!resumed.allowed(0,1,150)&&resumed.allowed(0,2,150),"old writer kept ownership");
  resumed.disconnect(1);check(resumed.suppression(150).requested,"late old disconnect erased resumed suppression");
  resumed.disconnect(2);
  check(!resumed.resume(0,1000,34,3,160,token,previous),"consumed ticket reused");
  check(resumed.allowed(0,3,861),"resume extended the input lease without heartbeat");
  check(resumed.claim(0,1000,34,3,false,15151,900),"new claim after expiry");
  check(resumed.prepareResume(0,3,910,token),"new ticket failed");resumed.disconnect(3);
  check(!resumed.resume(0,1000,34,4,1411,token,previous),"expired ticket accepted");
  check(resumed.claim(0,1000,34,3,false,15151,1700),"claim after ticket expiry");
  check(resumed.prepareResume(0,3,1710,token),"release ticket setup");
  check(resumed.release(0,3),"release failed");resumed.disconnect(3);
  check(!resumed.resume(0,1000,34,4,1720,token,previous),"explicit release retained ticket");
  std::puts("PASS one-use reconnect identity, mode, suppression, stale connection, expiry and explicit release");
}
