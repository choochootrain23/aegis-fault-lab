"""Read-only inspection of the running project's fleet limits and isolation."""
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from aegis.broker import validate_target


def docker(*args):
    return subprocess.check_output(['docker', *args], cwd=ROOT, text=True, timeout=15)


def main():
    report = {'at': datetime.now(timezone.utc).isoformat(), 'fleet': {}, 'passed': False}
    for node in ('gateway-a', 'catalog-b', 'gateway-c'):
        ids = docker('compose', 'ps', '-a', '-q', node).split()
        if len(ids) != 1:
            raise RuntimeError(f'{node}: expected exactly one project container')
        inspection = json.loads(docker('inspect', ids[0]))[0]
        validate_target(inspection, node)
        if not inspection['State']['Running']:
            raise RuntimeError(f'{node}: finish the investigation and restore baseline before verification')
        # Fixed reads only; never accept an operator-supplied command or path.
        code = """import json,os
from pathlib import Path
names=('memory.max','memory.swap.max','cpu.max','pids.max')
result={name:Path('/sys/fs/cgroup',name).read_text().strip() for name in names}
result['uid']=os.getuid()
result['security']={line.split(':')[0]:line.split(':')[1].strip() for line in Path('/proc/self/status').read_text().splitlines() if line.startswith(('CapEff:','NoNewPrivs:'))}
print(json.dumps(result))
"""
        limits = json.loads(docker('exec', ids[0], 'python', '-c', code))
        assert limits['memory.max'] == str(192*1024**2), (node, limits)
        assert limits['memory.swap.max'] == '0', (node, limits)
        assert limits['pids.max'] == '64', (node, limits)
        quota, period = map(int, limits['cpu.max'].split())
        assert quota/period == .25, (node, limits)
        assert limits['uid'] == 10001
        assert int(limits['security']['CapEff'], 16) == 0
        assert limits['security']['NoNewPrivs'] == '1'
        report['fleet'][node] = {'container_id': ids[0], 'effective': limits,
                                 'broker_inspection_contract': 'passed',
                                 'networks': list(inspection['NetworkSettings']['Networks'])}
    report['passed'] = True
    output = ROOT/'state'/'validation'/'runtime-isolation.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2)+'\n')
    print('Fleet inspection contract and effective cgroup/privilege checks passed:', output)


if __name__ == '__main__':
    main()
