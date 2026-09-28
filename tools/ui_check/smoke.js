// ui_drive scenario for Simple SFTP Client (see build-tools/ui_drive/README.md
// for the helpers and how this is launched). Drives the real pywebview
// window against tools/ui_check/sftp_fixture.py's throwaway in-process SFTP
// server, over the app's real connect path: host, port, username and
// password are typed into the form, and the host-key prompt is accepted
// through the UI, the same way a person would.
//
// Covers: the page-to-Python bridge answering with the real version; a
// successful connect listing the server's files; uploading and downloading
// one file each way, confirmed through the page's own pane listings; a
// connect to a closed port failing with a plain-language message; a theme
// round trip with a screenshot of each theme; and a clean disconnect at the
// end. Does not cover sessions, key-based auth, sync/compare, or watch: see
// tools/ui_check/run_ui_check.py's scenario.js for those, driven headlessly
// against a stand-in bridge instead of the real window.

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
  const verLabel = await evaluate("document.getElementById('verLabel').textContent");
  check("version label shows a version", /^v\d+\.\d+\.\d+$/.test(verLabel), verLabel);
  const meta = await evaluate("API.get_meta()");
  check("bridge get_meta answers with the version shown on screen", "v" + meta.version === verLabel,
    JSON.stringify(meta));

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

  // f) disconnect cleanly at the end.
  await click("#connBtn");
  const disconnected = await waitFor("connected === false", 10000).then(() => true).catch(() => false);
  check("disconnects cleanly with no active session left behind", disconnected);
}
