// Minimal NL2Bridge client for Node.js (no dependencies) + a small monitor.
//   node nl2bridge.mjs [host] [port]
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

export class NL2Bridge {
  constructor(host = "127.0.0.1", port = 15152) {
    this.host = host; this.port = port; this.req = 0;
    this.buf = Buffer.alloc(0); this.pending = new Map();
  }

  connect() {
    return new Promise((resolve, reject) => {
      this.sock = net.connect(this.port, this.host, resolve);
      this.sock.setNoDelay(true);
      this.sock.on("error", reject);
      this.sock.on("data", (d) => this.#onData(d));
    });
  }

  close() { this.sock.end(); }

  #onData(d) {
    this.buf = Buffer.concat([this.buf, d]);
    while (this.buf.length >= 10) {
      const id = this.buf.readUInt16BE(1), req = this.buf.readUInt32BE(3), size = this.buf.readUInt16BE(7);
      if (this.buf.length < size + 10) break;
      const data = this.buf.subarray(9, 9 + size);
      this.buf = this.buf.subarray(size + 10);
      const p = this.pending.get(req);
      if (!p) continue;
      this.pending.delete(req);
      if (id === 2) p.reject(new Error(data.toString("utf8")));
      else p.resolve({ id, data });
    }
  }

  // One request: 'N' | u16 msgId | u32 requestId | u16 size | data | 'L'  (big-endian)
  call(msgId, data = Buffer.alloc(0)) {
    const req = ++this.req;
    const head = Buffer.alloc(9);
    head.write("N", 0); head.writeUInt16BE(msgId, 1); head.writeUInt32BE(req, 3); head.writeUInt16BE(data.length, 7);
    this.sock.write(Buffer.concat([head, data, Buffer.from("L")]));
    return new Promise((resolve, reject) => this.pending.set(req, { resolve, reject }));
  }

  static i32(...v) { const b = Buffer.alloc(4 * v.length); v.forEach((x, i) => b.writeInt32BE(x, 4 * i)); return b; }
  async json(msgId, data) { return JSON.parse((await this.call(msgId, data)).data.toString("utf8")); }
  async int(msgId, data) { return (await this.call(msgId, data)).data.readInt32BE(0); }

  bridgeInfo() { return this.json(1001); }
  coasters() { return this.json(1200); }
  blocks(c) { return this.json(1201, NL2Bridge.i32(c)); }
  stations(c) { return this.json(1203, NL2Bridge.i32(c)); }
  trains(c) { return this.json(1205, NL2Bridge.i32(c)); }
  setBlockMode(c, mode) { return this.int(1130, NL2Bridge.i32(c, { auto: 0, manual: 1, fullmanual: 2 }[mode])); }
  estop(c, on) { return this.call(1131, Buffer.concat([NL2Bridge.i32(c), Buffer.from([on ? 1 : 0])])); }
  advance(c, sectionId, backwards = false) { return this.int(1110, NL2Bridge.i32(c, sectionId, backwards ? 2 : 1)); }
  stationOp(c, station, op) {            // op: 0 manual, 1 auto, 2 dispatch, 3/4 gates open/close, 5/6 harness open/close
    return this.call(1141, NL2Bridge.i32(c, station, op));
  }
  deviceParams(c, sectionId) { return this.json(1208, NL2Bridge.i32(c, sectionId)); }
  setDeviceParams(c, sectionId, device, speed = -1, accel = -1, decel = -1) {   // device 1 lift, 2 transport
    const b = Buffer.alloc(36);
    b.writeInt32BE(c, 0); b.writeInt32BE(sectionId, 4); b.writeInt32BE(device, 8);
    b.writeDoubleBE(speed, 12); b.writeDoubleBE(accel, 20); b.writeDoubleBE(decel, 28);
    return this.call(1152, b);
  }

  // Fast binary block poll (reply 1211)
  async blockStates(c) {
    const { data } = await this.call(1210, NL2Bridge.i32(c));
    const out = [], n = data.readUInt16BE(2);
    for (let i = 0; i < n; i++) {
      const o = 4 + 16 * i;
      out.push({ id: data.readUInt32BE(o), state: data[o + 4], trains: data[o + 5], lamp: data[o + 6],
                 isBlock: !!(data[o + 7] & 4), canAdvanceFwd: !!(data[o + 7] & 1), trainMask: data.readUInt32BE(o + 12) });
    }
    return { blockMode: data[0], estop: !!data[1], blocks: out };
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1])) {
  const nl = new NL2Bridge(process.argv[2] ?? "127.0.0.1", Number(process.argv[3] ?? 15152));
  await nl.connect();
  console.log(await nl.bridgeInfo());
  const [coaster] = await nl.coasters();
  console.log(`coaster 0: ${coaster.name}`);
  const names = Object.fromEntries((await nl.blocks(0)).map((b) => [b.id, b.name]));
  for (let i = 0; i < 10; i++) {
    const s = await nl.blockStates(0);
    const busy = s.blocks.filter((b) => b.isBlock && b.trains).map((b) => names[b.id]);
    console.log(`mode ${s.blockMode} estop ${s.estop} occupied: ${busy.join(", ")}`);
    await new Promise((r) => setTimeout(r, 500));
  }
  nl.close();
}
