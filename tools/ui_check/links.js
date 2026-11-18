// ui_drive scenario "links" (tools/ui_check/ui_drive.json), fixture
// tools/ui_check/links_fixture.py. It checks that Compare and both Sync
// directions disclose omitted local junctions without showing them as plan
// rows, that the Sync Up dialog names them beside real transfers, and that a
// folder upload skips a nested junction and sends only the real file.

export default async function links(helpers) {
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
  if (await ok(waitFor("$('confirmModal').classList.contains('show')", 10000))) await click("#cfOk");
  check("connect succeeds", await ok(waitFor("connected === true", 15000)));

  await click("#crumbsLocal");
  await type("#editLocal", fixture.local_dir);
  await press("Enter");
  await waitFor("state.local.entries.map(e=>e.name).includes('real.txt')", 10000);
  await waitFor("state.remote.entries.map(e=>e.name).includes('j')", 10000);

  await click("#compareBtn");
  const compared = await ok(waitFor("$('localCompare').textContent.includes('2 link(s) left out')", 20000));
  check("Compare shows the omitted local link", compared, await evaluate("$('localCompare').textContent"));
  check("Compare title names the omitted local link", await evaluate("$('localCompare').title.includes('j')"),
    await evaluate("$('localCompare').title"));
  await screenshot("compare-links");

  async function syncAndCheck(button, screen) {
    const start = await evaluate("$('console').textContent.length");
    await click(button);
    const shown = await ok(waitFor(
      `$('syncModal').classList.contains('show') || $('console').textContent.slice(${start}).includes('local link(s) were left out')`,
      20000
    ));
    const text = await evaluate("$('syncModal').classList.contains('show') ? $('syText').textContent : $('console').textContent");
    check(`${screen} reports the omitted local link`, shown && text.includes('local link(s)') && text.includes('left out'), text);
    check(`${screen} has no plan row named j`, await evaluate(
      "!$('syncModal').classList.contains('show') || ![...$('syBody').querySelectorAll('tr')].some(r=>r.querySelector('td.name')?.childNodes[0]?.textContent.trim()==='j')"
    ));
    await screenshot(screen);
    if (await evaluate("$('syncModal').classList.contains('show')")) await click("#syCancel");
  }

  await syncAndCheck("#syncDownBtn", "sync-down-links");
  await syncAndCheck("#syncUpBtn", "sync-up-links");

  // Sync Up has real transfers, so its dialog must name the links in amber.
  const start = await evaluate("$('console').textContent.length");
  await click("#syncUpBtn");
  const dialog = await ok(waitFor("$('syncModal').classList.contains('show')", 20000));
  check("Sync Up shows the confirm dialog", dialog,
    await evaluate(`$('console').textContent.slice(${start})`));
  if (dialog) {
    const syText = await evaluate("$('syText').textContent");
    check("Sync Up dialog names both links", syText.includes("2 local link(s) left out, not synced in either direction")
      && syText.includes("j") && syText.includes("up/k"), syText);
    check("Sync Up dialog lists new.txt", await evaluate("$('syBody').textContent.includes('new.txt')"),
      await evaluate("$('syBody').textContent"));
    await screenshot("sync-up-dialog-links");
    await click("#syCancel");
  }

  // Folder upload: the nested junction is skipped and only inner.txt is sent.
  const upStart = await evaluate("$('console').textContent.length");
  const pos = await evaluate("state.local.view.findIndex(ei=>state.local.entries[ei].name==='up')");
  check("local pane shows folder up", pos >= 0);
  await click(`#tbodyLocal tr:nth-child(${pos + 1})`);
  await click("#upBtn");
  const uploaded = await ok(waitFor(
    `$('console').textContent.slice(${upStart}).includes('1 link(s) skipped')`, 20000));
  check("folder upload reports the skipped link", uploaded,
    await evaluate(`$('console').textContent.slice(${upStart})`));
  await waitFor("queuePending === 0", 20000).catch(() => {});
  const names = await evaluate("API.list_remote('/up').then(r=>r.ok?r.entries.map(e=>e.name).sort().join(','):'error: '+r.error)");
  check("server folder up holds only inner.txt", names === "inner.txt", names);
  await screenshot("upload-links");
}
