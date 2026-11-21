// ui_drive scenario for Simple SFTP Client (see build-tools/ui_drive/README.md
// for the helpers and how this is launched). Drives the real pywebview
// window against tools/ui_check/fixture.py's throwaway in-process SFTP
// server, over the app's real connect path: host, port, username and
// password are typed into the form, and the host-key prompt is accepted
// through the UI, the same way a person would.
//
// Covers: the page-to-Python bridge answering with the real version; a
// successful connect listing the server's files; uploading and downloading
// one file each way, confirmed through the page's own pane listings; a
// server folder downloaded onto a symlinked local folder, with the console
// warning and the file landing in the link's target; a connect to a closed
// port failing with a plain-language message; a theme round trip with a
// screenshot of each theme; starting and stopping a watch, with the console's
// "Watch stopped." line after Stop and after a disconnect while watching; and
// a clean disconnect at the end. The debug log is switched on for the whole
// run, then read back: it must hold the fixed events for what happened and
// none of the typed values, paths, or file names. Does not cover sessions,
// key-based auth, sync/compare, or watch uploads: see
// tools/ui_check/run_ui_check.py's scenario.js for those, driven headlessly
// against a stand-in bridge instead of the real window.

import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

async function names(side, { evaluate }) {
  return evaluate(`state.${side}.entries.map(e=>e.name)`);
}

async function rowSelector(side, name, { evaluate }) {
  const tbody = side === "local" ? "tbodyLocal" : "tbodyRemote";
  const pos = await evaluate(
    `state.${side}.view.findIndex(ei=>state.${side}.entries[ei].name===${JSON.stringify(name)})`
  );
  if (pos < 0) throw new Error(`row not found in ${side} pane: ${name}`);
  return `#${tbody} tr:nth-child(${pos + 1})`;
}

async function waitForBatchDone(helpers) {
  await helpers.waitFor(`/^Done: \\d+ completed/.test($("qMeta").textContent)`, 20000);
}

