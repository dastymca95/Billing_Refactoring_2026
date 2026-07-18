import os
import sys
import types
from pathlib import Path

TEST_RUNTIME_ASSET_ROOT = Path(__file__).parent / "fixtures" / "runtime_assets"
os.environ.setdefault("INNER_VIEW_TEST_ASSET_ROOT", str(TEST_RUNTIME_ASSET_ROOT))
if os.environ.get("INNER_VIEW_CI") == "1":
    ci_policy = TEST_RUNTIME_ASSET_ROOT / "config" / "tenant_document_policies.yaml"
    if not ci_policy.is_file():
        raise RuntimeError("CI tenant document policy fixture is missing")


def _install_ci_only_processor_import_stubs() -> None:
    """Make registry-audit imports reproducible without private processor trees.

    The real processors remain deployment assets and this hook is active only
    when CI explicitly opts in. Stubs prove registry wiring is importable but
    deliberately raise if any test tries to execute production processing.
    """

    if os.environ.get("INNER_VIEW_CI") != "1":
        return
    entrypoints = {
        "process_richmond_utilities": "process_richmond_utilities_batch",
        "process_hopkinsville_water_environment_authority": (
            "process_hopkinsville_water_environment_authority_batch"
        ),
        "process_columbia_power_and_water_system": "process_columbia_power_and_water_system_batch",
        "process_atmos_energy_auto_pay": "process_atmos_energy_auto_pay_batch",
        "process_hardin_county_water_district_no_2": "process_hardin_county_water_district_no_2_batch",
        "process_shelbyville_power_system": "process_shelbyville_power_system_batch",
        "process_mcminnville_electric_system": "process_mcminnville_electric_system_batch",
        "process_pennyrile_electric": "process_pennyrile_electric_batch",
    }
    for module_name, entrypoint in entrypoints.items():
        module = types.ModuleType(module_name)

        def unavailable(*_args, _module_name: str = module_name, **_kwargs):
            raise RuntimeError(f"CI import-only processor stub cannot execute: {_module_name}")

        unavailable.__name__ = entrypoint
        setattr(module, entrypoint, unavailable)
        sys.modules[module_name] = module


def _use_ci_only_sanitized_vendor_configs() -> None:
    if os.environ.get("INNER_VIEW_CI") != "1":
        return
    from webapp.backend.services import deterministic_coverage, vendor_rules

    vendor_root = Path(__file__).parent / "fixtures" / "runtime_assets" / "config" / "vendors"
    deterministic_coverage.VENDORS_DIR = vendor_root
    vendor_rules.VENDORS_DIR = vendor_root
    vendor_rules.BACKUPS_DIR = vendor_root / "backups"
    deterministic_coverage.invalidate_inventory_cache()


_install_ci_only_processor_import_stubs()
_use_ci_only_sanitized_vendor_configs()
