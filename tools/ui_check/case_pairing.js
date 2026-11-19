// ui_drive scenario "case-pairing". A local name and a server name that
// differ only in capitals are compared as one file: Compare colours the local
// rows, the Sync preview shows the local spelling, and Sync Up writes into
// the server's existing spelling (the fixture refuses the local spelling).

async function rowSelector(side, name, { evaluate }) {
  const tbody = side === "local" ? "tbodyLocal" : "tbodyRemote";
  const pos = await evaluate(
    `state.${side}.view.findIndex(ei=>state.${side}.entries[ei].name===${JSON.stringify(name)})`
  );
  if (pos < 0) throw new Error(`row not found in ${side} pane: ${name}`);
  return `#${tbody} tr:nth-child(${pos + 1})`;
}

export default async function casePairing(helpers) {
  const { click, type, press, evaluate, waitFor, check, screenshot, fixture } = helpers;
  const ok = (p) => p.then(() => true).catch(() => false);
  const rowClass = async (side, name) => evaluate(
    `document.querySelector(${JSON.stringify(await rowSelector(side, name, helpers))}).className`);

  check("fixture provided a server and local folder", !!fixture && !!fixture.port && !!fixture.local_dir,
    JSON.stringify(fixture));
  await waitFor("typeof API !== 'undefined' && API", 10000);
  await type("#host", "127.0.0.1");
  await type("#port", String(fixture.port));
  await type("#user", fixture.user);
  await type("#pass", fixture.password);
  await click("#connBtn");
  if (await ok(waitFor("$('confirmModal').classList.contains('show')", 10000))) await click("#cfOk");
  check("connect succeeds", await ok(waitFor("connected === true", 15000)));
  await click("#crumbsLocal");
  await type("#editLocal", fixture.local_dir);
  await press("Enter");
  check("local folder opens", await ok(waitFor("state.local.cwd && state.local.entries.length === 2", 10000)));
  await evaluate(`document.querySelector(${JSON.stringify(await rowSelector("remote", fixture.shared_folder, helpers))}).ondblclick()`);
  check("remote Shared folder opens", await ok(waitFor("state.remote.cwd === '/Shared' && state.remote.entries.length === 2", 10000)));

  await click("#compareBtn");
  check("Compare pairs report.txt with Report.txt", await ok(waitFor("$('localCompare').textContent.includes('1 newer here')", 20000)),
    await evaluate("$('localCompare').textContent + ' | ' + $('remoteCompare').textContent"));
  check("only the new files are one-sided",
    await evaluate("$('localCompare').textContent.includes('1 local-only') && $('remoteCompare').textContent.includes('1 remote-only')"),
    await evaluate("$('localCompare').textContent + ' | ' + $('remoteCompare').textContent"));
  const reportClass = await rowClass("local", "report.txt");
  check("local report.txt row coloured newer here", reportClass.includes("c-newer_local"), reportClass);
  const docsClass = await rowClass("local", "Docs");
  check("local Docs row coloured as changed", docsClass.includes("c-has_changes"), docsClass);
  await screenshot("case-pairing-compare");

  await click("#syncUpBtn");
  check("Sync preview opens with Transfer enabled", await ok(waitFor("$('syncModal').classList.contains('show') && !$('syOk').disabled", 20000)));
  const previewText = await evaluate("$('syBody').textContent");
  check("preview shows both spellings",
    previewText.includes("Report.txt") && previewText.includes("on this PC: report.txt")
      && previewText.includes("on this PC: Docs/new.txt"),
    previewText);
  await screenshot("case-pairing-preview");
  const startLight = await evaluate("document.body.classList.contains('light')");
  await evaluate("$('theme-btn').click()");
  await waitFor(`document.body.classList.contains('light') === ${!startLight}`, 5000).catch(() => {});
  await screenshot(startLight ? "case-pairing-preview-dark" : "case-pairing-preview-light");
  await evaluate("$('theme-btn').click()");
  await click("#syOk");

  const remoteNames = async (path) => evaluate(
    `API.list_remote(${JSON.stringify(path)}).then(r=>(r.entries||[]).map(e=>e.name+':'+e.size).sort())`);
  let shared = [];
  for (let waited = 0; waited < 20000; waited += 250) {
    shared = await remoteNames("/Shared");
    if (Array.isArray(shared) && shared.includes("Report.txt:18")) break;
    await new Promise((r) => setTimeout(r, 250));
  }
  await waitFor("queuePending === 0", 20000).catch(() => {});
  shared = await remoteNames("/Shared");
  check("Sync Up updated the server's Report.txt in place",
    Array.isArray(shared) && shared.length === 2 && shared.includes("Report.txt:18"),
    JSON.stringify(shared));
  const docs = await remoteNames("/Shared/docs");
  check("new.txt went into the server's docs folder",
    Array.isArray(docs) && docs.includes("new.txt:3") && docs.includes("other.txt:5"),
    JSON.stringify(docs));
}
