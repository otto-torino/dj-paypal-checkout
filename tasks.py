import os
import webbrowser

from invoke import task


def open_browser(path):
    webbrowser.open("file://" + os.path.abspath(path))


@task
def clean_build(c):
    """
    Remove build artifacts
    """
    c.run("rm -fr build/")
    c.run("rm -fr dist/")
    c.run("rm -fr *.egg-info")


@task
def clean_pyc(c):
    """
    Remove python file artifacts
    """
    c.run("find . -name '*.pyc' -exec rm -f {} +")
    c.run("find . -name '*.pyo' -exec rm -f {} +")
    c.run("find . -name '*~' -exec rm -f {} +")


@task
def test(c):
    """
    Run the test suite
    """
    c.run("python tests/runtests.py")


@task
def coverage(c):
    """
    Check code coverage and open the HTML report
    """
    c.run("coverage run tests/runtests.py")
    c.run("coverage report -m")
    c.run("coverage html")
    c.run("xdg-open htmlcov/index.html")


@task
def docs(c):
    """
    Build the documentation and open it in the browser
    """
    c.run("sphinx-build -E -b html docs/source docs/build/html")
    open_browser(path="docs/build/html/index.html")


@task
def clean(c):
    """
    Remove python file and build artifacts
    """
    clean_build(c)
    clean_pyc(c)
