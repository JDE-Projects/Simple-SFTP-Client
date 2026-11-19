// ui_drive scenario "incomplete-scan" (tools/ui_check/ui_drive.json), fixture
// tools/ui_check/incomplete_scan_fixture.py. In the app's real window:
// download a server folder where one subfolder's listing is refused, and
// confirm the scan ends with one clickable "Scan incomplete" console line and
// one failed queue row, both opening the "Locations not read" list, while the
// readable file still downloads. Screenshots of the list in both themes are
// kept for review.

async function rowSelector(side, name, { evaluate }) {
  const tbody = side === "local" ? "tbodyLocal" : "tbodyRemote";
  const pos = await evaluate(
    `state.${side}.view.findIndex(ei=>state.${side}.entries[ei].name===${JSON.stringify(name)})`
  );
  if (pos < 0) throw new Error(`row not found in ${side} pane: ${name}`);
  return `#${tbody} tr:nth-child(${pos + 1})`;
}

export default async function incompleteScan(helpers) {
  const { click, type, press, evaluate, waitFor, check, screenshot, fixture } = helpers;
  const ok = (p) => p.then(() => true).catch(() => false);
  const modalOpen = "$('scanProblemsModal').classList.contains('show')";

  check("fixture provided a server and local folder", !!fixture && !!fixture.port && !!fixture.local_dir,
    JSON.stringify(fixture));
  await waitFor("typeof API !== 'undefined' && API", 10000);

  await type("#host", "127.0.0.1");
  await type("#port", String(fixture.port));
  await type("#user", fixture.user);
  await type("#pass", fixture.password);
  await click("#connBtn");
  if (await ok(waitFor("document.getElementById('confirmModal').classList.contains('show')", 10000))) {
    await click("#cfOk");
  }
  check("connect succeeds", await ok(waitFor("typeof connected !== 'undefined' && connected === true", 15000)));
  await waitFor("state.remote.cwd === '/'", 10000);

  await click("#crumbsLocal");
  await type("#editLocal", fixture.local_dir);
  await press("Enter");
  await waitFor(`state.local.cwd && state.local.cwd.toLowerCase() === ${JSON.stringify(fixture.local_dir.toLowerCase())}`, 10000)
    .catch(() => {});

  // a) download the folder with one refused subfolder.
  await click(await rowSelector("remote", fixture.scan_folder, helpers));
  await click("#downBtn");
  const lineSel = "[...document.querySelectorAll('#console .cline.scan-report')].find(l=>l.textContent.includes('Scan incomplete'))";
  const summarized = await ok(waitFor(`!!(${lineSel})`, 20000));
  check("console shows one clickable Scan incomplete line", summarized,
    await evaluate("$('console').textContent.slice(-400)"));
  check("console line counts one location not read",
    await evaluate(`(${lineSel}||{textContent:''}).textContent.includes('1 location(s) not read')`),
    await evaluate(`(${lineSel}||{textContent:''}).textContent`));
  check("no separate per-folder error line in the console",
    !(await evaluate(`[...document.querySelectorAll('#console .cline')].some(l=>l.textContent.includes('could not list'))`)));
  await waitFor("queuePending === 0", 20000).catch(() => {});
  const downloadedDir = fixture.local_dir + "\\" + fixture.scan_folder;
  const downloaded = await evaluate(
    `API.list_local(${JSON.stringify(downloadedDir)}).then(r=>!!r.ok&&(r.entries||[]).map(e=>e.name).join('|'))`);
  check("the readable file still downloaded", typeof downloaded === "string" && downloaded.split("|").includes("beach.jpg"),
    String(downloaded));
  check("footer counts the summary as failed",
    await evaluate("$('qMeta').textContent.includes('1 failed')"),
    await evaluate("$('qMeta').textContent"));
  await screenshot("console-scan-incomplete");

  // b) the console line opens the list.
  await evaluate(`(${lineSel}).click()`);
  const listed = await ok(waitFor(`${modalOpen} && $('scanProblemsList').textContent.includes(${JSON.stringify(fixture.refused_folder)})`, 5000));
  check("clicking the console line lists the refused folder", listed,
    await evaluate("$('scanProblemsSummary').textContent + ' | ' + $('scanProblemsList').textContent"));
  check("list summary names the download",
    await evaluate("$('scanProblemsSummary').textContent === '1 location(s) could not be read during this download.'"),
    await evaluate("$('scanProblemsSummary').textContent"));
  await screenshot("locations-list");

  // c) the same list in the other theme, then back.
  const startLight = await evaluate("document.body.classList.contains('light')");
  await evaluate("document.getElementById('theme-btn').click()");
  await waitFor(`document.body.classList.contains('light') === ${!startLight}`, 5000).catch(() => {});
  await screenshot(startLight ? "locations-list-dark" : "locations-list-light");
  await evaluate("document.getElementById('theme-btn').click()");
  await waitFor(`document.body.classList.contains('light') === ${startLight}`, 5000).catch(() => {});

  await press("Escape");
  check("Escape closes the list", await ok(waitFor(`!${modalOpen}`, 3000)));

  // d) the queue row opens the same list and offers no Retry.
  await click("#qViewBtn");
  const rowSel = "[...$('qList').querySelectorAll('.qrow')].find(r=>r.textContent.includes('Scan incomplete'))";
  check("queue shows a Scan incomplete row", await ok(waitFor(`!!(${rowSel})`, 5000)),
    await evaluate("$('qList').textContent"));
  check("summary row has Show list and no Retry",
    await evaluate(`(()=>{const r=${rowSel}; return !!r && [...r.querySelectorAll('button')].some(b=>b.textContent==='Show list') && !r.querySelector('.qretry');})()`));
  await screenshot("queue-summary-row");
  await evaluate(`[...(${rowSel}).querySelectorAll('button')].find(b=>b.textContent==='Show list').click()`);
  check("Show list opens the same list",
    await ok(waitFor(`${modalOpen} && $('scanProblemsList').textContent.includes(${JSON.stringify(fixture.refused_folder)})`, 5000)));
  await click("#scanProblemsClose");
  check("Close closes the list", await ok(waitFor(`!${modalOpen}`, 3000)));
  await click("#qCloseBtn");
}
