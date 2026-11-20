// ui_drive scenario "compare-omissions". It verifies Compare and Sync expose
// names that were intentionally not compared.

async function rowSelector(side, name, { evaluate }) {
  const tbody = side === "local" ? "tbodyLocal" : "tbodyRemote";
  const pos = await evaluate(
    `state.${side}.view.findIndex(ei=>state.${side}.entries[ei].name===${JSON.stringify(name)})`
  );
  if (pos < 0) throw new Error(`row not found in ${side} pane: ${name}`);
  return `#${tbody} tr:nth-child(${pos + 1})`;
}

export default async function compareOmissions(helpers) {
  const { click, type, press, evaluate, waitFor, check, screenshot, fixture } = helpers;
  const ok = (p) => p.then(() => true).catch(() => false);
  const popupOpen = "$('scanProblemsModal').classList.contains('show')";

  check("fixture provided a server and empty local folder", !!fixture && !!fixture.port && !!fixture.local_dir,
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
  check("local folder opens empty", await ok(waitFor("state.local.cwd && state.local.entries.length === 0", 10000)));
  await evaluate(`document.querySelector(${JSON.stringify(await rowSelector("remote", fixture.shared_folder, helpers))}).ondblclick()`);
  check("remote Shared folder opens", await ok(waitFor("state.remote.cwd === '/Shared'", 10000)));

  await click("#compareBtn");
  check("Compare shows three not compared", await ok(waitFor("$('remoteCompare').textContent.includes('3 not compared')", 20000)),
    await evaluate("$('remoteCompare').textContent"));
  await evaluate("$('remoteOmissions').click()");
  check("omission popup has three entries and Copy list", await ok(waitFor(`${popupOpen} && $('scanProblemsTitle').textContent === 'Entries not compared' && $('scanProblemsList').children.length === 3 && !$('scanProblemsCopy').disabled`, 5000)),
    await evaluate("$('scanProblemsSummary').textContent + ' | ' + $('scanProblemsList').textContent"));
  await screenshot("compare-omissions-list");
  const startLight = await evaluate("document.body.classList.contains('light')");
  await evaluate("$('theme-btn').click()");
  await waitFor(`document.body.classList.contains('light') === ${!startLight}`, 5000).catch(() => {});
  await screenshot(startLight ? "compare-omissions-list-dark" : "compare-omissions-list-light");
  await evaluate("$('theme-btn').click()");
  await click("#scanProblemsClose");

  await click("#syncDownBtn");
  check("Sync preview requires acknowledgement", await ok(waitFor("$('syncModal').classList.contains('show') && $('syOmissions').style.display !== 'none' && $('syOk').disabled", 20000)));
  await click("#syShowOmissions");
  check("Show list appears above Sync preview", await ok(waitFor(`${popupOpen} && $('syncModal').classList.contains('show')`, 5000)));
  await click("#scanProblemsClose");
  await click("#syAcknowledge");
  check("acknowledgement enables Transfer", await evaluate("$('syOk').disabled === false"));
  await click("#syOk");
  // queuePending is still 0 the moment Transfer is clicked, so wait for the
  // file itself, then for the queue to go idle before reading the folder.
  const listLocal = () => evaluate(`API.list_local(${JSON.stringify(fixture.local_dir)}).then(r=>(r.entries||[]).map(e=>e.name))`);
  let localNames = [];
  for (let waited = 0; waited < 20000; waited += 250) {
    localNames = await listLocal();
    if (Array.isArray(localNames) && localNames.includes("good.txt")) break;
    await new Promise((r) => setTimeout(r, 250));
  }
  await waitFor("queuePending === 0", 20000).catch(() => {});
  localNames = await listLocal();
  check("only good.txt transferred", Array.isArray(localNames) && localNames.includes("good.txt") && !localNames.includes("Report.txt") && !localNames.includes("report.txt"),
    JSON.stringify(localNames));

  await click("#syncDownBtn");
  check("no-transfer preview replaces Already in sync", await ok(waitFor("$('syncModal').classList.contains('show') && $('syOk').style.display === 'none' && $('syCancel').textContent === 'Close'", 20000)));
  check("console does not say Already in sync", !(await evaluate("$('console').textContent.includes('Already in sync')")));
  await screenshot("sync-omissions-no-transfer");
  await click("#syCancel");
}
