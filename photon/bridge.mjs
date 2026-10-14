import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { Spectrum } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";
import { inboundText, novaReply } from "./bridge-core.mjs";

const dataDir = process.env.NOVA_DATA_DIR || path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "NovaNative");
const lockPath = path.join(dataDir, "photon-bridge.lock");
for (let attempt = 0; attempt < 2; attempt++) {
  try {
    const lock = fs.openSync(lockPath, "wx");
    fs.writeFileSync(lock, String(process.pid));
    fs.closeSync(lock);
    process.on("exit", () => {
      try {
        if (fs.readFileSync(lockPath, "utf8") === String(process.pid)) fs.unlinkSync(lockPath);
      } catch { /* A later bridge may own the lock. */ }
    });
    break;
  } catch (error) {
    if (error?.code !== "EEXIST") throw error;
    const pid = Number(fs.readFileSync(lockPath, "utf8"));
    let running = false;
    if (Number.isInteger(pid) && pid > 0) {
      try { process.kill(pid, 0); running = true; }
      catch (checkError) { running = checkError?.code === "EPERM"; }
    }
    if (running) process.exit(0);
    fs.unlinkSync(lockPath);
    if (attempt === 1) throw new Error("Could not acquire Nova Photon bridge lock");
  }
}
const configPath = path.join(dataDir, "photon.json");
const statePath = path.join(dataDir, "photon-state.json");
const statusPath = path.join(dataDir, "photon-status.json");
const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
if (!config.projectId || !config.projectSecret || !config.allowedUser || !config.assignedLine)
  throw new Error("Nova Photon configuration is incomplete");
const baseUrl = "http://127.0.0.1:5100";
const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath, "utf8")) : { sessions: {}, seen: [] };
state.sessions ||= {}; state.seen ||= [];
const seen = new Set(state.seen);
const writeState = () => { fs.writeFileSync(statePath + ".tmp", JSON.stringify({ sessions: state.sessions, seen: [...seen].slice(-500) })); fs.renameSync(statePath + ".tmp", statePath); };
const status = (connected, detail) => fs.writeFileSync(statusPath, JSON.stringify({ connected, detail, at: Date.now() }));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
let currentStatus = { connected: false, detail: "Connecting" };
const setStatus = (connected, detail) => { currentStatus = { connected, detail }; status(connected, detail); };
setInterval(() => status(currentStatus.connected, currentStatus.detail), 30000).unref();

const app = await Spectrum({ projectId: config.projectId, projectSecret: config.projectSecret,
  providers: [imessage.config()], options: { flattenGroups: true }, telemetry: false });
setStatus(false, "Connecting");
let retry = 1000;
for (;;) {
  try {
    setStatus(true, "Listening for messages");
    for await (const [space, message] of app.messages) {
      setStatus(true, "Connected"); retry = 1000;
      const content = inboundText(space, message, config.allowedUser);
      if (!content) continue;
      const id = String(message.id || "");
      if (!id || seen.has(id)) continue;
      seen.add(id); writeState(); // Mark before responding; replay cannot double-send.
      const key = String(space.id || config.allowedUser);
      try {
        const result = await novaReply(baseUrl, content, state.sessions[key]);
        if (result.sessionId) { state.sessions[key] = result.sessionId; writeState(); }
        await message.reply(result.text.slice(0, 3500));
      } catch (error) {
        // Do not send a generic error text: uncertain upstream delivery could duplicate a reply.
        setStatus(true, "Message needs attention in Nova Native");
        console.error("Nova Photon message failed:", error?.message || String(error));
      }
    }
    setStatus(false, "Reconnecting");
  } catch (error) {
    setStatus(false, "Reconnecting");
    console.error("Nova Photon stream failed:", error?.message || String(error));
  }
  await sleep(retry); retry = Math.min(retry * 2, 30000);
}
