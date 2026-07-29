"""Fail a release when its version and status documentation disagree."""

import ast
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def declared_package_version():
    tree = ast.parse((ROOT / "paypal_checkout" / "__init__.py").read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__version__"
                for target in node.targets
            )
        ):
            return ast.literal_eval(node.value)
    raise ValueError("paypal_checkout.__version__ is not declared")


def main():
    with (ROOT / "pyproject.toml").open("rb") as fh:
        project_version = tomllib.load(fh)["project"]["version"]

    errors = []
    package_version = declared_package_version()
    if package_version != project_version:
        errors.append(
            f"paypal_checkout.__version__ is {package_version!r}, "
            f"but pyproject.toml declares {project_version!r}"
        )

    if project_version != "0.0.0":
        index = (ROOT / "docs" / "source" / "index.rst").read_text()
        marker = f"**Version {project_version}.**"
        if marker not in index:
            errors.append(f"docs/source/index.rst does not contain {marker!r}")
        readme = (ROOT / "README.md").read_text()
        readme_marker = f"**Status: {project_version}."
        if readme_marker not in readme:
            errors.append(f"README.md does not contain {readme_marker!r}")
        if "In development — not released yet." in index:
            errors.append(
                "docs/source/index.rst still says the package has not been released"
            )

    if errors:
        print("Release documentation check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Release documentation matches version {project_version}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
