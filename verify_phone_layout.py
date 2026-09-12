"""
Check that no page scrolls sideways at phone widths.

Run it against a server you already have up, the way the other verify_*
scripts work:

    python verify_phone_layout.py                        # http://127.0.0.1:5000
    python verify_phone_layout.py http://127.0.0.1:5058
    python verify_phone_layout.py http://127.0.0.1:5058 --negative-control

It needs Chrome. It drives a throwaway profile under the system temp
directory, so it never contends with the retailer scrapers' persistent
profiles under ~/.chrome_profiles -- one Chrome per --user-data-dir, and a
profile already in use fails in a way that looks like a bot wall.

WHICH NUMBER TO TRUST
---------------------
`document.documentElement.scrollWidth` is NOT the measure of whether a page
scrolls sideways, and reading it that way has twice produced a bug report for
a layout that was already correct. On the dashboard at 400px it says 740 while
the viewport is 388 -- but the page cannot be scrolled sideways at all, because
the only oversized thing is the table, and the table is inside
`.table-responsive`, which scrolls it. Blink reports a descendant's layout
overflow through the root's scrollWidth even when an intervening
`overflow-x: auto` box contains it.

The two signals that do mean something, and that this script asserts:

  1. `document.body.scrollWidth <= document.documentElement.clientWidth`
     -- the page itself has nothing to scroll to.
  2. every element whose right edge passes the viewport has an ancestor that
     is a scroll container narrower than the viewport -- i.e. something is
     deliberately holding it, rather than it hanging off the edge.

Proof that (1) is the load-bearing one: give `.table-responsive`
`overflow-x: visible` and body.scrollWidth jumps from 388 to 797, while the
root's scrollWidth barely moves. That is what --negative-control does, and the
script is only trustworthy if it FAILS under it.

Widths are set through CDP rather than --window-size, because a Windows
display scale of 125% makes a 400px window report a 320px viewport, and every
measurement taken that way is silently 0.8x off.
"""
import sys
import tempfile
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.support.ui import WebDriverWait

# 320 is the narrowest phone still in use; 430 is a Pro Max. 600 is above the
# stacking breakpoint and is here to catch a "fix" that only works when narrow.
WIDTHS = (320, 360, 375, 390, 400, 414, 430, 600)

# Paths that exist on any instance. Group pages are discovered from the
# dashboard's own links, so this does not need the database.
PATHS = ('/', '/?view=grouped', '/add', '/settings', '/history', '/wiki')

BREAK_CSS = '.table-responsive { overflow-x: visible !important; }'

# The dashboard's auto-refresh fires one /api/products fetch the moment the
# page loads and rebuilds the table body from the response. Measuring while
# that is in flight catches a half-replaced table and fails at random, so wait
# for it to land first. Pages without a table have nothing to wait for.
SETTLE_JS = """
if (document.readyState !== 'complete') return false;
if (!document.querySelector('table')) return true;
return performance.getEntriesByType('resource')
  .some(e => e.name.indexOf('/api/products') !== -1 && e.responseEnd > 0);
"""

# Returns the page's own verdict, so the rules live in one place.
AUDIT_JS = """
const de = document.documentElement, vw = de.clientWidth;
const held = el => {
  let p = el.parentElement;
  while (p) {
    const ox = getComputedStyle(p).overflowX;
    if ((ox === 'auto' || ox === 'scroll' || ox === 'hidden') && p.clientWidth <= vw + 0.5) return true;
    p = p.parentElement;
  }
  return false;
};
const loose = [];
for (const el of document.querySelectorAll('*')) {
  const r = el.getBoundingClientRect();
  if ((r.width === 0 && r.height === 0) || r.right <= vw + 0.5) continue;
  if (held(el)) continue;
  loose.push(el.tagName.toLowerCase()
    + (el.className ? '.' + el.className.toString().trim().split(/\\s+/)[0] : '')
    + ' right=' + Math.round(r.right));
}
const stuck = [];
for (const w of document.querySelectorAll('.table-responsive')) {
  const need = Math.round(w.scrollWidth - w.clientWidth);
  if (need <= 0) continue;
  w.scrollLeft = 99999;
  const reached = Math.round(w.scrollLeft);
  w.scrollLeft = 0;
  // A wrapper that cannot reach its own right edge is not scrolling its table.
  if (Math.abs(reached - need) > 1) stuck.push('needs ' + need + ' reached ' + reached);
}
return {
  viewport: vw,
  bodyScrollWidth: document.body.scrollWidth,
  rootScrollWidth: de.scrollWidth,
  loose: loose.slice(0, 4),
  looseCount: loose.length,
  stuck: stuck,
  groupLinks: [...new Set([...document.querySelectorAll('a[href^="/group/"]')]
    .map(a => new URL(a.href).pathname))].slice(0, 2)
};
"""


