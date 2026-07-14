import os
from pathlib import Path

os.environ.setdefault("INNER_VIEW_TEST_ASSET_ROOT", str(Path(__file__).parent / "fixtures" / "runtime_assets"))
