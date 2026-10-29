// Open Frappe desk pages in headless Chrome and fail on console errors.
//
// Usage (Node 22+, Google Chrome / Chromium installed):
//   FRAPPE_PASSWORD=... node browser_check.mjs --base http://dev.localhost:8000 \
//     --user Administrator --out results.json --shots shots/ /app/gate-pass /app/gate-pass/new ...
//   (routes can also come from a file: --routes-file routes.txt, one per line, e.g. the
//    output of `slice_tool.py routes`)
//
// For every route it records uncaught exceptions, console errors, failed HTTP requests and
// whether the page actually rendered (a form, list, report or page), and saves a screenshot.
// Exit code 1 when any page has an error, so it can gate a slice.

import { spawn } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync, existsSync, rmSync, mkdtempSync } from "node:fs";
import http from "node:http";
import https from "node:https";
import os from "node:os";
import path from "node:path";

const args = process.argv.slice(2);
const opt = (name, def) => {
	const i = args.indexOf(`--${name}`);
	if (i === -1) return def;
	const v = args[i + 1];
	args.splice(i, 2);
	return v;
};
const base = opt("base", "http://dev.localhost:8000").replace(/\/$/, "");
const user = opt("user", "Administrator");
const password = opt("password", process.env.FRAPPE_PASSWORD);
const out = opt("out", "browser-check.json");
const shots = opt("shots", "browser-shots");
const routesFile = opt("routes-file");
const waitMs = Number(opt("wait", "8000"));
const onPath = (bin) => (process.env.PATH || "").split(path.delimiter).some((dir) => existsSync(path.join(dir, bin)));
const chromeBin = opt("chrome", ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"].find(onPath) || "google-chrome");
const routes = [...args, ...(routesFile ? readFileSync(routesFile, "utf8").split("\n") : [])]
	.map((r) => r.trim())
	.filter((r) => r && !r.startsWith("#"));

if (!password) {
	console.error("Set FRAPPE_PASSWORD (or --password) for " + user);
	process.exit(2);
}
if (!routes.length) {
	console.error("No routes given");
	process.exit(2);
}

// noise that is not a fault of the app under test
const IGNORE = [/socket\.io/i, /favicon/i, /ERR_CONNECTION_REFUSED.*9000/i, /\/api\/method\/frappe\.realtime/i];
const ignored = (text) => IGNORE.some((re) => re.test(text || ""));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- log in (http module: *.localhost hosts are sent to 127.0.0.1 with the right Host header)
function login() {
	const url = new URL(base + "/api/method/login");
	const local = url.hostname.endsWith(".localhost");
	const body = JSON.stringify({ usr: user, pwd: password });
	const lib = url.protocol === "https:" ? https : http;
	return new Promise((resolve, reject) => {
		const req = lib.request(
			{
				host: local ? "127.0.0.1" : url.hostname,
				port: url.port || (url.protocol === "https:" ? 443 : 80),
				path: url.pathname,
				method: "POST",
				headers: { "Content-Type": "application/json", Host: url.host, "Content-Length": Buffer.byteLength(body) },
			},
			(res) => {
				let data = "";
				res.on("data", (c) => (data += c));
				res.on("end", () => {
					const sid = (res.headers["set-cookie"] || []).map((c) => c.match(/^sid=([^;]+)/)).find(Boolean);
					if (res.statusCode !== 200 || !sid || sid[1] === "Guest") reject(new Error(`login failed (${res.statusCode}): ${data.slice(0, 200)}`));
					else resolve(sid[1]);
				});
			}
		);
		req.on("error", reject);
		req.end(body);
	});
}

const sid = await login();
const profile = mkdtempSync(path.join(os.tmpdir(), "frappe-browser-check-"));
const port = 9300 + Math.floor(Math.random() * 500);
const chrome = spawn(chromeBin, ["--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
	"--no-first-run", "--no-default-browser-check", "--window-size=1400,1000", "about:blank"], { stdio: "ignore" });
const cleanup = () => {
	try { chrome.kill(); } catch {}
	try { rmSync(profile, { recursive: true, force: true }); } catch {}
};
process.on("exit", cleanup);

let version;
for (let i = 0; i < 60 && !version; i++) {
	try { version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json(); } catch { await sleep(250); }
}
if (!version) {
	console.error(`Could not start ${chromeBin} (use --chrome <path>)`);
	process.exit(2);
}
const ws = new WebSocket(version.webSocketDebuggerUrl);
await new Promise((r) => (ws.onopen = r));
let nextId = 0;
const pending = new Map();
const events = [];
ws.onmessage = (m) => {
	const d = JSON.parse(m.data);
	if (d.id && pending.has(d.id)) { pending.get(d.id)(d); pending.delete(d.id); } else events.push(d);
};
const send = (method, params = {}, sessionId) =>
	new Promise((r) => { const id = ++nextId; pending.set(id, r); ws.send(JSON.stringify({ id, method, params, sessionId })); });

const { result: { targetId } } = await send("Target.createTarget", { url: "about:blank" });
const { result: { sessionId } } = await send("Target.attachToTarget", { targetId, flatten: true });
for (const d of ["Runtime.enable", "Log.enable", "Network.enable", "Page.enable"]) await send(d, {}, sessionId);
await send("Network.setCookie", { name: "sid", value: sid, url: base + "/" }, sessionId);

// what "rendered" means per kind of route
const PROBE = `(() => {
	const r = frappe.get_route ? frappe.get_route() : [];
	const kind = r[0] || "";
	let ok = false, detail = "";
	if (kind === "Form") { ok = !!(window.cur_frm && cur_frm.doctype === r[1] && cur_frm.layout); detail = ok ? cur_frm.doctype + " / " + document.querySelectorAll(".frappe-control").length + " controls" : "form not rendered"; }
	else if (kind === "List" || kind === "Report" && r.length > 2) { ok = !!window.cur_list; detail = ok ? "list of " + cur_list.doctype : "list not rendered"; }
	else if (kind === "query-report") { ok = !!(frappe.query_report && frappe.query_report.report_name); detail = ok ? "report " + frappe.query_report.report_name : "report not rendered"; }
	else { ok = !!document.querySelector(".page-container.active, .layout-main-section"); detail = "page " + kind; }
	const msg = document.querySelector(".modal.show .msgprint");
	return { route: r.join("/"), ok, detail, dialog: msg ? msg.innerText.slice(0, 300) : "" };
})()`;

mkdirSync(shots, { recursive: true });
const results = [];
for (const [i, route] of routes.entries()) {
	events.length = 0;
	await send("Page.navigate", { url: base + (route.startsWith("/") ? route : "/" + route) }, sessionId);
	await sleep(waitMs);
	const errors = [];
	for (const e of events) {
		const p = e.params || {};
		if (e.method === "Runtime.exceptionThrown") {
			const d = p.exceptionDetails || {};
			const text = (d.exception && d.exception.description) || d.text || "exception";
			if (!ignored(text)) errors.push("exception: " + text.split("\n").slice(0, 3).join(" | "));
		} else if (e.method === "Runtime.consoleAPICalled" && p.type === "error") {
			const text = p.args.map((a) => a.value ?? a.description ?? "").join(" ");
			if (!ignored(text)) errors.push("console.error: " + text.slice(0, 300));
		} else if (e.method === "Log.entryAdded" && p.entry.level === "error") {
			if (!ignored(p.entry.text + " " + (p.entry.url || ""))) errors.push("log: " + p.entry.text.slice(0, 200) + " " + (p.entry.url || ""));
		} else if (e.method === "Network.responseReceived" && p.response.status >= 400) {
			if (!ignored(p.response.url)) errors.push(`http ${p.response.status}: ${p.response.url.slice(0, 200)}`);
		}
	}
	let probe = { ok: false, detail: "probe failed" };
	try {
		const r = await send("Runtime.evaluate", { expression: PROBE, returnByValue: true }, sessionId);
		probe = r.result.result.value || probe;
	} catch {}
	if (!probe.ok) errors.push("not rendered: " + probe.detail);
	if (probe.dialog) errors.push("error dialog: " + probe.dialog);
	const shot = path.join(shots, `${String(i + 1).padStart(2, "0")}-${route.replace(/[^a-z0-9]+/gi, "_").replace(/^_|_$/g, "")}.png`);
	try {
		const s = await send("Page.captureScreenshot", { format: "png" }, sessionId);
		writeFileSync(shot, Buffer.from(s.result.data, "base64"));
	} catch {}
	results.push({ route, ok: errors.length === 0, detail: probe.detail, errors, screenshot: shot });
	console.log(`${errors.length ? "FAIL" : "OK  "} ${route}  (${probe.detail})`);
	for (const err of errors) console.log("     " + err);
}
ws.close();
writeFileSync(out, JSON.stringify({ base, user, checked: new Date().toISOString(), screenshots: shots, results }, null, 1));
const failed = results.filter((r) => !r.ok).length;
console.log(`\n${results.length - failed}/${results.length} pages OK - results: ${out}`);
cleanup();
process.exit(failed ? 1 : 0);
