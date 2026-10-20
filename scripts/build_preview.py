"""Build the static browser-simulator preview from the real web UI.

Copies web/ into site/, loads preview/sim.js ahead of the UI scripts and
relabels every claim of real measurement. Fails if any expected text is
missing, so UI changes cannot silently leave a "real" claim in the simulator.
Run from the project root: python3 scripts/build_preview.py
"""
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
REPO = 'https://github.com/choochootrain23/aegis-fault-lab'

INSTALL = REPO + '#run-the-full-lab-locally'
EXPLAIN = REPO + '#how-it-works'

BANNER = f'''<div class="sim-banner" role="note" aria-label="Simulator notice"><strong>SIMULATOR</strong><p>Browser-only simulation. Services, failures and measurements are <b>generated in this tab</b>; no containers run and nothing here is real. The full lab runs real Docker services on your machine.</p><div class="sim-actions"><button id="sim-skip" class="button" disabled title="Jump to 40 seconds before the timer expires">⏩ Skip to timer expiry</button><a class="button sim-explain" href="{EXPLAIN}">How it works ↗</a><a class="button sim-download" href="{INSTALL}">⬇ Install the full lab</a></div></div>'''

STRIP = f'''<div class="sim-strip" role="note"><span class="sim-tag">SIMULATION</span><span class="sim-strip-text">You are using a browser simulation, <b>not the real lab</b>. Every reading is generated.</span><a href="{INSTALL}">Install the real lab on GitHub ↗</a><a href="{EXPLAIN}">How it works ↗</a></div>'''

WELCOME = f'''<dialog id="sim-welcome" class="sim-welcome" aria-labelledby="sim-welcome-title"><div class="sim-welcome-head"><span class="sim-tag">SIMULATION</span><span class="eyebrow">Before you start</span></div><h2 id="sim-welcome-title">This is a <em>simulator</em>, not the real lab.</h2><p>AegisRack is a hands-on lab that stops real Docker services so you can practise finding and fixing the failure. This page runs the same interface against a <b>JavaScript simulation</b> in your browser:</p><ul><li><b>No containers run.</b> Services, failures and timings are generated in this tab.</li><li><b>Nothing is saved.</b> Reloading starts a fresh simulation.</li><li><b>The workflow is the same:</b> healthy baseline, injected fault, your investigation, verified recovery.</li></ul><p>To use the real thing, install the full lab from GitHub. It takes about 10 minutes on Linux or Windows (WSL2) with Docker.</p><div class="sim-welcome-actions"><a class="button primary" href="{INSTALL}">⬇ Install the full lab on GitHub</a><a class="button subtle" href="{EXPLAIN}">How it works ↗</a><button class="button sim-continue" id="sim-continue" autofocus>Explore the simulator →</button></div></dialog>'''

HTML = [
    ('<title>AegisRack · Fleet investigation</title>', '<title>SIMULATOR · AegisRack browser preview</title>'),
    ('<link rel="stylesheet" href="/static/style.css">', '<link rel="stylesheet" href="style.css"><link rel="stylesheet" href="preview.css"><script src="sim.js"></script>'),
    ('src="/static/view-model.js"', 'src="view-model.js"'),
    ('src="/static/app.js"', 'src="app.js"'),
    ('src="/static/topology.js"', 'src="topology.js"'),
    ('src="/static/report.js"', 'src="report.js"'),
    ('src="/static/tour.js"', 'src="tour.js"'),
    ('<body>', '<body>' + STRIP + BANNER + WELCOME),
    ('<p class="wordmark" aria-hidden="true">AEGISRACK</p>', '<p class="wordmark" aria-hidden="true">AEGISRACK</p><p class="sim-stamp">Simulator</p>'),
    ('href="/" aria-label="AegisRack home"', 'href="./" aria-label="AegisRack home"'),
    ('<a href="#history-section">History</a></nav>', f'<a href="#history-section">History</a><a class="nav-install" href="{INSTALL}">Install ↗</a></nav>'),
    ('One fault: stopped, starved of CPU, or out of disk.', 'One simulated fault: stopped, starved of CPU, or out of disk.'),
    ('<span class="local-dot"></span> Local workspace <span class="version">EARLY BUILD 01</span>', '<span class="local-dot sim-dot"></span> Simulated <span class="version">NOT REAL</span>'),
    ('Connecting to the local collector…', 'Starting the simulated collector…'),
    ('Two real services.', 'Two simulated services.'),
    ('Container lab · shared kernel', 'Simulated in your browser'),
    ('LINUX CONTAINER <span>', 'SIMULATED NODE <span>'),
    ('<span class="eyebrow">03 / MEASURED OBSERVATIONS</span>', '<span class="eyebrow">03 / SIMULATED OBSERVATIONS</span>'),
    ('Real checks will appear here when the collector runs.', 'Simulated checks will appear here when the collector runs.'),
    ('<span class="eyebrow">08 / PERSISTED LOCALLY</span>', '<span class="eyebrow">08 / THIS TAB ONLY</span>'),
    ('Real processes & measurements. No physical hardware claims.', 'Simulator preview · generated measurements. The downloadable lab uses real processes.'),
]
APP = [
    ('Investigation active. Checks reflect real service responses; choose your own path.',
     'Investigation active. Simulated checks follow the real lab\\\'s rules; choose your own path.'),
    ("'Fleet measurements unavailable. Start the project containers; unknown is not healthy.'",
     "'Simulated measurements unavailable. Reload the page; unknown is not healthy.'"),
]


def replace_all(text, pairs, name):
    for old, new in pairs:
        count = text.count(old)
        if count == 0:
            sys.exit(f'build_preview: expected text not found in {name}: {old[:70]!r}')
        text = text.replace(old, new)
    return text


def main():
    site = ROOT / 'site'
    shutil.rmtree(site, ignore_errors=True)
    site.mkdir()
    web = ROOT / 'web'
    for name in ('style.css', 'view-model.js', 'topology.js', 'report.js', 'tour.js'):
        shutil.copy2(web / name, site / name)
    shutil.copytree(web / 'fonts', site / 'fonts')
    for name in ('sim.js', 'preview.css'):
        shutil.copy2(ROOT / 'preview' / name, site / name)
    (site / 'index.html').write_text(replace_all((web / 'index.html').read_text(), HTML, 'index.html'))
    (site / 'app.js').write_text(replace_all((web / 'app.js').read_text(), APP, 'app.js'))
    (site / '.nojekyll').write_text('')
    leftover = [claim for claim in ('Real processes', 'real service responses', 'Local workspace')
                if claim in (site / 'index.html').read_text() + (site / 'app.js').read_text()]
    if leftover:
        sys.exit(f'build_preview: unlabelled real-measurement claims remain: {leftover}')
    print(f'Preview built in {site.relative_to(ROOT)}/')


if __name__ == '__main__':
    main()
