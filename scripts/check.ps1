$ErrorActionPreference = "Stop"

function Invoke-UvChecked {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$UvArguments)

    & uv @UvArguments
    if ($LASTEXITCODE -ne 0) {
        throw "uv $($UvArguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

Invoke-UvChecked run ruff check .
Invoke-UvChecked run ruff format --check .
Invoke-UvChecked run mypy
Invoke-UvChecked run pytest
Invoke-UvChecked run coverage report --fail-under=90
Invoke-UvChecked run bandit -c pyproject.toml -r src
Invoke-UvChecked run python scripts/scan_secrets.py
Invoke-UvChecked run pip-audit --skip-editable
