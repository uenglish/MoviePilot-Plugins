"""Run the shared behavioral suite and V2 host checks inside an isolated host."""
from pathlib import Path
import subprocess
import sys
import tempfile

from build_v2 import ROOT, IDS, MODULES, adapt_python

failed = False
with tempfile.TemporaryDirectory(prefix='pt-keepers-v2-tests-') as temporary:
    for pid in IDS:
        folder = pid.lower()
        target = Path(temporary) / folder
        target.mkdir()
        for source in (ROOT / 'tests/v3' / folder).glob('*.py'):
            text = adapt_python(source.read_text(), folder)
            # Existing mocks must patch the V2 import site, not a nonexistent
            # V3 SDK. Assertions and business fixtures are shared unchanged.
            text = text.replace('app.agent.llm.gateway.resolve_llm_provider_runtime',
                                MODULES['app.agent.llm.gateway'] + '.resolve_llm_provider_runtime')
            (target / source.name).write_text(text)
        result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                                 '-s', str(target), '-p', 'test_*.py'], cwd='/app')
        failed = failed or result.returncode != 0
    result = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover',
                             '-s', str(ROOT / 'tests/v2'), '-p', 'test_*.py'], cwd='/app')
    failed = failed or result.returncode != 0
raise SystemExit(int(failed))
