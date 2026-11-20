// ui_drive scenario for Simple SFTP Client (see build-tools/ui_drive/README.md
// for the helpers and how this is launched). Shared by the window-fit scenarios.
// The fixture plants a saved window rectangle, then measures where the app's
// window ended up and writes the result file read here.
//
// Checks that the visible window rectangle lies fully inside the work area of
// its monitor. Oversized: it is no larger than the work area and its title bar
// is on screen. Offscreen: it kept its saved size and moved fully on screen.

import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

export default async function windowFit(helpers) {
  const { waitFor, check, evaluate, fixture } = helpers;
  const fmt = (r) => `[${r.left},${r.top} - ${r.right},${r.bottom}]`;

  await waitFor("window.pywebview && window.pywebview.api && typeof window.pywebview.api.get_meta === 'function'", 10000);

  const file = join(fixture.out_dir, fixture.result_file);
  const deadline = Date.now() + 80000;
  while (!existsSync(file) && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 250));
  }
  check("fixture wrote the window measurement", existsSync(file), file);
  if (!existsSync(file)) return;
  const res = JSON.parse(readFileSync(file, 'utf8'));
  check("window was found and measured", !res.error, String(res.error));
  if (res.error) return;

  const { visible, work } = res;
  const detail = `visible ${fmt(visible)} work ${fmt(work)} frame ${fmt(res.frame)} planted ${JSON.stringify(res.planted)} dpi ${res.dpi}`;
  check("work area is at least the app's minimum window size", !res.work_too_small,
    `work ${fmt(work)} is smaller than min size ${res.min_size.width}x${res.min_size.height}; cannot judge fitting`);
  if (res.work_too_small) return;

  check("window lies fully inside the work area",
    visible.left >= work.left && visible.top >= work.top &&
    visible.right <= work.right && visible.bottom <= work.bottom, detail);

  if (res.case === 'oversized') {
    const vw = visible.right - visible.left;
    const vh = visible.bottom - visible.top;
    check("visible size is no larger than the work area",
      vw <= work.right - work.left && vh <= work.bottom - work.top,
      `visible ${vw}x${vh}, ${detail}`);
    check("title bar is on screen", visible.top >= work.top && visible.top < work.bottom, detail);
  } else if (res.case === 'offscreen') {
    const fw = res.frame.right - res.frame.left;
    const fh = res.frame.bottom - res.frame.top;
    check("window kept its saved size",
      Math.abs(fw - res.planted.width) <= 4 && Math.abs(fh - res.planted.height) <= 4,
      `frame ${fw}x${fh}, saved ${res.planted.width}x${res.planted.height}, ${detail}`);
  } else if (res.case === 'minimum') {
    const visibleHeight = visible.bottom - visible.top;
    const clientWidth100 = res.client.width * 96 / res.dpi;
    const clientHeight100 = res.client.height * 96 / res.dpi;
    const visibleHeight100 = visibleHeight * 96 / res.dpi;
    check("window is at its minimum size",
      Math.abs(clientWidth100 - 1000) <= 4 && Math.abs(clientHeight100 - 630) <= 4,
      `client ${res.client.width}x${res.client.height} physical px, ${clientWidth100.toFixed(1)}x${clientHeight100.toFixed(1)} at 100% scale, dpi ${res.dpi}`);
    check("minimum window fits a 1080p screen at 150%",
      visibleHeight100 <= 672,
      `visible height ${visibleHeight} physical px, ${visibleHeight100.toFixed(1)} at 100% scale, dpi ${res.dpi}, limit 672`);

    const footer = await evaluate(`(() => {
      const selectors = ['.dbg-toggle', '#updateBtn', '#qViewBtn', '#cancelBtn', '#pauseBtn'];
      const viewport = { width: window.innerWidth, height: window.innerHeight };
      const items = selectors.map((selector) => {
        const element = document.querySelector(selector);
        const rect = element?.getBoundingClientRect();
        const inside = Boolean(rect) && rect.width > 0 && rect.height > 0 &&
          rect.left >= 0 && rect.top >= 0 && rect.right <= viewport.width && rect.bottom <= viewport.height;
        return { selector, inside, rect: rect && { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height } };
      });
      return { pass: items.every((item) => item.inside), viewport, items };
    })()`);
    check("footer and queue bar are fully visible", footer.pass, JSON.stringify(footer));

    const middle = await evaluate(`(() => {
      const selectors = ['#upBtn', '#downBtn', '#compareBtn', '#syncUpBtn', '#syncDownBtn', '#watchBtn'];
      const viewport = { width: window.innerWidth, height: window.innerHeight };
      const panes = document.querySelector('.panes')?.getBoundingClientRect();
      const items = selectors.map((selector) => {
        const element = document.querySelector(selector);
        const rect = element?.getBoundingClientRect();
        const inside = Boolean(rect) && rect.width > 0 && rect.height > 0 &&
          rect.left >= 0 && rect.top >= 0 && rect.right <= viewport.width && rect.bottom <= viewport.height;
        const withinPanes = Boolean(rect && panes) && rect.top >= panes.top && rect.bottom <= panes.bottom;
        return { selector, inside, withinPanes, rect: rect && { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom, width: rect.width, height: rect.height } };
      });
      const overlaps = [];
      for (let index = 0; index < items.length; index += 1) {
        for (let other = index + 1; other < items.length; other += 1) {
          const a = items[index].rect;
          const b = items[other].rect;
          if (a && b && a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top) {
            overlaps.push([items[index].selector, items[other].selector]);
          }
        }
      }
      return { pass: Boolean(panes) && items.every((item) => item.inside && item.withinPanes) && overlaps.length === 0,
        viewport, panes: panes && { top: panes.top, bottom: panes.bottom }, items, overlaps };
    })()`);
    check("middle buttons fit between the file lists", middle.pass, JSON.stringify(middle));

    const logPanel = await evaluate(`(() => {
      const element = document.querySelector('#console');
      const style = element && getComputedStyle(element);
      const lineHeight = style && Number.parseFloat(style.lineHeight);
      const visibleTextHeight = element && style
        ? Math.max(0, element.clientHeight - Number.parseFloat(style.paddingTop) - Number.parseFloat(style.paddingBottom))
        : 0;
      return { pass: Number.isFinite(lineHeight) && visibleTextHeight >= lineHeight,
        visibleTextHeight, lineHeight, clientHeight: element?.clientHeight ?? 0 };
    })()`);
    check("log panel shows at least one line", logPanel.pass,
      `visible text height ${logPanel.visibleTextHeight}px, line height ${logPanel.lineHeight}px, client height ${logPanel.clientHeight}px`);
  } else {
    check("known case", false, String(res.case));
  }
}
