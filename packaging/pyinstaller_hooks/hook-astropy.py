"""Limit the portable build to the Astropy FITS surface used by SciDataFusion."""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("astropy")
datas += collect_data_files(
    "astropy",
    include_py_files=True,
    includes=["units/format/*_lextab.py", "units/format/*_parsetab.py"],
)


def _runtime_module(name: str) -> bool:
    return ".tests" not in name and not name.endswith(".tests")


hiddenimports = [
    module
    for package in (
        "astropy.constants",
        "astropy.io.fits",
        "astropy.table",
        "astropy.units",
    )
    for module in collect_submodules(package, filter=_runtime_module)
]
excludedimports = ["astropy.visualization", "matplotlib", "pytest", "hypothesis"]
