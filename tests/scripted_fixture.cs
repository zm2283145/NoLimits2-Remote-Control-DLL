// Executes the actual NLVM engine with deterministic game API fixtures.
// This checks command/interlock decisions, not NL2 physics or API scheduling.
using System;
using System.Collections.Generic;
namespace TestHost {
public static class World {
  public static Coaster coaster;
  public static long now;
  public static bool auxiliary;
  public static bool transit;
  public static bool storage;
  public static bool invalid;
  public static void Reset() {
    now=100; storage=false; transit=false; auxiliary=false; invalid=false; coaster=new Coaster();
    foreach(var name in new[]{"Station","Lift","Brake","Table"}) coaster.blocks[name]=new Block(name);
    coaster.trains=new[]{new Train(0),new Train(1)};
    SetTrain("Station",0); SetTrain("Brake",1);
    coaster.blocks["Station"].section.waitAdvance=true;
  }
  public static void SetTrain(string name,int index) {
    var b=coaster.blocks[name]; b.count=index<0?0:1;
    b.section.trains=index<0?Array.Empty<Train>():new[]{coaster.trains[index]};
  }
}
public class Script { public Sim sim=new Sim(); public int getParentEntityId()=>0; }
public class Sim { public Coaster getCoasterForEntityId(int id)=>World.coaster; }
public interface BlockSystemController {}
public static class NLVMSystem {
  public static Log err=new Log(), @out=new Log();
  public static long currentTimeMillis()=>World.now;
}
public class Log { public void println(string message) {} }
public class Coaster {
  public const int E_BLOCK_SYSTEM_MODE_AUTOMATIC=0,E_BLOCK_SYSTEM_MODE_SEMI_MANUAL=1,E_BLOCK_SYSTEM_MODE_FULL_MANUAL=2,E_BLOCK_SYSTEM_MODE_OFFLINE=-1;
  public int mode;
  public int getBlockSystemMode()=>mode;
  public Dictionary<string,Block> blocks=new(); public Train[] trains;
  public bool emergency; public SpecialTrack track=new SpecialTrack();
  public bool isScriptedOperationMode()=>true;
  public Block getBlock(string name)=>blocks.GetValueOrDefault(name);
  public Section getSection(string name)=>getBlock(name)?.section;
  public SpecialTrack getSpecialTrack(string name)=>track;
  public string getName()=>"Fixture";
  public void setBlockSystemController(BlockSystemController controller) {}
  public bool isEmergencyStop()=>emergency;
  public void setEmergencyStop(bool on)=>emergency=on;
  public int getTrainCount()=>trains.Length;
  public Train getTrainAt(int i)=>trains[i];
}
public class Vector3f { public float x,y,z; }
public class Train {
  public int index; public double speed; public bool lashed;
  public float frontPos=100, rearPos=50;
  public int getBogieCount()=>2;
  public void getBogieOrientationAndPosition(int i,Vector3f f,Vector3f top,Vector3f right,Vector3f p) { if(f!=null) {f.x=1;f.y=0;f.z=0;} if(p!=null) {p.x=i==0?frontPos:rearPos;p.y=0;p.z=0;} }
  public Train(int index) { this.index=index; }
  public int getTrainIndex()=>index;
  public float getHarnessState()=>0;
  public double getSpeed()=>speed;
  public bool isLashedToTrack()=>lashed;
  public void setLashedToTrack(bool on)=>lashed=on;
}
public class SpecialTrack {
  public int position=3;
  public int getNumberOfSwitchDirections()=>4;
  public int getSwitchDirection()=>position;
}
public class Block {
  public const int LAMP_OFF=0,LAMP_ON=1,LAMP_FLASHING=2;
  public string name; public int state,count; public bool fwd,bwd,warning=true; public Section section=new Section();
  public Block(string name) { this.name=name; }
  public string getName()=>name;
  public Section getSection()=>section;
  public int getNumberOfTrainsOnBlock()=>count;
  public int getState()=>state;
  public void setState(int s)=>state=s;
  public void registerState(int s,string text,int lamp) {}
  public void setEnableMultipleTrainsOnBlockWarning(bool on)=>warning=on;
  public void setAdvanceFwdVisible(bool on) {}
  public void setAdvanceBwdVisible(bool on) {}
  public void setAdvanceFwdEnabled(bool on)=>fwd=on;
  public void setAdvanceBwdEnabled(bool on)=>bwd=on;
}
public class Section {
  public Train[] trains=Array.Empty<Train>();
  public bool brakes=true,manual,waitClear,waitAdvance,canDispatch=true,park,overshot,trigger=true,beforeCenter=true,behindCenter=true;
  public int transport,lift,departures,dispatches,arrivals;
  public bool isTrainOnSection()=>trains.Length>0;
  public Train[] getTrainsOnSection()=>trains;
  public Train getTrainOnSection()=>trains.Length>0?trains[0]:null;
  public void setBrakesOn()=>brakes=true;
  public void setBrakesOff()=>brakes=false;
  public void setBrakesTrim()=>brakes=false;
  public bool isBrakesOn()=>brakes;
  public void setTransportsOff()=>transport=0;
  public void setTransportsStandardFwdOn()=>transport=1;
  public void setTransportsStandardBwdOn()=>transport=-1;
  public void setTransportsStandardFwdDependingOnBrake()=>transport=1;
  public void setLiftOff()=>lift=0;
  public void setLiftFwdOn()=>lift=1;
  public void setLiftBwdOn()=>lift=-1;
  public void setLiftFwdIdleOn()=>lift=2;
  public void setStationManualDispatchMode(bool on)=>manual=on;
  public void setStationEntering() { arrivals++; waitAdvance=false; }
  public void setStationLeaving() { departures++; waitAdvance=false; }
  public bool isStationWaitingForClearBlock()=>waitClear;
  public bool isStationWaitingForAdvance()=>waitAdvance;
  public void setStationNextBlockClear()=>waitClear=false;
  public void setStationNextBlockOccupied() { waitClear=false;waitAdvance=false; }
  public bool canStationManualDispatch()=>canDispatch;
  public void doStationManualDispatch() {dispatches++;waitAdvance=true;}
  public bool isTrainBehindEndOfSection(double distance)=>distance<1?overshot:park;
  public bool isTrainBehindLiftTrigger()=>trigger;
  public bool isTrainBehindBrakeTrigger()=>trigger;
  public bool isTrainBeforeCenterOfSection()=>beforeCenter;
  public bool isTrainBehindCenterOfSection()=>behindCenter;
}
}
public class RideProfile {
  public static void configure(PanelController c) {
    if(TestHost.World.invalid) { c.addBlock("Missing",0,34,1);return; }
    int station=c.addBlock("Station",0,34,1);
    int lift=c.addBlock("Lift",1,33,1);
    int brake=c.addBlock("Brake",2,22,1);
    if(TestHost.World.transit) {
      int table=c.addBlock("Table",5,38,1);
      c.setPhysicalClear(station);c.setPhysicalClear(table);c.setReturnToPark(station);
      c.addRoute(station,table,false,"Switch",3);c.addRoute(table,lift,false,"Switch",3);
      if(TestHost.World.storage) {
        int storage=c.addBlock("Storage",3,37,1);
        c.addRoute(table,storage,true,"Switch",0);c.addRoute(storage,table,false,"Switch",0);
        c.addRoute(table,station,true,"Switch",3);
      }
    } else c.addRoute(station,lift,false,"Switch",3);
    c.addRoute(lift,brake,false,null,0);
    c.addRoute(brake,station,false,"Switch",3);
    c.setBeforeStation(brake);
    if(TestHost.World.auxiliary) {c.addAuxiliary(station,"Table");c.setParkingSection(station,"Table");}
  }
}
public static class Acceptance {
  static int passed;
  static TestHost.Section S(string name)=>TestHost.World.coaster.blocks[name].section;
  static TestHost.Block B(string name)=>TestHost.World.coaster.blocks[name];
  static void Check(bool condition,string why) {if(!condition)throw new Exception(why);}
  static PanelController Init(bool auxiliary=false,bool transit=false,bool storage=false) {
    TestHost.World.Reset();TestHost.World.auxiliary=auxiliary;TestHost.World.transit=transit;TestHost.World.storage=storage;
    if(storage)TestHost.World.coaster.blocks["Storage"]=new TestHost.Block("Storage");
    var c=new PanelController();Check(c.onInit(),"initialize");return c;
  }
  static void Send(PanelController c,int flags,int mode=0,int selected=34,float tick=2f,bool permit=true) {
    B("Station").state=0x40000000+(selected<<13)+(mode<<11)+flags+(permit?256:0);
    TestHost.World.now+=100;c.onNextFrame(tick);
  }
  static void Frame(PanelController c,int ms=100) {TestHost.World.now+=ms;c.onNextFrame(.1f);}
  static void GameMode(PanelController c,int mode,bool notify=true) {
    var ride=TestHost.World.coaster;ride.mode=mode;
    if(!notify)return;
    if(mode==0)c.onAutoMode(ride);else if(mode==1)c.onManualBlockMode(ride);else c.onFullManualMode(ride);
  }
  static PanelController Flying() {
    var c=Init();Frame(c);TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Lift",0);
    Frame(c);Frame(c);TestHost.World.SetTrain("Brake",-1);Frame(c);Frame(c);Frame(c);
    Check(B("Lift").state==4&&B("Brake").state==1,"flight timer preparation did not release lift");
    TestHost.World.SetTrain("Lift",-1);return c;
  }
  static void Simulate(PanelController c,float seconds) {c.onNextFrame(seconds);}
  static void Test(string name,Action action) {action();passed++;Console.WriteLine("PASS "+name);}
  public static void Main() {
    Test("Panel entry with both buttons held cannot dispatch",()=>{
      var c=Init();S("Station").waitAdvance=false;Send(c,31, tick:2);
      Check(S("Station").dispatches==0&&S("Station").departures==0,"held entry bypassed rearm");
    });
    Test("Both buttons required; dropping either stops a departure",()=>{
      var c=Init();S("Station").waitAdvance=false;
      Send(c,19);Send(c,23,tick:2);Check(S("Station").dispatches==0,"one button dispatched");
      Send(c,31,tick:2);Send(c,31);
      Check(S("Station").departures==1&&S("Station").transport==1,"both did not depart");
      Send(c,27);Check(S("Station").brakes&&S("Station").transport==0,"advance release did not stop");
      Send(c,31);Send(c,23);Check(S("Station").brakes&&S("Station").transport==0,"dispatch release did not stop");
    });
    Test("Independent restraint interlock prevents departure despite native dispatch ready",()=>{
      var c=Init();Send(c,19,permit:false);Send(c,31,permit:false);
      Check(S("Station").dispatches==0&&S("Station").departures==0,"row interlock bypassed");
      Send(c,31,tick:.1f);Check(S("Station").dispatches==0,"interlock closure reused old hold time");
      Send(c,31,tick:2);Check(S("Station").dispatches==1,"valid interlock never permitted dispatch");
    });
    Test("Auxiliary tail holds reservation and obeys release",()=>{
      var c=Init(true);Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Table",0);
      Send(c,31);Check(B("Brake").state==3&&S("Table").transport==1,"tail freed station early");
      Send(c,23);Check(S("Table").brakes&&S("Table").transport==0,"auxiliary kept driving");
    });
    Test("Lift cannot pull a train whose tail remains in a released station",()=>{
      var c=Init();Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.SetTrain("Lift",0);S("Lift").trigger=false;
      Send(c,23);Check(S("Lift").lift==0,"downstream lift bypassed button release");
      Send(c,31);Check(S("Lift").lift==1,"paired hold failed to resume lift overlap");
      TestHost.World.SetTrain("Station",-1);Send(c,23);
      Check(S("Lift").lift==1,"lift failed to continue after station tail cleared");
    });
    Test("Panel lift block stop needs a fresh Lift Start after clearance",()=>{
      var c=Init();Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Lift",0);
      Send(c,19,selected:33);Send(c,19,selected:33);
      Check(S("Lift").lift==0,"blocked lift ran");
      Send(c,31,selected:33);
      TestHost.World.SetTrain("Brake",-1);
      Send(c,19,selected:33);Send(c,19,selected:33);
      Check(S("Lift").lift==0,"clearance restarted lift without new press");
      Send(c,3,selected:33);Send(c,19,selected:33);Send(c,19,selected:33);
      Check(S("Lift").lift==1,"fresh lift start did not resume");
    });
    Test("Unowned lift automatically restarts after block clearance",()=>{
      var c=Init();Frame(c);
      TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Lift",0);
      Frame(c);Frame(c);Check(S("Lift").lift==0,"occupied destination released lift");
      TestHost.World.SetTrain("Brake",-1);Frame(c);Frame(c);Frame(c);
      Check(S("Lift").lift==1,"unowned lift failed to restart");
    });
    Test("Station physically clears while the departing train occupies transfer",()=>{
      var c=Init(transit:true);Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Table",0);
      B("Station").count=1;
      Send(c,31);Check(S("Station").transport==0,"empty station kept driving");
      Send(c,31);Check(B("Brake").state==4,"logical station assignment blocked arrival");
      Check(!TestHost.World.coaster.emergency,"expected transfer ownership caused fault");
    });
    Test("Logical station overlap during approach does not require physical station entry",()=>{
      var c=Init(transit:true);Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Table",0);B("Station").count=1;
      Send(c,31);Send(c,31);Check(!B("Station").warning,"warning was not disabled before native assignment");B("Station").count=2;Send(c,31);
      Check(!TestHost.World.coaster.emergency&&!B("Station").warning,"valid approaching reservation faulted or warned");
    });
    Test("Manual station departure pulls its tail onto the feeder using the main pair",()=>{
      var c=Init(transit:true);S("Table").behindCenter=false;
      Send(c,19,mode:1);Send(c,31,mode:1);Send(c,31,mode:1);
      Check(S("Station").departures==1,"manual station departure unavailable");
      TestHost.World.SetTrain("Table",0);Send(c,31,mode:1);
      Check(S("Table").transport==1,"manual feeder required extra jog");
      Send(c,23,mode:1);Check(S("Table").transport==0,"manual feeder ignored main button release");
    });
    Test("Manual empty brake opens independently and closes without tire movement",()=>{
      var c=Init(transit:true);Send(c,3,mode:1,selected:38);
      B("Station").state=0x52000000+(38<<2)+2;Frame(c);
      Check(!S("Table").brakes&&S("Table").transport==0,"empty manual brake did not open independently");
      Send(c,3,mode:1,selected:38);Check(!S("Table").brakes,"brake setting lost after heartbeat");
      B("Station").state=0x52000000+(38<<2)+1;Frame(c);
      Check(S("Table").brakes&&S("Table").transport==0,"manual brake did not close");
      B("Station").state=0x52000000+(38<<2)+2;Frame(c);
      Send(c,3,mode:1,selected:33);Send(c,3,mode:1,selected:38);
      Check(S("Table").brakes,"selection change retained an open override");
      Send(c,3,mode:0,selected:38);Check(S("Table").brakes,"Auto retained manual brake override");
    });
    Test("Manual occupied brake reserves destination before opening and preserves held jog",()=>{
      var c=Init(transit:true);TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Table",0);
      Send(c,3,mode:1,selected:38);Send(c,3,mode:1,selected:38);
      Check(B("Table").state==3,"table did not park for manual test");
      B("Station").state=0x52000000+(38<<2)+2;Frame(c);
      Frame(c);
      Check(!S("Table").brakes&&S("Table").transport==0&&B("Lift").state==1,"brake release lacked reservation or powered tires");
      Send(c,3+32,mode:1,selected:38);Check(S("Table").transport==1,"held manual jog failed");
      Send(c,3,mode:1,selected:38);Check(S("Table").transport==0&&!S("Table").brakes,"jog release failed or erased brake setting");
      B("Station").state=0x52000000+(38<<2)+1;Frame(c);
      Check(S("Table").brakes&&S("Table").transport==0,"manual close failed during reserved move");
    });
    Test("Manual brake cannot bypass occupied destination, block hold or station advance pair",()=>{
      var c=Init(transit:true);TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Table",0);TestHost.World.SetTrain("Lift",1);TestHost.World.SetTrain("Brake",-1);
      Send(c,3,mode:1,selected:38);Send(c,3,mode:1,selected:38);
      B("Station").state=0x52000000+(38<<2)+2;Frame(c);
      Check(S("Table").brakes&&B("Table").state==3,"manual brake released into occupied lift");
      TestHost.World.SetTrain("Lift",-1);Send(c,3+128,mode:1,selected:38);
      Check(S("Table").brakes,"block hold failed to override brake open");
      c=Init();TestHost.World.SetTrain("Station",-1);Send(c,3,mode:1,selected:22);
      B("Station").state=0x52000000+(22<<2)+2;Frame(c);
      Check(S("Brake").brakes&&B("Brake").state==3,"manual brake bypassed station advance pair");
    });
    Test("Closing a manual waiting brake stops the same train's station parking tires",()=>{
      var c=Init();TestHost.World.SetTrain("Station",-1);
      Send(c,3,mode:1,selected:22);Send(c,15,mode:1,selected:22);Send(c,15,mode:1,selected:22);
      TestHost.World.SetTrain("Station",1);Send(c,15,mode:1,selected:22);
      Check(S("Station").transport==1,"fixture train did not start parking");
      B("Station").state=0x52000000+(22<<2)+1;Frame(c);
      Check(S("Station").transport==0&&S("Brake").transport==0,"closed brake left arrival tires pulling");
    });
    Test("Transfer departure needs station selection and pair; table parks instead of passing through",()=>{
      var c=Init(transit:true);S("Table").behindCenter=false;
      Send(c,19,mode:2,selected:38);Send(c,31,mode:2,selected:38);
      Check(S("Station").departures==0,"unselected station departed in Transfer");
      Send(c,19,mode:2);Send(c,31,mode:2);Send(c,31,mode:2);
      Check(S("Station").departures==1,"selected transfer departure failed");
      TestHost.World.SetTrain("Table",0);Send(c,31,mode:2);
      Check(S("Table").transport==1,"table did not follow held departure");
      TestHost.World.SetTrain("Station",-1);S("Table").behindCenter=true;Send(c,31,mode:2);Send(c,31,mode:2);
      Check(B("Table").state==3&&S("Table").transport==0&&B("Lift").state==0,"table failed to park/ran into lift");
      Check(B("Brake").state==3,"Transfer automatically advanced next station train");
    });
    Test("Storage route reserves, holds selected jog, parks lashed, and returns to table",()=>{
      var c=Init(transit:true,storage:true);TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Table",0);
      TestHost.World.coaster.track.position=0;
      Send(c,19,mode:2,selected:38);Send(c,19+32+64,mode:2,selected:38);
      Check(B("Storage").state==1&&S("Storage").transport==-1,"storage was not reserved/powered before entry");
      Send(c,19+32+64,mode:2,selected:38);Check(S("Table").transport==-1,"reverse jog did not drive table");
      Send(c,19,mode:2,selected:38);Check(S("Table").transport==0&&S("Storage").transport==0,"jog release did not stop source and destination");
      TestHost.World.SetTrain("Table",-1);TestHost.World.SetTrain("Storage",0);
      Send(c,19+32+64,mode:2,selected:38);Send(c,19,mode:2,selected:37);
      Check(B("Storage").state==3&&TestHost.World.coaster.trains[0].lashed,"storage park did not secure train");
      Send(c,19+32,mode:2,selected:37);Send(c,19+32,mode:2,selected:37);
      Check(B("Table").state==1&&!TestHost.World.coaster.trains[0].lashed&&S("Storage").transport==1,"storage return did not reserve/unlash/move");
    });
    Test("Reverse table-to-station parking obeys jog and returns to station handler",()=>{
      var c=Init(transit:true,storage:true);TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Table",0);
      Send(c,19,mode:2,selected:38);Send(c,19+32+64,mode:2,selected:38);
      TestHost.World.SetTrain("Station",0);S("Station").park=true;
      Send(c,19+32+64,mode:2,selected:38);Check(S("Station").transport==-1,"reverse arrival did not move toward park");
      Send(c,19,mode:2,selected:38);Check(S("Station").transport==0,"reverse arrival ignored jog release");
      TestHost.World.SetTrain("Table",-1);S("Station").park=false;
      Send(c,19+32+64,mode:2,selected:38);Check(B("Station").state==5&&S("Station").arrivals>0,"reverse transfer did not park");
    });
    Test("Controller remains identifiable during handback blocked by transfer alignment",()=>{
      var c=Init(transit:true);Send(c,3,mode:2);TestHost.World.coaster.track.position=0;
      Frame(c,900);Check(B("Station").state==0x20000004,"blocked handback hid controller signature");
      Send(c,31,mode:2);Check(S("Station").transport==0,"held reconnect bypassed release");
    });
    Test("Selected trigger-next missed park produces 908 on arrival",()=>{
      var c=Init(transit:true);c.setStopMonitor(0,6000,0);TestHost.World.SetTrain("Station",-1);
      Send(c,19);B("Station").state=0x50000000+2+8192+16384;Frame(c);
      Send(c,31);TestHost.World.SetTrain("Brake",-1);TestHost.World.SetTrain("Station",1);S("Station").park=false;
      Send(c,31);Check(S("Station").transport==0&&!TestHost.World.coaster.emergency,"forced missed park did not hold before timeout");
      Send(c,31,tick:7);Check(TestHost.World.coaster.emergency&&B("Station").state==0x20010000+908,"missed park training did not latch 908");
    });
    Test("Training disabled cannot inject a trigger-next missed park",()=>{
      var c=Init(transit:true);c.setStopMonitor(0,6000,0);TestHost.World.SetTrain("Station",-1);
      Send(c,19);B("Station").state=0x50000000+2+16384;Frame(c);
      Send(c,31);TestHost.World.SetTrain("Brake",-1);TestHost.World.SetTrain("Station",1);S("Station").park=false;
      Send(c,31);Check(S("Station").transport==1&&!TestHost.World.coaster.emergency,"disabled training injected a fault");
    });
    Test("Reserved tire gap cannot drive on a misaligned route",()=>{
      var c=Init(transit:true,storage:true);TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Table",0);
      TestHost.World.coaster.track.position=0;Send(c,19,mode:2,selected:38);Send(c,19+32+64,mode:2,selected:38);
      TestHost.World.coaster.track.position=-1;Send(c,19+32+64,mode:2,selected:38);
      Check(S("Table").transport==0&&S("Storage").transport==0,"misaligned reservation powered tire gap");
    });
    Test("Empty reserved lift still idles when enabled",()=>{
      var c=Init(transit:true);Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Table",0);Send(c,31);Send(c,31);
      Check(B("Lift").state==1&&S("Lift").lift==2,"empty approaching lift failed to idle");
    });
    Test("Startup offline does not fault; going offline after operation latches 909",()=>{
      var c=Init();TestHost.World.coaster.mode=-1;Frame(c);
      Check(!TestHost.World.coaster.emergency,"initial offline startup faulted");
      TestHost.World.coaster.mode=0;Frame(c);TestHost.World.coaster.mode=-1;Frame(c);
      Check(TestHost.World.coaster.emergency&&B("Station").state==0x20010000+909,"simulator offline/crash was not latched");
      Check(S("Station").transport==0&&S("Lift").lift==0,"offline fault left outputs running");
    });
    Test("Empty lift idles unowned and obeys panel Lift On/Off",()=>{
      var c=Init();S("Station").waitAdvance=false;Frame(c);Check(S("Lift").lift==2,"unowned empty lift did not idle");
      Send(c,3);Check(S("Lift").lift==0,"Lift Off left idle running");
      Send(c,19);Check(S("Lift").lift==2,"Lift On did not idle empty lift");
    });
    Test("Held restraint request reverses a stopped interrupted departure and parks",()=>{
      var c=Init(transit:true);S("Station").park=true;Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.SetTrain("Table",0);Send(c,23);Send(c,19+1024);
      Check(S("Station").transport==-1&&S("Table").transport==-1,"return request failed to reverse");
      Check(B("Station").state==8,"reversing warning missing");
      Send(c,19);Check(S("Station").transport==0&&S("Table").transport==0,"return release kept driving");
      Send(c,19+1024);S("Station").park=false;TestHost.World.SetTrain("Table",-1);Send(c,19+1024);
      Check(S("Station").transport==0&&S("Station").arrivals>0,"return failed to hand off parking");
    });
    Test("Station without an enabled tire return profile cannot reverse",()=>{
      var c=Init();Send(c,19);Send(c,31);Send(c,31);Send(c,19+1024);
      Check(S("Station").transport==0,"unsupported return drove station");
    });
    Test("Return refuses a train already occupying the next lift",()=>{
      var c=Init(transit:true);S("Station").park=true;Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.SetTrain("Table",0);Send(c,31);Send(c,31);TestHost.World.SetTrain("Lift",0);
      Send(c,19+1024);Check(S("Station").transport==0,"return pulled a train back from lift");
    });
    Test("Multi-move starts only when enabled and preserves held-button stops",()=>{
      var c=Init(transit:true);c.setMultiMove(10,2);Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.coaster.trains[0].speed=1;
      TestHost.World.coaster.trains[1].frontPos=0;TestHost.World.coaster.trains[1].rearPos=-50;
      Send(c,31);Check(B("Brake").state==3,"disabled multi-move advanced early");
      Send(c,31+512);Check(B("Station").state==10&&B("Brake").state==4,"enabled multi-move did not begin");
      TestHost.World.SetTrain("Table",0);
      S("Station").trains=new[]{TestHost.World.coaster.trains[0],TestHost.World.coaster.trains[1]};B("Station").count=2;
      Send(c,31+512);Check(!B("Station").warning&&!TestHost.World.coaster.emergency,"expected overlap faulted/warned");
      Send(c,23+512);Check(S("Station").transport==0&&S("Brake").transport==0&&S("Table").transport==0,"release left coupled outputs moving");
      TestHost.World.SetTrain("Station",1);S("Station").park=false;Send(c,31+512);
      Check(S("Station").transport==1,"station failed to continue parking after departing rear cleared");
    });
    Test("Multi-move separation loss faults instead of hiding an actual conflict",()=>{
      var c=Init(transit:true);c.setMultiMove(10,2);Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.coaster.trains[0].speed=1;TestHost.World.coaster.trains[1].frontPos=0;
      Send(c,31+512);TestHost.World.coaster.trains[1].frontPos=45;
      Send(c,31+512);Check(TestHost.World.coaster.emergency&&B("Station").state==536936448+906,"unsafe separation not faulted");
      Check(B("Station").warning&&S("Station").transport==0,"actual conflict was suppressed or kept moving");
    });
    Test("Station stop-limit overrun latches a numbered fault and stops every output",()=>{
      var c=Init();c.setStopMonitor(0,6000,.5f);S("Station").overshot=true;Frame(c);
      Check(TestHost.World.coaster.emergency&&B("Station").state==536936448+904,"overshoot not reported");
      Check(S("Station").transport==0&&S("Lift").lift==0,"overshoot left outputs moving");
    });
    Test("Stop monitor allows deceleration then faults a train that fails to stop",()=>{
      var c=Init();c.setStopMonitor(0,6000,0);TestHost.World.coaster.trains[0].speed=2;
      Send(c,1);Frame(c,3000);Check(!TestHost.World.coaster.emergency,"normal deceleration faulted early");
      Frame(c,4000);Check(TestHost.World.coaster.emergency&&B("Station").state==536936448+905,"failed stop not reported");
    });
    Test("Lift-to-block missing arrival faults after 70 simulation seconds",()=>{
      var c=Flying();Simulate(c,68);Check(!TestHost.World.coaster.emergency,"circuit faulted before deadline");
      TestHost.World.now+=120000;Simulate(c,0);Check(!TestHost.World.coaster.emergency,"paused wall time consumed flight timeout");
      Simulate(c,3);Check(TestHost.World.coaster.emergency&&B("Station").state==536936448+907,"missing arrival failed to fault");
    });
    Test("Block arrival cancels the flight timeout",()=>{
      var c=Flying();TestHost.World.SetTrain("Brake",0);Simulate(c,1);Simulate(c,71);
      Check(!TestHost.World.coaster.emergency,"completed flight timed out");
    });
    Test("Panel Manual lift on/off only drives the selected lift",()=>{
      var c=Init();TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Lift",0);S("Lift").trigger=false;
      Send(c,19,mode:1);Check(S("Lift").lift==0,"unselected manual lift ran");
      Send(c,19,mode:1,selected:33);Check(S("Lift").lift==1,"selected manual lift did not run");
      Send(c,3,mode:1,selected:33);Check(S("Lift").lift==0,"selected manual lift did not stop");
    });
    Test("Manual occupied lift accepts idle and returns to normal in Auto",()=>{
      var c=Init();TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Lift",0);S("Lift").trigger=false;
      Send(c,19,mode:1,selected:33);Check(S("Lift").lift==1,"manual preparation failed");
      B("Station").state=0x51000000+(33<<2)+1;Frame(c);
      Check(S("Lift").lift==2,"manual idle was ignored");
      Send(c,19,mode:0,selected:33);Check(S("Lift").lift==1,"Auto retained manual idle");
    });
    Test("Manual idle mailbox cannot configure a station as a lift",()=>{
      var c=Init();TestHost.World.SetTrain("Station",-1);TestHost.World.SetTrain("Lift",0);S("Lift").trigger=false;
      Send(c,19,mode:1,selected:33);B("Station").state=0x51000000+(34<<2)+1;Frame(c);
      Check(S("Lift").lift==1&&!TestHost.World.coaster.emergency,"non-lift selection affected output");
    });
    Test("Held buttons advance the next train as soon as station clears",()=>{
      var c=Init();Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.SetTrain("Station",-1);Send(c,31);Send(c,31);
      Check(B("Brake").state==4,"continuous hold failed to advance next train");
      Send(c,27);Check(S("Brake").transport==0,"released button failed to stop arrival");
    });
    Test("Loss stops first and keeps manual dispatch until stationary handback",()=>{
      var c=Init();Send(c,19);Send(c,31);Send(c,31);
      TestHost.World.coaster.trains[0].speed=5;Frame(c,900);
      Check(S("Station").brakes&&S("Station").manual,"expired lease reopened auto dispatch");
      Frame(c,1200);Check(S("Station").manual,"handback while moving");
      TestHost.World.coaster.trains[0].speed=0;Frame(c);
      Check(!S("Station").manual&&S("Station").transport==1,"auto did not resume after safe handback");
    });
    Test("Reconnect held and mode changes require a fresh release",()=>{
      var c=Init();Send(c,19);Send(c,31);Send(c,31);Frame(c,900);
      Send(c,31);Check(S("Station").transport==0,"held reconnect moved");
      Send(c,19);Send(c,31);Check(S("Station").transport==1,"rearmed reconnect failed");
      Send(c,31,1);Check(S("Station").transport==0,"held mode change moved");
    });
    Test("Forward storage branches do not block a correctly aligned main handback",()=>{
      var c=Init(transit:true,storage:true);c.addRoute(3,4,false,"Switch",0);
      Send(c,1);Frame(c,900);Frame(c,1200);
      Check(!S("Station").manual,"unaligned storage branch blocked main handback");
    });
    Test("Game manual selection during stopped handback is preserved",()=>{
      var c=Init();Send(c,1);Frame(c,900);
      GameMode(c,1);Frame(c,1200);
      S("Station").waitAdvance=true;Frame(c);
      Check(B("Station").fwd&&S("Station").departures==0,"handback discarded game manual choice");
    });
    Test("Unaligned or occupied destination refuses movement",()=>{
      var c=Init();TestHost.World.coaster.track.position=2;Send(c,19);Send(c,31);
      Check(S("Station").departures==0,"misaligned route departed");
      TestHost.World.coaster.track.position=3;TestHost.World.SetTrain("Lift",1);Send(c,31);
      Check(S("Station").departures==0,"occupied destination departed");
    });
    Test("Block check stops an already reserved move",()=>{
      var c=Init();Send(c,19);Send(c,31);Send(c,31);Send(c,159);
      Check(S("Station").transport==0&&S("Station").brakes,"hold did not stop movement");
      Send(c,31);Check(S("Station").transport==1,"cleared hold did not resume");
    });
    Test("Emergency stop remains latched through lease loss",()=>{
      var c=Init();Send(c,19);TestHost.World.coaster.emergency=true;Frame(c,900);Frame(c,2000);
      Check(S("Station").manual&&S("Station").brakes&&TestHost.World.coaster.emergency,"EStop cleared/handed back");
    });
    Test("Two different trains in one group fault before movement",()=>{
      var c=Init(true);TestHost.World.SetTrain("Table",1);Send(c,19);
      Check(TestHost.World.coaster.emergency&&S("Station").transport==0,"group conflict not stopped");
      Check(B("Station").state==536936448+902,"structured occupancy fault code missing");
    });
    Test("In-game Manual Block enables and executes safe forward button",()=>{
      var c=Init();GameMode(c,1);Frame(c);
      Check(B("Station").fwd&&S("Station").departures==0,"manual button unavailable/auto departure");
      c.onAdvanceFWDButton(B("Station"));Frame(c);
      Check(S("Station").departures==1&&S("Station").transport==1,"game forward callback did not run");
    });
    Test("Game forward callback cannot bypass panel held buttons",()=>{
      var c=Init();Send(c,19);GameMode(c,1);c.onAdvanceFWDButton(B("Station"));Frame(c);
      Check(S("Station").departures==0,"game input bypassed panel ownership");
    });
    Test("Full Manual device controls survive; Auto waits for stop and resumes",()=>{
      var c=Init();GameMode(c,2);S("Station").transport=-1;Frame(c);
      Check(S("Station").transport==-1,"script overwrote game manual device");
      TestHost.World.coaster.trains[0].speed=2;GameMode(c,0);Frame(c);
      Check(S("Station").transport==0,"Auto did not wait for stop");
      TestHost.World.coaster.trains[0].speed=0;Frame(c);S("Station").waitAdvance=true;Frame(c);Frame(c);
      Check(S("Station").transport==1,"Auto did not reconcile and resume");
    });
    Test("External native mode changes work without NLVM notifications",()=>{
      var c=Init();GameMode(c,1,notify:false);Frame(c);
      Check(B("Station").fwd&&S("Station").departures==0,"mode query failed to detect Manual Block");
      GameMode(c,2,notify:false);Frame(c);S("Station").transport=-1;Frame(c);
      Check(S("Station").transport==-1,"mode query failed to detect Full Manual");
      GameMode(c,0,notify:false);Frame(c);S("Station").waitAdvance=true;Frame(c);Frame(c);
      Check(S("Station").transport==1,"mode query failed to restore Auto");
    });
    Test("Invalid profile fails initialization with emergency stop",()=>{
      TestHost.World.Reset();TestHost.World.invalid=true;var c=new PanelController();
      Check(!c.onInit()&&TestHost.World.coaster.emergency,"invalid profile accepted");
    });
    Console.WriteLine(passed+" controller acceptance tests passed (fixture, not physics).");
  }
}
