"""Start MCP adapter directly from this checkout (backend runs separately)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'plugins/zju-study/src'))
from zju_study_plugin.server import main
if __name__ == '__main__':
    main()
