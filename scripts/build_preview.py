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

BANNER = f'''<div class="sim-banner" role="note" aria-label="Simulator notice"><strong>SIMULATOR</strong><p>Browser-only preview. Services, failures and measurements are <b>generated in this tab</b> by a JavaScript copy of the lab's rules. No containers run, and the state resets when you reload. The full lab runs real Docker services on your machine.</p><div class="sim-actions"><button id="sim-skip" class="button" disabled title="Jump to 40 seconds before the timer expires">⏩ Skip to timer expiry</button><a class="button sim-download" href="{REPO}#run-the-full-lab-locally">⬇ Get the full lab</a></div></div>'''

HTML = [
    ('<title>AegisRack · Fleet investigation</title>', '<title>AegisRack · Browser simulator</title>'),
    ('<link rel="stylesheet" href="/static/style.css">', '<link rel="stylesheet" href="style.css"><link rel="stylesheet" href="preview.css"><script src="sim.js"></script>'),
    ('src="/static/view-model.js"', 'src="view-model.js"'),
    ('src="/static/app.js"', 'src="app.js"'),
    ('<body>', '<body>' + BANNER),
    ('href="/" aria-label="AegisRack home"', 'href="./" aria-label="AegisRack home"'),
    ('<span class="local-dot"></span> Local workspace <span class="version">EARLY BUILD 01</span>', '<span class="local-dot"></span> Browser simulator <span class="version">PREVIEW</span>'),
    ('Connecting to the local collector…', 'Starting the simulated collector…'),
    ('Two real services. One interruption.', 'Two simulated services. One interruption.'),
    ('Container lab · shared kernel', 'Simulated in your browser'),
    ('LINUX CONTAINER <span>', 'SIMULATED NODE <span>'),
    ('<span class="eyebrow">03 / MEASURED OBSERVATIONS</span>', '<span class="eyebrow">03 / SIMULATED OBSERVATIONS</span>'),
    ('Real checks will appear here when the collector runs.', 'Simulated checks will appear here when the collector runs.'),
    ('<span class="eyebrow">07 / PERSISTED LOCALLY</span>', '<span class="eyebrow">07 / THIS TAB ONLY</span>'),
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
    for name in ('style.css', 'view-model.js'):
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
