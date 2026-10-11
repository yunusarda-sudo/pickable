"""Chromium visual verification for the ambient discovery-field animation.

Serves web/ over a local HTTP server (no backend needed; the animation is
backend-independent) and asserts, in a real Chromium at 1366x768 with the
mouse untouched:
  - all drift nodes move a clearly visible distance within the first 5s,
  - SVG line endpoints coincide with node centers on screen (~0px deviation
    despite non-uniform SVG scaling + parallax layers),
  - pulses advance continuously along their live edge (no mid-path jumps;
    the only large step is the invisible end-of-path wrap where opacity ~0),
  - no page errors occur,
  - the field is hidden on mobile widths and the animation stays off
    under prefers-reduced-motion.

Design/colors/hero/typography are not touched by this test.
"""
import functools
import http.server
import math
import threading
import unittest
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

SNAPSHOT_JS = """() => {
  const svg = document.querySelector('.field-links');
  if (!svg) return {ok:false};
  const circles = [...svg.querySelectorAll('circle')].map((el) => {
    const r = el.getBoundingClientRect();
    return {cls: el.getAttribute('class'), tr: el.getAttribute('transform'),
      screenX: r.left + r.width / 2, screenY: r.top + r.height / 2};
  });
  const path = svg.querySelector('path');
  const pts = [];
  const nums = path.getAttribute('d').match(/-?\\d+\\.?\\d*/g).map(Number);
  const pt = svg.createSVGPoint();
  const ctm = svg.getScreenCTM();
  for (let k = 0; k < nums.length; k += 2) {
    pt.x = nums[k]; pt.y = nums[k + 1];
    const s = pt.matrixTransform(ctm);
    pts.push([s.x, s.y]);
  }
  return {ok: true, circles, pts,
    drift: svg.querySelectorAll('.drift-node').length,
    pulses: svg.querySelectorAll('.pulse-dot').length};
}"""


def _serve(web_dir):
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(web_dir))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class AmbientAnimationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("playwright is not installed")
        cls._pw = sync_playwright().start()
        try:
            cls._browser = cls._pw.chromium.launch()
        except Exception as exc:
            cls._pw.stop()
            raise unittest.SkipTest(f"chromium unavailable: {exc}")
        cls._server = _serve(WEB_DIR)
        cls._base = f"http://127.0.0.1:{cls._server.server_port}/index.html"

    @classmethod
    def tearDownClass(cls):
        cls._server.shutdown()
        cls._browser.close()
        cls._pw.stop()

    def test_nodes_move_visibly_and_lines_stay_attached(self):
        page = self._browser.new_page(viewport={"width": 1366, "height": 768})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(self._base, wait_until="load")
            page.wait_for_timeout(800)
            before = page.evaluate(SNAPSHOT_JS)
            self.assertTrue(before["ok"])
            self.assertEqual(before["drift"], 10)
            self.assertEqual(before["pulses"], 2)
            # Mouse is never moved: parallax stays parked, drift is organic only.
            page.wait_for_timeout(5000)
            after = page.evaluate(SNAPSHOT_JS)
            nodes_b = [c for c in before["circles"] if "pulse" not in (c["cls"] or "")]
            nodes_a = [c for c in after["circles"] if "pulse" not in (c["cls"] or "")]
            self.assertEqual(len(nodes_b), 10)
            dists = [math.hypot(a["screenX"] - b["screenX"],
                                 a["screenY"] - b["screenY"])
                     for a, b in zip(nodes_a, nodes_b)]
            # Every node must have visibly moved within 5s (well above the
            # ~4.6px still-looks-static threshold from the earlier report).
            self.assertGreater(min(dists), 8,
                               f"a node barely moved in 5s: {[round(d,1) for d in dists]}")
            # Line endpoints vs node centers, measured in screen pixels so SVG
            # scaling / CSS transforms are included, not just model math.
            pairs = [(0, 1), (1, 2), (3, 4), (4, 5), (6, 7), (8, 9)]
            for e, (a, b) in enumerate(pairs):
                for (px, py), n in ((after["pts"][2 * e], nodes_a[a]),
                                    (after["pts"][2 * e + 1], nodes_a[b])):
                    dev = math.hypot(px - n["screenX"], py - n["screenY"])
                    self.assertLess(dev, 1.0, f"edge {e} detaches by {dev:.2f}px")
            # Pulses: consecutive 200ms samples must step smoothly; only the
            # invisible end-of-path wrap may jump.
            track = page.evaluate("""() => new Promise((res) => {
              const dots = [...document.querySelectorAll('.pulse-dot')];
              const seq = [];
              const iv = setInterval(() => {
                seq.push(dots.map((d) => [+d.getAttribute('cx'),
                  +d.getAttribute('cy'), +d.getAttribute('opacity')]));
                if (seq.length >= 20) { clearInterval(iv); res(seq); }
              }, 200);
            })""")
            for pi in range(len(track[0])):
                for k in range(len(track) - 1):
                    step = math.hypot(track[k + 1][pi][0] - track[k][pi][0],
                                      track[k + 1][pi][1] - track[k][pi][1])
                    if step > 25:
                        op = min(track[k][pi][2], track[k + 1][pi][2])
                        self.assertLess(
                            op, 0.15,
                            f"pulse {pi} visibly jumps {step:.1f} units at opacity {op}")
            self.assertEqual(errors, [])
        finally:
            page.close()

    def test_mobile_hides_field_and_reduced_motion_stays_still(self):
        mobile = self._browser.new_page(viewport={"width": 390, "height": 844})
        try:
            mobile.goto(self._base, wait_until="load")
            mobile.wait_for_timeout(600)
            self.assertEqual(mobile.evaluate(
                "() => getComputedStyle(document.querySelector"
                "('.discovery-field')).display"), "none")
            self.assertEqual(mobile.evaluate(
                "() => document.querySelectorAll('.drift-node').length"), 0)
        finally:
            mobile.close()
        still = self._browser.new_page(
            viewport={"width": 1366, "height": 768}, reduced_motion="reduce")
        try:
            still.goto(self._base, wait_until="load")
            still.wait_for_timeout(600)
            a = still.evaluate(SNAPSHOT_JS)
            still.wait_for_timeout(2000)
            b = still.evaluate(SNAPSHOT_JS)
            self.assertEqual(a["drift"], 0)
            for ca, cb in zip(a["circles"], b["circles"]):
                self.assertAlmostEqual(ca["screenX"], cb["screenX"], places=1)
                self.assertAlmostEqual(ca["screenY"], cb["screenY"], places=1)
        finally:
            still.close()


if __name__ == "__main__":
    unittest.main()
