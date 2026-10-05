// NL2Bridge - offsets and signatures for NoLimits 2 (Steam build, nolimits2stm.exe)
// Reverse-engineered from nolimits2stm.exe (PE timestamp 0x696F4E97 / 2026-01-20, 17,923,144 bytes).
// All addresses are RVAs relative to the module base (image base 0x140000000, no ASLR).
// Each function also carries a byte signature (?? = wildcard). At startup the bridge checks
// the bytes at the RVA; if they don't match (game updated) it scans .text for the pattern.
#pragma once
#include <cstdint>

namespace nl2 {

struct Sig { const char* name; uint32_t rva; const char* pattern; };

// ---- Functions -------------------------------------------------------------
// void  Lock(Benaphore*)                         -- global sim/script lock (NOT recursive)
// void  Unlock(Benaphore*)
// bool  SectionSet(BlockSystem*, uint secId, int cmd, int param)                 (script: internSetSectionNodeState)
// bool  SectionGet(BlockSystem*, uint secId, int cmd, SectionGetResult* inout)   (script: internGetSectionNodeState[Double])
// bool  StationOp(Station*, int op)
// int   StationCan(Station*, int op)
// Station* StationAt(NLCoaster*, int idx)
// void  SwitchSet(NLSpecialTrack*, int dir, bool immediate)   (immediate=false -> animated move)
// void  SetBlockMode(NLCoaster*, char mode)   mode 1=Auto 2=Manual block 3=Full manual
// bool  CanAutoOrManual(NLCoaster*, std::string* err, bool manual, bool force)
// bool  CanFullManual(NLCoaster*, std::string* err, bool force)
// void  SetEStop(NLCoaster*, bool on)
static const Sig kSigs[] = {
  {"Lock",            0x53f290, "44 8B 41 04 41 B9 01 00 00 00 45 85 C0 74 ?? 90"},
  {"Unlock",          0x53f870, "83 C8 FF F0 0F C1 01 83 E8 01 7E ?? 48 8B 49 08 48 FF 25 ?? ?? ?? ??"},
  {"SectionSet",      0x0ab910, "48 89 5C 24 08 48 89 6C 24 10 48 89 74 24 18 48 89 7C 24 20 41 56 48 83 EC 20 48 8B 71 18"},
  {"SectionGet",      0x0a6460, "48 89 5C 24 10 48 89 74 24 18 48 89 7C 24 20 41 56 48 83 EC 20 48 8B 79 18"},
  {"StationOp",       0x3e26f0, "48 89 5C 24 08 57 48 83 EC 20 33 FF 48 8B D9 83 FA 0A"},
  {"StationCan",      0x3e2b00, "48 89 5C 24 08 57 48 83 EC 30 0F 29 74 24 20 48 8B D9 83 FA 11"},
  {"StationAt",       0x4279b0, "48 83 EC 28 4C 8B C9 49 BA AB AA AA AA AA AA AA 2A 48 63 CA 49 8B C2 49 8B 91 90 01 00 00"},
  {"SwitchSet",       0x47cc40, "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 20 48 8B 01 41 0F B6 F0"},
  {"SetBlockMode",    0x0cf7e0, "40 55 41 56 48 83 EC 28 80 B9 6E A0 01 00 00 0F B6 EA"},
  {"CanAutoOrManual", 0x0bbbe0, "48 89 54 24 10 48 89 4C 24 08 55 41 55 48 81 EC B8 00 00 00"},
  {"CanFullManual",   0x0bc2b0, "48 89 5C 24 08 57 48 83 EC 20 80 B9 6E A0 01 00 00"},
  {"SetEStop",        0x0d00b0, "88 91 70 A0 01 00 C3 CC CC CC CC CC CC CC CC CC"},
  // Anchor used to locate the g_pSim global: script native Simulator.getCoasterCount()
  //   ... E8 ?? ?? ?? ?? 48 8B 05 <disp32 -> g_pSim>
  {"NatCoasterCount", 0x37a570, "48 89 4C 24 08 48 83 EC 28 E8 ?? ?? ?? ?? 48 8B 05 ?? ?? ?? ?? 33 C9"},
  // Anchor used to locate the global lock object: script native internSetSectionNodeState
  //   ... 4C 8D 3D <disp32 -> g_Lock>   (lea r15,[rip+X] at +0x5E)
  {"NatSetSection",   0x37e9a0, "48 89 4C 24 08 55 56 57 41 56 41 57 48 83 EC 30 48 C7 44 24 20 FE FF FF FF 48 89 5C 24 70 E8 ?? ?? ?? ?? 8B F0"},
};

// ---- Optional: track sensors / triggers (sensors.inc). Missing ones only disable sensors. ----------
// void NatAddTrigger(vm)                  script native nativeAddTrackTriggerAtPositionWithOffset (hooked)
// void EventsConsumed(NLCoaster*)          erases this frame's trigger events after scripts ran (hooked)
// void** VmStorageGet(void** vmHolder, void** out, const void* key)
// uint8_t* EntityObjForId(uint32_t entityId, void* vm)   -> scene object
// NatParentEntity                          script native returning the owning entity (anchor for its key)
static const Sig kSensorSigs[] = {
  {"NatAddTrigger",   0x37f480, "48 89 4C 24 08 55 53 56 57 41 56 48 8B EC 48 81 EC 80 00 00 00 48 C7 45 B0 FE FF FF FF 0F 29 74"},
  {"EventsConsumed",  0x0c9ea0, "48 83 EC 28 4C 8B 89 60 A0 01 00 4D 85 C9 74 ?? 4C 8D 91 68 01 00 00"},
  {"VmStorageGet",    0x37b7e0, "40 53 48 83 EC 30 48 8B 09 48 8B DA C7 44 24 20 00 00 00 00 E8 ?? ?? ?? ?? 48 8B C3 48 83 C4 30"},
  {"EntityObjForId",  0x37fce0, "48 89 54 24 10 57 48 83 EC 30 48 C7 44 24 20 FE FF FF FF 48 89 5C 24 40 48 8B C2 8B F9 4C 8D 05"},
  {"NatParentEntity", 0x3800b0, "48 89 4C 24 08 53 48 83 EC 40 48 C7 44 24 30 FE FF FF FF 33 DB E8 ?? ?? ?? ?? 4C 8D 05"},
};
constexpr uint32_t ANCHOR_TRIGKEY_DISP_OFF   = 0x1b8; // NatAddTrigger +0x1B5: lea r8,[rip+X] -> NLScriptTrackTriggers VM key
constexpr uint32_t ANCHOR_PARENTKEY_DISP_OFF = 0x1d;  // NatParentEntity +0x1A: lea r8,[rip+X] -> parent entity VM key
// First 14 bytes of each hooked function (whole instructions, no RIP-relative operands).
static const uint8_t HOOK_EVENTS_PROLOGUE[14]      = {0x48,0x83,0xEC,0x28,0x4C,0x8B,0x89,0x60,0xA0,0x01,0x00,0x4D,0x85,0xC9};
static const uint8_t HOOK_ADD_TRIGGER_PROLOGUE[14] = {0x48,0x89,0x4C,0x24,0x08,0x55,0x53,0x56,0x57,0x41,0x56,0x48,0x8B,0xEC};

namespace trig {
  constexpr int CoasterTracksBegin = 0xd8, CoasterTracksEnd = 0xe0;       // std::vector<NLTrack*>
  constexpr int CoasterEventsBegin = 0x168, CoasterEventsEnd = 0x170;     // std::vector<CoasterEvent>
  constexpr int CoasterEventsConsumed = 0x1a060;                          // size_t, events handed to scripts this frame
  constexpr int TrackPointsBegin = 0x150, TrackPointsEnd = 0x158;         // std::vector<NLTrackTriggerPoint*> (and other points)
  constexpr int PointStepCoord = 0x20;   // double
  constexpr int PointName      = 0x50;   // std::string (usually empty for scripted sensors)
  constexpr int PointTrigger   = 0x70;   // NLTrackTrigger*
  constexpr int TriggerId      = 0x10;   // u32
  constexpr int TriggerPoint   = 0x48;   // back-pointer to NLTrackTriggerPoint
  constexpr int TrainIndex     = 0x6be;  // u8 on NLTrain
  constexpr int ScriptTriggersVec = 0x10; // NLScriptTrackTriggers: std::vector<NLTrackTriggerPoint*>
  constexpr int ScriptEntityId = 0x10;   // NLScriptEntity: u32 entity id (as read by NatParentEntity)
  constexpr int SceneObjectName = 0x30;  // std::string on the scene object
}
struct CoasterEvent { void* trigger; void* train; uint32_t a; uint32_t type; }; // 0x18 bytes; type 1/2 = enter/leave

// ---- Optional helpers (missing ones only degrade the matching JSON fields) ----------------------
// bool DeviceActive(StationDevice*)   platform / seat-locker device is configured on this station (0x14013EF60)
static const Sig kExtraSigs[] = {
  {"DeviceActive",    0x13ef60, "48 83 EC 38 0F 29 74 24 20 0F 57 F6 66 0F 2F 71 20 72 ?? E8 ?? ?? ?? ?? 66 0F 2F C6 77"},
  // Script native Coaster.requestReset: sets coaster::ResetRequest then xchg's g_ResetPending (disp32 at +0x67)
  {"NatRequestReset", 0x370c80, "48 89 5C 24 10 57 48 83 EC 20 48 89 91 C0 00 00 00 48 8B F9 48 8B 5A 10 E8 ?? ?? ?? ?? 4C 8D 43 18 89 44 24 30 8B D0 48 8B CF E8 ?? ?? ?? ?? 48 8B D7 8B C8 E8 ?? ?? ?? ?? 48 8B D8 48 85 C0 74 2A 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? 48 8D 0D ?? ?? ?? ?? C6 83 77 A0 01 00 01 E8 ?? ?? ?? ?? B8 01 00 00 00 86 05"},
};
constexpr uint32_t ANCHOR_RESET_DISP_OFF = 0x67;

// ---- Globals (RVA, used only as a fallback if the anchors fail) --------------
constexpr uint32_t RVA_g_pSim = 0x1011a00;   // NLSim** ; *g_pSim -> sim, *(sim) -> NLPark*
constexpr uint32_t RVA_g_Lock = 0x10144e8;   // benaphore {int count; int spin; HANDLE evt;}
constexpr uint32_t ANCHOR_SIM_DISP_OFF  = 0x11; // offset of disp32 inside NatCoasterCount (mov rax,[rip+X])
constexpr uint32_t ANCHOR_LOCK_DISP_OFF = 0x61; // offset of disp32 inside NatSetSection (lea r15,[rip+X] @ +0x5E)

// ---- Structure offsets -----------------------------------------------------
namespace sim  { constexpr int PlayModeA = 0x1aa; }          // byte, must be !=0 in play mode
namespace park { constexpr int PlayModeB = 0x92;            // byte, must be !=0 in play mode
                 constexpr int CoastersBegin = 0x98, CoastersEnd = 0xa0; } // std::vector<NLCoaster*>
namespace coaster {
  constexpr int Name           = 0x68;    // std::string (MSVC)
  constexpr int SpecialBegin   = 0xc0, SpecialEnd = 0xc8;    // std::vector<NLSpecialTrack*> (switches/transfer tables)
  constexpr int TrainsBegin    = 0x120, TrainsEnd = 0x128;   // std::vector<NLTrain*>
  constexpr int BlockSystem    = 0x180;   // NLBlockSystem*
  constexpr int StationsBegin  = 0x188, StationsEnd = 0x190; // 0x18-byte elements, use StationAt()
  constexpr int OperationMode  = 0x290;   // byte: 1 = shuttle, 2 = scripted operation, else normal
  constexpr int BlockMode      = 0x291;   // byte: 1 = auto, 2 = manual block, 3 = full manual
  constexpr int Ready          = 0x1a06e; // byte: coaster initialised / not "needs reset"
  constexpr int Busy           = 0x1a06f; // byte: mode change not allowed while set
  constexpr int EStop          = 0x1a070; // byte: emergency stop active
  constexpr int ResetRequest   = 0x1a077; // byte: sim loop resets the coaster (trains to start) when g_ResetPending is set
}
namespace blocksys {
  constexpr int NodesBegin = 0x00, NodesEnd = 0x08;   // std::vector<NLBlockNode*>
  constexpr int Entries    = 0x18;                    // SectionEntry* (sorted by id)
  constexpr int EntryCount = 0x20;                    // size_t
}
struct SectionEntry { uint32_t id; uint32_t pad; void* node; uint8_t* section; }; // 0x18 bytes
namespace node {
  constexpr int TrainCount = 0x4c;  // byte - trains currently assigned to block
  constexpr int State      = 0x4d;  // byte - internal state enum (see BlockStateName)
  constexpr int IsBlock    = 0x480; // byte
  constexpr int Section    = 0x38;  // NLSection*
  // vtable slots
  constexpr int VF_GetStateText = 0x08; // void (node, std::string* out /*nullable*/, uint8_t* lamp)
  constexpr int VF_SetState     = 0x58; // bool (node, int cmd, uint param) ; cmd 7 = semi-manual move (bit0 fwd, bit1 bwd)
  constexpr int VF_IsScripted   = 0x88; // bool (node)
  constexpr int VF_GetStation   = 0x90; // Station* (node) or null
  constexpr int VF_SemiManual   = 0xa8; // normal nodes: int (node, op) 1=canFwd 2=doFwd 4=canBwd 5=doBwd
                                        // scripted nodes: 0=fwd visible, 1=canFwd, 2=PRESS fwd,
                                        //                 3=bwd visible, 4=canBwd, 5=PRESS bwd
                                        // PRESS pushes coaster event 6/7. Status polls read flags directly.
  // Scripted block nodes only (NLScriptedBlockNode / NLScriptedStationBlockNode), script Block API:
  constexpr int VF_RegisterState = 0x60; // void (node, int state, const char* text, uint8 lamp 0 off/1 on/2 flash)
  constexpr int VF_SetUserState  = 0x68; // void (node, int state)     Block.setState
  constexpr int VF_GetUserState  = 0x70; // int  (node)                Block.getState
  constexpr int UserState  = 0x498;      // int, current scripted state id
  constexpr int FwdVisible = 0x49c, FwdEnabled = 0x49d, BwdVisible = 0x49e, BwdEnabled = 0x49f; // bytes
}
namespace section {
  constexpr int BrakeDev     = 0x30;
  constexpr int LiftDev      = 0x38;
  constexpr int TransportDev = 0x40;
  constexpr int Name         = 0x80;  // std::string
  constexpr int BrakeMode    = 0x36c; // 0 off(open) 1 on(closed) 2 trim
  constexpr int TransportMode= 0x36d; // 0 off 1 standard 2 depends-on-brake
  constexpr int LiftMode     = 0x36e; // 0 off 1 fwd 2 fwd-idle 3 bwd
  constexpr int TrackStart   = 0x1d8; // double, metres along Track where the section starts
  constexpr int TrackEnd     = 0x1e0; // double
  constexpr int Track        = 0x1e8; // NLTrack* (index into coaster tracks vector trig::CoasterTracksBegin)
  constexpr int Id           = 0x878; // uint32
}
namespace device {                   // NLBrakeDevice / NLLiftDevice / NLTransportDevice (section::BrakeDev etc.)
  constexpr int State       = 0x21;  // byte: brake/lift 0 off 1 on; transport 0 off 1 fwd 2 bwd
  constexpr int VF_SetState = 0x38;  // void (dev, u8 state)  - what the full-manual control panel calls
  // Lift (NLLiftDevice -> NLChainDevice at +0x48) and transport (NLTransportDevice) parameters, m/s and m/s^2.
  // SetParams stores |speed|,|accel|,|decel| at +0x28/+0x30/+0x38; the lift also passes them to its chain drive
  // (which ramps to the new speed) and rescales the chain animation / sound rate (0x14013F570).
  constexpr int VF_SetParams   = 0x40;  // void (dev, double speed, double accel, double decel)
  constexpr int VF_CurrentSpeed= 0x48;  // double (dev)
  constexpr int Speed = 0x28, Accel = 0x30, Decel = 0x38;
  constexpr int LiftChain      = 0x48;  // NLLiftDevice: NLChainDevice* (own speed/accel/decel copy; this one drives)
  // NLChainDevice update (vtbl+0x28, 0x14013F9A0): target = +speed (state 1) / -speed (2) / 0; with the idle flag set
  // the target is scaled by a game constant and clamped (idle chain ~0.4 m/s - not configurable). The current speed
  // (+0x40) ramps to the target with accel (+0x30) / decel (+0x38).
  constexpr int ChainCurrent   = 0x40;  // double
  constexpr int ChainIdleFlag  = 0x68;  // byte
}
// NLCustomTrack (coaster+0xD8 vector) end connections: {object*, i32 port}. The object is the special track
// (switch / transfer table) or track the end is joined to; port = the special track position (direction index) that
// lines up with this track. Not valid on NLBranchTrack (the moving part of a switch/table, different layout).
namespace track {
  constexpr int EndLink   = 0x1f8, EndPort   = 0x200;  // where trains go after the track's end
  constexpr int StartLink = 0x208, StartPort = 0x210;  // where trains come from before the track's start
}
namespace special {
  constexpr int Name          = 0x98;  // std::string ("Unnamed Special Track 1")
  constexpr int VF_DirCount   = 0x28;  // int (track)
  constexpr int CurrentDir    = 0x70;  // int, -1 while moving
  constexpr int TargetDir     = 0x78;  // int
  constexpr int Switchable1   = 0x8c;  // byte
  constexpr int Switchable2   = 0x8d;  // byte
  constexpr int ManualAllowed = 0x8e;  // byte
}
// NLCoaster+0x188 vector elements (0x18 bytes): { NLBlockNode* node; Station* station; NLSection* section; }
struct StationEntry { void* node; uint8_t* station; uint8_t* section; };
namespace station {
  constexpr int Section      = 0x00;  // NLSection* (name at section::Name)
  constexpr int Train        = 0x10;  // NLTrain* currently handled by the station (or null)
  constexpr int State        = 0x40;  // byte: 0x15..0x17 = train stopped and ready for gates/harness/floor ops
  constexpr int AutoDispatch = 0x41;  // byte: 1 = automatic dispatch, 0 = manual
  constexpr int HasFlyer     = 0x46;  // byte: flyer seats present
}
namespace section {
  constexpr int PlatformDev = 0x68;   // NLStationPlatformDevice* (floorless floor / platform)
  constexpr int FlyerDev    = 0x70;   // NLStationSeatLockerDevice*
  constexpr int GatesDev    = 0x78;   // NLStationGatesDevice*
}

namespace train {
  constexpr int FrontPos = 0x00, FrontTrack = 0x08;    // double metres along NLTrack*
  constexpr int RearPos  = 0x10, RearTrack  = 0x18;
  constexpr int CenterPos = 0x20, CenterTrack = 0x28;
  constexpr int HoldingNode   = 0x4d0; // NLBlockNode* the block system has assigned the train to
  constexpr int Speed         = 0x520; // double m/s (negative = backwards)
  constexpr int Accel         = 0x530; // double m/s^2
  constexpr int Index         = 0x6be; // byte
  constexpr int FlyerPos      = 0x580; // float: 0 = seats unlocked (loading), 1 = locked (flying)
  constexpr int HarnessPos    = 0x584; // float: 0 = closed, 1 = open
  constexpr int Lashed        = 0x6c3; // byte: 'Lashed To Track' (required on storage tracks, forbidden elsewhere, for auto/manual block mode)
  constexpr int MoveTrain     = 0x6c4; // byte: 'Move Train' enabled (cleared when lashing) - see Train cmd fn 0x14049d5e0 (0 lash, 1 unlash)
  constexpr int FlyerMotion   = 0x6c5; // byte: 1 = locking, 2 = unlocking
  constexpr int HarnessMotion = 0x6c6; // byte: 1 = closing, 2 = opening
  constexpr int HarnessOpenSpeed  = 0x570; // float, harness position units per second while opening
  constexpr int HarnessCloseSpeed = 0x574; // float, while closing
  constexpr int GearsBegin = 0x450, GearsEnd = 0x458;  // std::vector<shared_ptr<NLASteeringGear>>, placed along the train (16-byte elements)
  constexpr int CarsBegin  = 0x468, CarsEnd  = 0x470;  // std::vector<shared_ptr<car>> front to back (NLFrontBalancedCar, NLBackBalancedCar, ...)
}
// Car / steering-gear object (first pointer of each element above). The game animates every car's restraints
// separately, from the train-wide train::HarnessPos/HarnessMotion (CarRestraintAnim).
namespace car {
  constexpr int ModelsBegin = 0x08, ModelsEnd = 0x10; // car model instances (0x38 bytes each). Empty when the park hides the
                                                     // NL2 train and draws its own with a script (CarRestraintAnim skips it)
  constexpr int Train       = 0x20;              // NLTrain*
  constexpr int AnimBegin   = 0x28, AnimEnd = 0x30;  // restraint animation groups (0x28 bytes: {nodes vector, .., scale, cache})
  constexpr int SeatsBegin  = 0x40, SeatsEnd = 0x48; // std::vector<Seat*>: one entry per seat (rider camera). Train::SeatedCarCount
                                                     // (0x140497de0) and the "%s (Train %d, Car %d, Seat %d)" camera list use it
  constexpr int NodesBegin  = 0x58, NodesEnd = 0x60; // restraint nodes animated directly
  constexpr int Offset      = 0x1d0;             // double (steering gears): -(distance behind the train front)
}
// void CarRestraintAnim(car*)  (hooked for per-row restraints). Prologue is 17 bytes of whole,
// position-independent instructions (push r14 / sub rsp,70 / mov rax,[rcx+10] / mov r14,rcx / cmp [rcx+8],rax).
static const Sig kRowSig = {"CarRestraintAnim", 0x4a1f90,
  "41 56 48 83 EC 70 48 8B 41 10 4C 8B F1 48 39 41 08 0F 84 ?? ?? ?? ?? 48 8B 41 20 4C 89 64 24 68 45 32 E4 "
  "4C 89 7C 24 60 45 32 FF 0F 29 74 24 50 0F B6 88 C6 06 00 00 0F 29 7C 24 40 F3 0F 10 B8 84 05 00 00"};
static const uint8_t HOOK_CAR_ANIM_PROLOGUE[17] = {0x41,0x56,0x48,0x83,0xEC,0x70,0x48,0x8B,0x41,0x10,0x4C,0x8B,0xF1,0x48,0x39,0x41,0x08};
namespace device {                     // NLStationGatesDevice / NLStationPlatformDevice
  constexpr int Travel     = 0x20;     // double, >0 when configured
  constexpr int Pos        = 0x30;     // double: gates 0 closed .. 1 open; platform 0 lowered .. 1 raised
  constexpr int GateOpening = 0x50, GateClosing = 0x51;   // bytes (gates)
  constexpr int PlatformMotion = 0xa8; // byte (platform): 1 = lowering, 2 = raising
}

// StationCan(station, q) query codes, taken from the jump table at 0x1403E2B00.
// Even codes = "can do" (same as the official telemetry station-state bits), odd codes = current position.
enum StationQuery : int {
  SQ_AUTO = 0, SQ_CAN_DISPATCH = 1,
  SQ_CAN_OPEN_GATES = 2, SQ_GATES_OPEN = 3, SQ_CAN_CLOSE_GATES = 4, SQ_GATES_CLOSED = 5,
  SQ_CAN_OPEN_HARNESS = 6, SQ_HARNESS_OPEN = 7, SQ_CAN_CLOSE_HARNESS = 8, SQ_HARNESS_CLOSED = 9,
  SQ_CAN_RAISE_PLATFORM = 10, SQ_PLATFORM_RAISED = 11, SQ_CAN_LOWER_PLATFORM = 12, SQ_PLATFORM_LOWERED = 13,
  SQ_CAN_UNLOCK_FLYER = 14, SQ_FLYER_UNLOCKED = 15, SQ_CAN_LOCK_FLYER = 16, SQ_FLYER_LOCKED = 17,
};

// Scripted block nodes (NLScripted*BlockNode) accept these SetState (vtbl+0x58) commands, param 0/1:
enum ScriptedBlockCmd : int { SB_ADV_FWD_VISIBLE = 1, SB_ADV_BWD_VISIBLE = 2, SB_ADV_FWD_ENABLED = 3,
  SB_ADV_BWD_ENABLED = 4, SB_FLAG_4F = 5 /* node+0x4F, meaning unconfirmed */ };

// Internal block state (node+0x4d) -> text, taken from the game's own debug strings.
inline const char* BlockStateName(uint8_t s) {
  static const char* n[] = {"No Block","Offline","Idle","Reserved","Approaching Fwd","Approaching Bwd",
    "Leaving Fwd","Leaving Fwd to Ourself","Leaving Bwd","Leaving Bwd to Ourself","Full Manual Mode",
    "Passing Fwd to Trigger","Passing Bwd to Trigger","Station","Waiting for Clear Block",
    "Waiting for Advance","Complete Stopping","Wait-Time Pause","Pass Through","Scripted"};
  return s < sizeof(n)/sizeof(n[0]) ? n[s] : "Unknown";
}

// SectionSet command codes (same numbers the script VM uses)
// Scripted operation only (all need coaster OperationMode == 2):
enum SectionCmd : int {
  CMD_STATION_NEXT_CLEAR = 2, CMD_STATION_NEXT_OCCUPIED = 3,  // station state 0x15->0x16 / 0x16,0x17->0x15
  CMD_STATION_LEAVING = 5,
  CMD_BRAKES_OFF = 7, CMD_BRAKES_ON = 8, CMD_BRAKES_TRIM = 12,
  CMD_TRANSPORT_OFF = 9, CMD_TRANSPORT_FWD = 10, CMD_TRANSPORT_BWD = 11,
  CMD_TRANSPORT_FWD_DEP_BRAKE = 13, CMD_TRANSPORT_BWD_DEP_BRAKE = 14,
  CMD_LAUNCH_FWD = 17, CMD_LAUNCH_BWD = 18,
  CMD_STATION_ENTERING = 21,
  CMD_LIFT_FWD = 23, CMD_LIFT_BWD = 24, CMD_LIFT_OFF = 25, CMD_LIFT_FWD_IDLE = 41,
  CMD_STATION_MANUAL_DISPATCH_MODE = 44,
};
// SectionGet query codes (jump table at 0x1400A6D60). Position queries fail while the station handler owns the train.
enum SectionQuery : int {
  SG_STATION_WAIT_CLEAR = 1, SG_STATION_WAIT_ADVANCE = 4, SG_TRAIN_INDEX = 6 /* -1 none */,
  SG_BEFORE_BRAKE_TRIG = 15, SG_BEHIND_BRAKE_TRIG = 16, SG_BRAKE_COMPLETE_STOP = 19, SG_BRAKE_WAIT_TIME = 20 /*d*/,
  SG_IS_STATION = 22, SG_BEHIND_END = 26 /*d in: dist, d out 0/1*/, SG_BEFORE_START = 27 /*d in: dist*/,
  SG_TRAIN_MASK = 28, SG_BEFORE_CENTER = 32, SG_BEHIND_CENTER = 33, SG_BEFORE_LIFT_TRIG = 34, SG_BEHIND_LIFT_TRIG = 35,
  SG_GATE_STATE = 36 /*d*/, SG_PLATFORM_STATE = 37 /*d*/, SG_TRANSPORT_SPEED = 38 /*d*/, SG_BRAKES_ON = 39 /*d*/,
  SG_LIFT_SPEED = 40 /*d*/, SG_IS_LIFT = 42, SG_MANUAL_DISPATCH = 44,
  // 46..70 even: StationCan(q) for q = 1,6,8,2,4,10,12,14,16,7,9,3,5,11,13,17,15
};
// SectionGet result block: the game writes either the double or the int.
struct SectionGetResult { double d; int32_t i; int32_t pad; };

// Station ops (StationOp / telemetry use the same numbers). 3..10 need manual dispatch mode and a train
// stopped in the station; each is refused unless the matching StationCan query (in brackets) allows it.
enum StationOpCode : int { ST_MANUAL = 0, ST_AUTO = 1, ST_DISPATCH = 2 /*[1]*/,
  ST_GATES_OPEN = 3 /*[2]*/, ST_GATES_CLOSE = 4 /*[4]*/, ST_HARNESS_OPEN = 5 /*[6]*/, ST_HARNESS_CLOSE = 6 /*[8]*/,
  ST_PLATFORM_RAISE = 7 /*[10]*/, ST_PLATFORM_LOWER = 8 /*[12]*/,   // floorless: 8 drops the floor, 7 raises it
  ST_FLYER_UNLOCK = 9 /*[14]*/, ST_FLYER_LOCK = 10 /*[16]*/ };

} // namespace nl2
