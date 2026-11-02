"""Load real pure modules without executing the HA package entrypoint.

Runtime tests replace only HA/Supervisor boundaries. These are isolated unit
tests, not a substitute for real Core config-flow or rendered UI acceptance.
"""

import importlib
from pathlib import Path
import sys
import types

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "hikvision_intercom_chime"
PACKAGE = "chime_under_test"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package


def load(name):
    return importlib.import_module(PACKAGE + "." + name)
