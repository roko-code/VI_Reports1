"""Re-run Stage 4 (financial performance table) for one company.

Stage 4 is checkpointed, so a re-run resumes from the last completed model
call instead of redoing the work.  The checkpoint database and the run's
metadata live under the workspace's supporting data directory, so this must
be run with that directory supplied - invoking ``performance_table.py``
directly skips checkpointing.

    python run_stage4_fresh.py AMP 2026
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from performance_table import run_pipeline  # noqa: E402

ticker = sys.argv[1] if len(sys.argv) > 1 else "AMP"
year = sys.argv[2] if len(sys.argv) > 2 else "2026"

workspace = Path("data/companies") / f"{ticker.upper()}_{year}"
supporting = workspace / "supporting_data" / "stage4_performance_table"

success = run_pipeline(workspace, supporting_data_dir=supporting)
print(f"\nStage 4 result: {'SUCCESS' if success else 'FAILED'}")
