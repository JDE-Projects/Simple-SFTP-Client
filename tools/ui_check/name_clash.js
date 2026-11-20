// ui_drive scenario "name-clash" (tools/ui_check/ui_drive.json), fixture
// tools/ui_check/name_clash_fixture.py. In the app's real window: download a
// server folder whose listing holds Report.txt and report.txt, and confirm the
// clash reaches the console and the queue as a failed row with no Retry
// control; then download notes.txt into a folder holding NOTES.txt and confirm
// the overwrite prompt. Screenshots of each screen are kept for review.

async function rowSelector(side, name, { evaluate }) {
  const tbody = side === "local" ? "tbodyLocal" : "tbodyRemote";
  const pos = await evaluate(
    `state.${side}.view.findIndex(ei=>state.${side}.entries[ei].name===${JSON.stringify(name)})`
  );
  if (pos < 0) throw new Error(`row not found in ${side} pane: ${name}`);
  return `#${tbody} tr:nth-child(${pos + 1})`;
}

export default async function nameClash(helpers) {
  const { click, type, press, evaluate, waitFor, check, screenshot, fixture } = helpers;
  const ok = (p) => p.then(() => true).catch(() => false);

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
  await waitFor("state.local.entries.map(e=>e.name).includes('NOTES.txt')", 10000);

  // a) folder download with a clashing pair.
  await click(await rowSelector("remote", fixture.clash_folder, helpers));
  await click("#downBtn");
  const scanned = await ok(waitFor(
    "$('console').textContent.includes('1 name clash(es) not downloaded')", 20000));
  check("console reports the clash count", scanned,
    await evaluate("$('console').textContent.slice(-400)"));
  check("console tells the user how to get the skipped file",
    await evaluate("$('console').textContent.includes('download it on its own into a different local folder')"));
  await waitFor("queuePending === 0", 20000).catch(() => {});
  await waitFor("$('qMeta').textContent.startsWith('Done:')", 10000).catch(() => {});
  await screenshot("console-after-clash");

  await click("#qViewBtn");
  await waitFor("$('qList').textContent.includes('report.txt')", 5000).catch(() => {});
  check("queue shows the clash message",
    await evaluate("$('qList').textContent.includes('Same name on Windows as Report.txt')"),
    await evaluate("$('qList').textContent"));
  check("clash row has no retry control", await evaluate("!$('qList').querySelector('.qretry')"));
  check("Retry all failed is disabled", await evaluate("$('qRetryFailedBtn').disabled === true"));
  check("footer shows the Done line without repeating the queue summary",
    await evaluate("$('qMeta').textContent.startsWith('Done:') && $('qSummary').textContent === ''"),
    await evaluate("$('qMeta').textContent + ' | ' + $('qSummary').textContent"));
  await screenshot("queue-clash-row");
  await click("#qCloseBtn");

  // b) case-variant overwrite prompt.
  await click(await rowSelector("remote", "notes.txt", helpers));
  await click("#downBtn");
  const prompted = await ok(waitFor("$('diffModal').classList.contains('show')", 5000));
  check("downloading notes.txt beside NOTES.txt opens the overwrite prompt", prompted);
  check("prompt table shows the local file's size",
    await evaluate("$('dfBody').querySelectorAll('td.size')[0].textContent.trim() !== '-'"),
    await evaluate("$('dfBody').textContent"));
  await screenshot("overwrite-prompt");
  await click("#dfCancel");
}
