// ui_drive scenario for Simple SFTP Client (see build-tools/ui_drive/README.md
// for the helpers and how this is launched). Shared by window-fit-oversized and
// window-fit-offscreen. The fixture plants a saved window rectangle (larger
// than the screen, or mostly off the bottom right corner), then measures where
// the app's window ended up and writes the result file read here.
//
// Checks that the visible window rectangle lies fully inside the work area of
// its monitor. Oversized: it is no larger than the work area and its title bar
// is on screen. Offscreen: it kept its saved size and moved fully on screen.

import { readFileSync, existsSync } from 'node:fs';
import { join } from 'node:path';

export default async function windowFit(helpers) {
  const { waitFor, check, fixture } = helpers;
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
  } else {
    check("known case", false, String(res.case));
  }
}
