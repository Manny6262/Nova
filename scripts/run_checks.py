"""Run isolated checks in separate processes; never start the live assistant."""
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
CASES=['native_qa_regressions.py','model_polish_regressions.py','natural_voice_regressions.py',
       'memory_projects.py','native_background_regressions.py','responsibilities_regressions.py',
       'fix_task_regressions.py','local_agent_milestone.py','test_public_defaults.py']
for name in CASES:
    print('Checking '+name,flush=True)
    subprocess.run([sys.executable,'-B',str(ROOT/'tests'/name)],cwd=ROOT,check=True)
print('All isolated checks passed.')
