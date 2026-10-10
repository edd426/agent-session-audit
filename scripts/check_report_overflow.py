#!/usr/bin/env python3
"""Render every page of a report in headless Chrome and find CLIPPED TEXT.

Why this exists
---------------
A reader of `orientation.html` found a label on one session reading
'opened a handoff note first — measus', with the rest cut off, and the same
clipping throughout the document.

Every one of those was an SVG `<text>` whose single long line ran past its
viewBox — SVG does not wrap, so the glyphs were simply painted outside the box
and clipped by the `<svg>` element's bounds. `test_orientation_profile.py`
guards the arithmetic version of this (a wrapped line, at the width it was
given, cannot be wider than its box). This script is the RENDERED version:
Chrome measures the real glyphs with the real fonts, so it also catches the
case the estimate gets wrong.

It needs a browser, so it is deliberately NOT part of the unit tests, which
use the standard library only, with nothing to install. Run it after any
change to a panel's layout.

How it works
------------
Chrome headless has no scripting API here, but `--dump-dom` prints the DOM
*after* load and after inline scripts have run. So the page is copied to a
scratch directory with one extra inline script appended, which measures
every `<text>` with `getBBox()` against its owning `<svg>`'s viewBox, and
every caption-ish HTML element with `scrollWidth` against `clientWidth`, then
writes the findings into a `<pre id="overflowreport">`. We dump the DOM and
read that element back out.

Usage
-----
    python3 check_report_overflow.py DIR [--widths 1400,1000] [--shots DIR]

Exit code 0 when nothing is clipped, 1 when something is.
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit_common                                              # noqa: E402

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

# The probe. Runs in the page, writes its findings into #overflowreport.
#
# TOLERANCE. getBBox() on a <text> reports the ink box, which for a font with
# overhanging glyphs can sit a fraction of a pixel outside a box that is
# visually fine. 1.5 user units is below one character at any size used here,
# so a real clip (which loses whole words) is never masked by it.
PROBE = r"""
<script>
(function(){
 var TOL=1.5, out=[];
 document.querySelectorAll('svg').forEach(function(sv,si){
  var vb=(sv.getAttribute('viewBox')||'').trim().split(/[\s,]+/).map(parseFloat);
  if(vb.length!==4||isNaN(vb[2]))return;
  var x0=vb[0],y0=vb[1],w=vb[2],h=vb[3];
  sv.querySelectorAll('text').forEach(function(t){
   var b; try{b=t.getBBox();}catch(e){return;}
   if(!b||(b.width===0&&b.height===0))return;
   var over=[];
   if(b.x<x0-TOL)over.push('left by '+(x0-b.x).toFixed(1));
   if(b.x+b.width>x0+w+TOL)over.push('right by '+(b.x+b.width-(x0+w)).toFixed(1));
   if(b.y<y0-TOL)over.push('top by '+(y0-b.y).toFixed(1));
   if(b.y+b.height>y0+h+TOL)over.push('bottom by '+(b.y+b.height-(y0+h)).toFixed(1));
   if(over.length)out.push({kind:'svg-text',svg:si,box:[w,h],
     over:over.join(', '),text:(t.textContent||'').slice(0,70)});
  });
 });
 // HTML captions and cells: a clip here is overflow:hidden or nowrap, not SVG.
 document.querySelectorAll('p,li,code,figcaption,.note,.sub,.label,.q,.st,td,th')
  .forEach(function(el){
   var s=getComputedStyle(el);
   if(s.overflow==='visible'&&s.overflowX==='visible'&&s.textOverflow!=='ellipsis')return;
   if(el.scrollWidth>el.clientWidth+1)
    out.push({kind:'html',tag:el.tagName.toLowerCase(),cls:el.className||'',
              over:(el.scrollWidth-el.clientWidth)+'px',
              text:(el.textContent||'').slice(0,70)});
  });
 var pre=document.createElement('pre');
 pre.id='overflowreport';
 pre.textContent=JSON.stringify(out);
 document.body.appendChild(pre);
})();
</script>
"""


def find_chrome() -> str:
    env = os.environ.get("CHROME_BIN")
    if env and Path(env).exists():
        return env
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    found = shutil.which("google-chrome") or shutil.which("chromium")
    if found:
        return found
    raise SystemExit("no Chrome/Chromium found; set CHROME_BIN")


def run_chrome(chrome: str, args, profile: Path, seconds: int = 90,
               sink: "Path | None" = None, done: "Path | None" = None) -> str:
    """Run headless Chrome and return what it printed.

    stdout goes to a FILE, not a pipe: headless Chrome often
    does not exit after --dump-dom, and reading a pipe after killing the
    process returns only what fitted in the OS buffer -- which truncated a
    290 KB appendix page at 64 KB and made the probe look like it had not run.
    """
    command = [chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
               "--no-first-run", "--no-default-browser-check",
               "--user-data-dir=%s" % profile, "--virtual-time-budget=4000"] + list(args)
    sink = sink or (profile.parent / "chrome-stdout.txt")
    audit_common.write_private(sink, "")     # the dumped DOM is report content: 0600
    with open(sink, "wb") as handle:
        process = subprocess.Popen(command, stdout=handle, stderr=subprocess.DEVNULL)
        deadline = time.time() + seconds
        while process.poll() is None and time.time() < deadline:
            # Chrome has finished the job the moment the probe's <pre> lands.
            try:
                if done is not None:
                    if done.exists() and done.stat().st_size > 0:
                        time.sleep(0.6)          # let the writer finish
                        break
                elif b"</pre>" in sink.read_bytes()[-4096:]:
                    break
            except OSError:
                pass
            time.sleep(0.4)
        if process.poll() is None:
            process.kill()
        process.wait()
    return sink.read_bytes().decode("utf-8", "replace")


def probe_page(chrome: str, source: Path, scratch: Path, width: int,
               profile: Path) -> list:
    body = source.read_text(encoding="utf-8")
    marker = "</body>"
    injected = (body.replace(marker, PROBE + marker) if marker in body else body + PROBE)
    target = scratch / source.name
    # The copy carries the report's own (private) content: owner-only, like it.
    audit_common.write_private(target, injected)
    dom = run_chrome(chrome, ["--dump-dom", "--window-size=%d,2400" % width,
                              target.resolve().as_uri()], profile)
    match = re.search(r'<pre id="overflowreport">(.*?)</pre>', dom, re.S)
    if not match:
        raise SystemExit("probe did not run on %s at %dpx (Chrome printed %d bytes)"
                         % (source.name, width, len(dom)))
    return json.loads(htmllib.unescape(match.group(1)) or "[]")


def screenshot(chrome: str, source: Path, out: Path, width: int, profile: Path) -> None:
    """A full-page screenshot. Chrome does not reliably exit after writing one,
    so the wait ends when the PNG appears rather than when the process does."""
    if out.exists():
        out.unlink()
    run_chrome(chrome, ["--screenshot=%s" % out, "--window-size=%d,2600" % width,
                        source.resolve().as_uri()], profile, seconds=45, done=out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("directory", help="a report directory written by `report`")
    ap.add_argument("--widths", default="1400,1000",
                    help="comma-separated viewport widths (default: 1400,1000)")
    ap.add_argument("--shots", help="also write a screenshot per page and width here")
    ap.add_argument("--scratch", help="where to put the instrumented copies "
                                      "(default: <directory>/.overflow-probe)")
    args = ap.parse_args(argv)

    directory = Path(args.directory).expanduser()
    pages = sorted(p for p in directory.glob("*.html"))
    if not pages:
        raise SystemExit("no .html pages in %s" % directory)
    chrome = find_chrome()
    scratch = Path(args.scratch).expanduser() if args.scratch else directory / ".overflow-probe"
    audit_common.make_private_dir(scratch)
    profile = scratch / "chrome-profile"
    audit_common.make_private_dir(profile)
    shots = Path(args.shots).expanduser() if args.shots else None
    if shots:
        audit_common.make_private_dir(shots)

    widths = [int(w) for w in args.widths.split(",") if w.strip()]
    failures = 0
    for width in widths:
        for source in pages:
            findings = probe_page(chrome, source, scratch, width, profile)
            state = "clipped" if findings else "clean"
            print("%-18s %5dpx  %s" % (source.name, width, state))
            for item in findings[:20]:
                failures += 1
                print("    %s  over %s  %r"
                      % (item.get("kind"), item.get("over"), item.get("text")))
            if shots:
                screenshot(chrome, source,
                           shots / ("%s-%dpx.png" % (source.stem, width)), width, profile)
    if failures:
        print("\n%d clipped element(s)." % failures)
        return 1
    print("\nno clipped text at %s px." % " / ".join(str(w) for w in widths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
