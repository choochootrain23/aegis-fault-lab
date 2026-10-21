"""Record a captioned 2–3 minute demo of the browser simulator as a WebM video.

Drives the static site/ build (no Docker, no services) through one practice investigation, network delay on
the shared catalogue, matching story 1 of docs/interview-walkthrough.md. Captions are added on top of the
page for the recording only. Output: ignored state/demo/aegis-demo.webm, plus aegis-demo-small.webm (about
7 MB, under GitHub's 10 MB limit for README videos) when an ffmpeg with VP9 is available, such as Playwright's.
Run from the project root with the Playwright environment: python scripts/record_demo.py
"""
import functools
import glob
import os
import http.server
from pathlib import Path
import shutil
import subprocess
import sys
import threading

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'state' / 'demo'
SIZE = {'width': 1280, 'height': 800}
CAPTION = """(text) => {
  let box = document.getElementById('demo-caption');
  if (!box) {
    box = document.createElement('div');
    box.id = 'demo-caption';
    Object.assign(box.style, {position: 'fixed', left: '50%', bottom: '28px', transform: 'translateX(-50%)', zIndex: 9999,
      maxWidth: '78%', padding: '14px 22px', background: 'rgba(16,16,20,.92)', color: '#fff', borderLeft: '6px solid #137333',
      font: '600 17px/1.4 Inter, system-ui, sans-serif', boxShadow: '6px 6px 0 rgba(16,16,20,.2)', transition: 'opacity .3s'});
    document.body.append(box);
  }
  box.style.opacity = text ? '1' : '0';
  if (text) box.textContent = text;
}"""


def compress(source):
    """Two-pass VP9 at 15 fps: the demo is mostly still UI, so this loses nothing visible."""
    browsers = os.path.abspath(os.getenv('PLAYWRIGHT_BROWSERS_PATH', str(Path.home() / '.cache' / 'ms-playwright')))
    ffmpeg = shutil.which('ffmpeg') or next(iter(sorted(glob.glob(os.path.join(browsers, 'ffmpeg-*', 'ffmpeg-linux')))), None)
    if not ffmpeg:
        print('No ffmpeg found; skipped the small copy')
        return
    small, scratch = source.with_name('aegis-demo-small.webm'), source.with_name('pass1.tmp.webm')
    common = ['-loglevel', 'error', '-y', '-i', str(source), '-r', '15', '-c:v', 'libvpx-vp9', '-b:v', '450k', '-row-mt', '1']
    subprocess.run([ffmpeg, *common, '-deadline', 'good', '-cpu-used', '4', '-pass', '1', '-f', 'webm', str(scratch)], check=True, cwd=source.parent)
    subprocess.run([ffmpeg, *common, '-deadline', 'good', '-cpu-used', '2', '-pass', '2', str(small)], check=True, cwd=source.parent)
    for leftover in [scratch, *source.parent.glob('ffmpeg2pass-*.log')]:
        leftover.unlink(missing_ok=True)
    print(f'Small copy: {small} ({small.stat().st_size / 2**20:.1f} MiB)')


def main():
    subprocess.run([sys.executable, str(ROOT / 'scripts' / 'build_preview.py')], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / 'site'))
    handler.log_message = lambda *args: None
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    raw = OUTPUT / 'raw'
    shutil.rmtree(raw, ignore_errors=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            context = browser.new_context(viewport=SIZE, record_video_dir=str(raw), record_video_size=SIZE)
            context.add_init_script("localStorage.setItem('aegis-tour-seen','1')")
            page = context.new_page()

            def say(text, hold=0):
                page.evaluate(CAPTION, text)
                page.wait_for_timeout(hold)

            def show(selector, hold=0):
                page.locator(selector).first.evaluate("e => e.scrollIntoView({behavior: 'smooth', block: 'center'})")
                page.wait_for_timeout(900 + hold)

            page.goto(f'http://127.0.0.1:{server.server_address[1]}/')
            expect(page.locator('#sim-welcome')).to_be_visible()
            say('AegisRack: find and fix faults in a small fleet of real services, from evidence alone', 5500)
            say('This is the browser simulator. The full lab runs the same interface against real Docker containers', 5500)
            page.click('#sim-continue')
            say('', 600)

            show('#journey')
            say('First the lab proves the fleet is healthy: five clean baseline cycles before any fault', 0)
            expect(page.locator('#start')).to_be_enabled(timeout=30000)
            page.wait_for_timeout(3500)
            show('.scenario-strip')
            say('Pick a fault and a service. Practice tells you the fault; challenge hides both', 4500)
            page.select_option('#practice-kind', 'network-delay')
            page.wait_for_timeout(1200)
            page.select_option('#practice-target', 'catalog-b')
            say('Here: a network delay on catalog-b, the catalogue both gateways depend on', 4500)
            page.click('#start')
            expect(page.locator('#session-status')).to_have_text('investigating', timeout=20000)

            show('#topology')
            say('Both gateways turn slow, but stay alive. Requests crawl across the live map', 8000)
            page.click('#node-gateway-a')
            page.click('[data-check="business"]')
            page.wait_for_timeout(1500)
            page.click('#node-gateway-c')
            page.click('[data-check="business"]')
            say('Business requests on gateway-a and gateway-c both take about 610 ms, double the 300 ms threshold', 5500)
            say('One slow gateway would be local. Both slow points at what they share: the catalogue', 5500)
            page.click('#node-catalog-b')
            page.click('[data-check="resources"]')
            say('CPU on catalog-b is nearly idle, so it is not CPU starvation', 5000)
            page.click('[data-check="network"]')
            say('The network path check reads the kernel queue: a 600 ms netem delay on eth0', 3500)
            show('#evidence-section')
            row = page.locator('.evidence-row').filter(has_text='netem queue').first
            row.locator('summary').click()
            page.wait_for_timeout(6000)

            show('#help-result')
            page.click('[data-help="action"]')
            say('Guidance is optional and cites the exact evidence behind it', 6000)
            page.locator('#ai-panel summary').click()
            show('#ai-panel')
            say('In the local lab, an optional AI investigator reads the same evidence. It never sees the fault and cannot act', 7000)
            page.locator('#ai-panel summary').click()

            show('#inspector-title')
            say('The fix: remove the traffic delay on catalog-b', 3500)
            page.click('[data-remedy="undelay"]')
            say('A successful action is not proof. Recovery counts only after three healthy cycles in a row', 0)
            expect(page.locator('#session-status')).to_have_text('recovered', timeout=25000)
            page.wait_for_timeout(5000)

            page.click('#reset')
            expect(page.locator('#session-status')).to_have_text('ended', timeout=20000)
            show('#report-section')
            say('The incident report: time to detect, actions that changed nothing, and a replayable latency chart', 2000)
            replay = page.locator('.replay-controls button').first
            if replay.count():
                replay.click()
            page.wait_for_timeout(10000)
            show('.hero')
            say('Five faults · three services · a broker that limits Docker access · github.com/choochootrain23/aegis-fault-lab', 6500)
            video = page.video
            context.close()
            browser.close()
            OUTPUT.mkdir(parents=True, exist_ok=True)
            final = OUTPUT / 'aegis-demo.webm'
            shutil.move(video.path(), final)
            shutil.rmtree(raw, ignore_errors=True)
            print('Demo recorded:', final)
            compress(final)
    finally:
        server.shutdown()


if __name__ == '__main__':
    main()