export default async function smoke(helpers) {
  const { click, type, press, evaluate, waitFor, check, screenshot, fixture } = helpers;

  check("fixture provided a server and local folder", !!fixture && !!fixture.port && !!fixture.local_dir,
    JSON.stringify(fixture));

  // a) the page-to-Python link answers, with the real version.
  await waitFor("typeof API !== 'undefined' && API", 10000);
  await waitFor("document.getElementById('verLabel').textContent !== ''", 10000).catch(() => {});
  const verLabel = await evaluate("document.getElementById('verLabel').textContent");
  check("version label shows a version", /^v\d+\.\d+\.\d+$/.test(verLabel), verLabel);
  const meta = await evaluate("API.get_meta()");
  check("bridge get_meta answers with the version shown on screen", "v" + meta.version === verLabel,
    JSON.stringify(meta));

  // Debug log on for the whole run; read back at the end.
  await click(".dbg-track");
  const debugOn = await waitFor("$('dbgToggle').checked === true", 5000).then(() => true).catch(() => false);
  check("debug log switches on", debugOn);

  async function connectAndExpectSuccess(port, expectHostKeyPrompt) {
    await type("#host", "127.0.0.1");
    await type("#port", String(port));
    await type("#user", fixture.user);
    await type("#pass", fixture.password);
    await click("#connBtn");
    if (expectHostKeyPrompt) {
      const promptShown = await waitFor(
        "document.getElementById('confirmModal').classList.contains('show')", 10000
      );
      check("host-key prompt appears on first connect to a new host/port", promptShown);
      await click("#cfOk");
    }
    const connected = await waitFor("typeof connected !== 'undefined' && connected === true", 15000);
    check("connect succeeds", connected);
    await waitFor("state.remote.cwd === '/'", 10000);
  }

  // b) connect succeeds, remote pane lists the server's files.
  await connectAndExpectSuccess(fixture.port, true);
  const remoteAfterConnect = await names("remote", helpers);
  check(
    "remote pane lists the server's files after connecting",
    fixture.server_files.every((n) => remoteAfterConnect.includes(n)),
    JSON.stringify(remoteAfterConnect)
  );

  // c) core workflow: navigate local pane to the fixture folder, upload one
  // file, download one file, each confirmed through the page's own listing.
  await click("#crumbsLocal");
  await type("#editLocal", fixture.local_dir);
  await press("Enter");
  await waitFor(`state.local.entries.map(e=>e.name).includes(${JSON.stringify(fixture.local_files[0])})`, 10000);
  const localAfterNav = await names("local", helpers);
  check("local pane shows the fixture's files after navigating there",
    fixture.local_files.every((n) => localAfterNav.includes(n)), JSON.stringify(localAfterNav));

  const uploadName = fixture.local_files[0];
  await click(await rowSelector("local", uploadName, helpers));
  await click("#upBtn");
  await waitForBatchDone(helpers);
  const uploaded = await waitFor(
    `state.remote.entries.map(e=>e.name).includes(${JSON.stringify(uploadName)})`, 10000
  ).then(() => true).catch(() => false);
  check("uploaded file appears in the remote pane's listing", uploaded, JSON.stringify(await names("remote", helpers)));

  const downloadName = fixture.server_files[0];
  await click(await rowSelector("remote", downloadName, helpers));
  await click("#downBtn");
  await waitForBatchDone(helpers);
  const downloaded = await waitFor(
    `state.local.entries.map(e=>e.name).includes(${JSON.stringify(downloadName)})`, 10000
  ).then(() => true).catch(() => false);
  check("downloaded file appears in the local pane's listing", downloaded, JSON.stringify(await names("local", helpers)));

  // c2) downloading a server folder onto a local folder that is symlinked
  // (the fixture made it a junction) follows the link and warns in the
  // console where the files will really be written.
  await click(await rowSelector("remote", fixture.link_folder, helpers));
  await click("#downBtn");
  const clashShown = await waitFor("$('diffModal').classList.contains('show')", 10000)
    .then(() => true).catch(() => false);
  check("existing symlinked folder triggers the overwrite prompt", clashShown);
  if (clashShown) await click("#dfOk");
  const warning = `${fixture.link_folder} is symlinked to ${fixture.link_target}; files will be written there.`;
  const warned = await waitFor(`$("console").textContent.includes(${JSON.stringify(warning)})`, 10000)
    .then(() => true).catch(() => false);
  check("console warns that the download folder is symlinked", warned,
    await evaluate(`$("console").textContent.slice(-600)`));
  let landed = false;
  for (let i = 0; i < 100 && !landed; i++) {
    const r = await evaluate(`API.list_local(${JSON.stringify(fixture.link_target)})`);
    landed = !!(r && r.ok && r.entries.some((e) => e.name === fixture.link_file));
    if (!landed) await new Promise((res) => setTimeout(res, 100));
  }
  check("the folder's file is written into the symlink's target", landed);
  // list_local remembers the last folder it listed; point it back at the
  // folder the local pane is showing.
  await evaluate(`API.list_local(${JSON.stringify(fixture.local_dir)})`);

  // c3) a rename typed as a path that would leave the shown folder is
  // refused with a plain message, and nothing appears beside the folder.
  const renameStart = await evaluate(`$("console").textContent.length`);
  await evaluate(`renameItem("local", state.local.entries.find(e=>e.name===${JSON.stringify(uploadName)})), true`);
  await waitFor("$('promptModal').classList.contains('show')", 5000);
  await type("#pmInput", "..\\escaped.txt");
  await click("#pmOk");
  const refused = await waitFor(`$("console").textContent.slice(${renameStart}).includes("Rename failed:")`, 10000)
    .then(() => true).catch(() => false);
  const refusalText = await evaluate(`$("console").textContent.slice(${renameStart})`);
  check("a rename that would leave the shown folder is refused with a plain message",
    refused && refusalText.includes("can't be used here"), refusalText);
  const besideFolder = await evaluate(`API.list_local(${JSON.stringify(fixture.local_dir + "\\..")})`);
  const stillHere = await evaluate(`API.list_local(${JSON.stringify(fixture.local_dir)})`);
  check("the refused rename moved nothing out of the folder",
    !!(besideFolder && besideFolder.ok && !besideFolder.entries.some((e) => e.name === "escaped.txt")
      && stillHere && stillHere.ok && stillHere.entries.some((e) => e.name === uploadName)),
    JSON.stringify(besideFolder && besideFolder.entries.map((e) => e.name)));

  // d) error path: connect to a closed port shows a plain-language message.
  await click("#connBtn"); // currently connected: this disconnects
  await waitFor("connected === false", 10000);
  await type("#host", "127.0.0.1");
  await type("#port", String(fixture.closed_port));
  await type("#user", fixture.user);
  await type("#pass", fixture.password);
  await click("#connBtn");
  const failModalShown = await waitFor(
    "document.getElementById('confirmModal').classList.contains('show')", 15000
  );
  check("a connect to a closed port shows a message", failModalShown);
  const failText = await evaluate("document.getElementById('cfText').textContent");
  const looksRaw = /errno|winerror|0x[0-9a-f]{4,}/i.test(failText);
  check("the closed-port message reads as plain language, not a raw error code",
    failModalShown && !looksRaw && failText.trim().length > 0, failText);
  await click("#cfOk");
  check("still not connected after the failed attempt",
    await evaluate("connected === false"));

  // Reconnect for the remaining checks, which need an active session.
  await connectAndExpectSuccess(fixture.port, false);

  // e) theme round trip, screenshot of each.
  const startLight = await evaluate("document.body.classList.contains('light')");
  await click("#theme-btn");
  await waitFor(`document.body.classList.contains('light') === ${!startLight}`, 5000);
  const afterFirstToggle = await evaluate("document.body.classList.contains('light')");
  check("theme toggled to the other theme", afterFirstToggle === !startLight, String(afterFirstToggle));
  await screenshot(afterFirstToggle ? "theme-light" : "theme-dark");
  await click("#theme-btn");
  await waitFor(`document.body.classList.contains('light') === ${startLight}`, 5000);
  const afterSecondToggle = await evaluate("document.body.classList.contains('light')");
  check("theme toggled back to the starting theme", afterSecondToggle === startLight, String(afterSecondToggle));
  await screenshot(afterSecondToggle ? "theme-light" : "theme-dark");

  // e2) Watch through the real bridge: Stop resets the button and the
  // console then reports the watch has finished.
  async function startWatch() {
    await click("#watchBtn");
    await waitFor("document.getElementById('confirmModal').classList.contains('show')", 5000);
    await click("#cfOk");
    return waitFor("watching === true", 10000).then(() => true).catch(() => false);
  }
  check("watch starts", await startWatch());
  const stopStart = await evaluate(`$("console").textContent.length`);
  await click("#watchBtn");
  const buttonReset = await waitFor(`watching === false && $("watchBtn").textContent === "Watch"`, 1000)
    .then(() => true).catch(() => false);
  check("Stop resets the Watch button at once", buttonReset, await evaluate(`$("watchBtn").textContent`));
  const stopLine = await waitFor(`$("console").textContent.slice(${stopStart}).includes("Watch stopped.")`, 10000)
    .then(() => true).catch(() => false);
  check("console says the watch stopped after Stop", stopLine,
    await evaluate(`$("console").textContent.slice(${stopStart})`));
  check("watch starts again before disconnecting", await startWatch());

  // f) disconnect cleanly at the end, with the watch still on.
  const disconnectStart = await evaluate(`$("console").textContent.length`);
  await click("#connBtn");
  const disconnected = await waitFor("connected === false", 10000).then(() => true).catch(() => false);
  check("disconnects cleanly with no active session left behind", disconnected);
  await waitFor(`$("console").textContent.slice(${disconnectStart}).includes("Watch stopped.")`, 10000)
    .catch(() => {});
  const afterDisconnect = await evaluate(`$("console").textContent.slice(${disconnectStart})`);
  check("disconnect while watching shows one stop line and no connection-ended warning",
    afterDisconnect.split("Watch stopped.").length - 1 === 1
      && !afterDisconnect.includes("the connection it started on has ended"), afterDisconnect);

  // g) the debug log holds fixed events only, never typed values or names.
  await click(".dbg-track");
  await waitFor("$('dbgToggle').checked === false", 5000).catch(() => {});
  const logFiles = readdirSync(fixture.app_dir).filter((n) => /^Debug_Log_\d{8}_\d{6}(_\d+)?\.txt$/.test(n));
  check("a debug log file was written next to the app", logFiles.length > 0, JSON.stringify(logFiles));
  const logText = logFiles.map((n) => readFileSync(join(fixture.app_dir, n), "utf-8")).join("\n");
  for (const event of ["debug enabled", "connect\nauth=password", "] connected\n", "remote list\nitems=",
    "transfer\noutcome=ok", "symlink target", "connect failed", "disconnected"]) {
    check(`debug log records the fixed event: ${JSON.stringify(event)}`, logText.includes(event));
  }
  const forbidden = [fixture.password, "127.0.0.1", fixture.local_dir, fixture.link_target,
    "fixture_local", "fixture_server", "escaped.txt", fixture.link_folder, fixture.link_file,
    ...fixture.server_files, ...fixture.local_files];
  const leaked = forbidden.filter((v) => v && logText.includes(v));
  if (new RegExp(`\\b${fixture.user}\\b(?! connection)`).test(logText)) leaked.push(fixture.user);
  check("debug log holds no typed value, path, or file name", leaked.length === 0, JSON.stringify(leaked));
}
