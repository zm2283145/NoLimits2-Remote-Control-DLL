"""Exercise the bridge's actual status/press functions with event-counting nodes.

The fixture catches the live regression where a status query enqueued Forward.
No game process is needed; output is kept in the ignored build folder.
"""
from pathlib import Path
import shutil
import subprocess
import sys
import os

root = Path(__file__).resolve().parents[1]
source = (root / "src/nl2bridge.cpp").read_text()


def function(name):
    start = source.index("static bool " + name + "(")
    opening = source.index("{", start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


prefix = r'''
#include <cstdint>
#include <cassert>
#include <cstdio>
#include "../../src/offsets.h"
using namespace nl2;
struct FakeNode { uint8_t bytes[0x4b0]{}; bool scripted=true; int calls=0, presses=0, last=-1; };
static bool IsScriptedNode(void* p) { return p && ((FakeNode*)p)->scripted; }
template<class T> static T Rd(void* p, int offset) { return *(T*)(((FakeNode*)p)->bytes+offset); }
static int Semi(void* p,int op) {
  auto& node=*(FakeNode*)p;node.calls++;node.last=op;
  if(op==1 || op==4) return 1;
  if(op==2 || op==5) {node.presses++;return 1;}
  assert(false);return 0;
}
template<class T> static T VCall(void*,int slot) { assert(slot==node::VF_SemiManual);return (T)&Semi; }
'''
suffix = r'''
int main() {
  FakeNode n;
  assert(!CanAdvance(nullptr,false));
  for(int flags=0;flags<16;flags++) {
    n.bytes[node::FwdVisible]=(flags&1)!=0;n.bytes[node::FwdEnabled]=(flags&2)!=0;
    n.bytes[node::BwdVisible]=(flags&4)!=0;n.bytes[node::BwdEnabled]=(flags&8)!=0;
    for(int i=0;i<100;i++) {
      assert(CanAdvance(&n,false)==((flags&3)==3));
      assert(CanAdvance(&n,true)==((flags&12)==12));
    }
  }
  assert(n.calls==0 && n.presses==0);
  n.bytes[node::FwdVisible]=n.bytes[node::FwdEnabled]=1;
  n.bytes[node::BwdVisible]=n.bytes[node::BwdEnabled]=1;
  assert(PressScriptedAdvance(&n,false));assert(n.presses==1&&n.last==2);
  assert(PressScriptedAdvance(&n,true));assert(n.presses==2&&n.last==5);
  n.bytes[node::FwdEnabled]=0;assert(!PressScriptedAdvance(&n,false));assert(n.presses==2);
  n.scripted=false;assert(CanAdvance(&n,false));assert(n.last==1);
  assert(CanAdvance(&n,true));assert(n.last==4);assert(n.presses==2);
  assert(!PressScriptedAdvance(&n,true));
  std::puts("PASS scripted status polls: 3,200 reads, zero native calls or Advance events");
  std::puts("PASS Forward/Backward press operations and disabled-button guard");
  std::puts("PASS normal-mode queries remain operations 1/4");
}
'''
output = root / "build/scripted_bridge_fixture"
output.mkdir(parents=True, exist_ok=True)
cpp = output / "fixture.cpp"
cpp.write_text(prefix + function("CanAdvance") + "\n" + function("PressScriptedAdvance") + suffix)
compiler = shutil.which("g++") or r"C:\msys64\mingw64\bin\g++.exe"
exe = output / "fixture.exe"
env = dict(os.environ)
env["PATH"] = str(Path(compiler).parent) + os.pathsep + env.get("PATH", "")
result = subprocess.run([compiler, "-std=c++17", "-static", str(cpp), "-o", str(exe)], env=env, timeout=60)
if result.returncode:
    sys.exit(result.returncode)
sys.exit(subprocess.run([str(exe)]).returncode)