def make_driver():
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-first-run')
    options.add_argument('--force-device-scale-factor=1')
    options.add_argument('--user-data-dir='
                         + tempfile.mkdtemp(prefix='verify_phone_layout_'))
    return webdriver.Chrome(options=options)


def set_width(driver, width):
    """Set an exact CSS viewport width, independent of display scaling."""
    driver.execute_cdp_cmd('Emulation.setDeviceMetricsOverride', {
        'width': width, 'height': 900, 'deviceScaleFactor': 1, 'mobile': False,
    })


def settle(driver):
    """Wait for the dashboard's load-time table rebuild, if there is one."""
    try:
        WebDriverWait(driver, 5).until(lambda d: d.execute_script(SETTLE_JS))
    except TimeoutException:
        # Not fatal: worst case this is the flake the wait exists to avoid, and
        # a failure has to reproduce on the recheck below before it is reported.
        pass


def audit_page(driver, base, path, width, negative_control):
    set_width(driver, width)
    driver.get(base + path)
    settle(driver)
    if negative_control:
        driver.execute_script(
            "const s=document.createElement('style');"
            "s.textContent=arguments[0];document.head.appendChild(s);",
            BREAK_CSS)
    result = driver.execute_script(AUDIT_JS)

    problems = []
    if result['bodyScrollWidth'] > result['viewport'] + 0.5:
        problems.append('body scrolls sideways (%d > %d)'
                        % (result['bodyScrollWidth'], result['viewport']))
    if result['looseCount']:
        problems.append('%d element(s) past the edge with nothing holding them: %s'
                        % (result['looseCount'], ', '.join(result['loose'])))
    if result['stuck']:
        problems.append('wrapper not scrolling its table: %s'
                        % '; '.join(result['stuck']))
    return result, problems


def main(argv):
    base = 'http://127.0.0.1:5000'
    negative_control = False
    for arg in argv:
        if arg == '--negative-control':
            negative_control = True
        elif arg.startswith('-'):
            sys.exit('unknown option %s' % arg)
        else:
            base = arg.rstrip('/')

    print('base=%s widths=%s%s'
          % (base, ','.join(str(w) for w in WIDTHS),
             ' NEGATIVE CONTROL (expected to fail)' if negative_control else ''))

    try:
        driver = make_driver()
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        print('could not start Chrome: %s: %s' % (type(exc).__name__, exc))
        return 3

    failures = []
    checked = 0
    try:
        paths = list(PATHS)
        for path in paths:
            for width in WIDTHS:
                result, problems = audit_page(driver, base, path, width,
                                              negative_control)
                checked += 1

                if problems:
                    # Load one more time before believing it. A real layout
                    # fault reproduces; a table caught mid-rebuild does not,
                    # and a check that cries wolf gets ignored.
                    time.sleep(0.3)
                    result, problems = audit_page(driver, base, path, width,
                                                  negative_control)

                label = '%-16s %4dpx  body=%-4d root=%-4d' % (
                    path, result['viewport'], result['bodyScrollWidth'],
                    result['rootScrollWidth'])
                if problems:
                    failures.append((path, width, problems))
                    print('  [FAIL] %s  %s' % (label, ' | '.join(problems)))
                else:
                    print('  [ok  ] %s' % label)

                # Pick up real group pages from whichever view links to them --
                # the flat table does not, the grouped one does.
                for link in result['groupLinks']:
                    if link not in paths:
                        paths.append(link)
    finally:
        driver.quit()

    print()
    if negative_control:
        # Here a failure is the pass condition: if breaking the wrapper does not
        # trip these assertions, the assertions are not measuring anything.
        if failures:
            print('negative control OK: breaking .table-responsive tripped '
                  '%d of %d checks' % (len(failures), checked))
            return 0
        print('NEGATIVE CONTROL DID NOT FAIL: %d checks all passed with the '
              'wrapper disabled, so these assertions prove nothing' % checked)
        return 1

    if failures:
        print('%d of %d checks failed' % (len(failures), checked))
        return 1
    print('all %d checks passed' % checked)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
