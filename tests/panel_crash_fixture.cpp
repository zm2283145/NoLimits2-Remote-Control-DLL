#include "../src/panel_crash.h"
#include <cassert>
#include <cstdio>
int main() {
  PanelCrashWatch w;w.watch(0,123,34);auto& r=w.records()[0];
  PanelCrashWatch::observe(r,true,false,0,10);assert(!r.fault);
  PanelCrashWatch::observe(r,true,true,1,20);
  PanelCrashWatch::observe(r,true,false,1,30);assert(!r.fault);
  PanelCrashWatch::observe(r,true,true,1,40);
  PanelCrashWatch::observe(r,true,false,0,50);
  PanelCrashWatch::observe(r,true,false,0,299);assert(!r.fault);
  PanelCrashWatch::observe(r,true,true,1,300);assert(!r.fault);
  PanelCrashWatch::observe(r,true,false,0,400);
  PanelCrashWatch::observe(r,true,false,0,650);assert(r.fault==909);
  PanelCrashWatch::observe(r,false,true,1,700);assert(r.fault==909);
  w.beginReset(0,123);PanelCrashWatch::observe(r,true,false,0,1000);assert(r.fault==909);
  PanelCrashWatch::observe(r,true,true,1,1100);assert(!r.fault && !r.resetting);
  PanelCrashWatch::observe(r,false,false,0,1200);assert(!r.fault);
  PanelCrashWatch::observe(r,false,false,0,1500);assert(!r.fault);
  assert(!w.fault(0,456));w.watch(0,456,34);assert(!w.records()[0].seenOnline);
  std::puts("PASS crash startup, debounce, transient recovery, disconnect latch, reset completion and object replacement");
}
