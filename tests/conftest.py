import compileall
import fnmatch
import io
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Callable, Dict, Iterable, Iterator, List, Optional
from unittest.mock import patch

import py.path
import pytest

# Config will be available from the public API in pytest >= 7.0.0:
# https://github.com/pytest-dev/pytest/commit/88d84a57916b592b070f4201dc84f0286d1f9fef
from _pytest.config import Config

# Parser will be available from the public API in pytest >= 7.0.0:
# https://github.com/pytest-dev/pytest/commit/538b5c24999e9ebb4fab43faabc8bcc28737bcdf
from _pytest.config.argparsing import Parser
from setuptools.wheel import Wheel

from pip._internal.cli.main import main as pip_entry_point
from pip._internal.locations import _USE_SYSCONFIG
from pip._internal.utils.temp_dir import global_tempdir_manager
from tests.lib import DATA_DIR, SRC_DIR, PipTestEnvironment, TestData
from tests.lib.path import Path
from tests.lib.server import MockServer as _MockServer
from tests.lib.server import make_mock_server, server_running
from tests.lib.venv import VirtualEnvironment, VirtualEnvironmentType

from .lib.compat import nullcontext

if TYPE_CHECKING:
    from typing import Protocol

    from wsgi import WSGIApplication
else:
    # TODO: Protocol was introduced in Python 3.8. Remove this branch when
    # dropping support for Python 3.7.
    Protocol = object


def pytest_addoption(parser: Parser) -> None:
    parser.addoption(
        "--keep-tmpdir",
        action="store_true",
        default=False,
        help="keep temporary test directories",
    )
    parser.addoption(
        "--resolver",
        action="store",
        default="2020-resolver",
        choices=["2020-resolver", "legacy"],
        help="use given resolver in tests",
    )
    parser.addoption(
        "--use-venv",
        action="store_true",
        default=False,
        help="use venv for virtual environment creation",
    )
    parser.addoption(
        "--run-search",
        action="store_true",
        default=False,
        help="run 'pip search' tests",
    )
    parser.addoption(
        "--proxy",
        action="store",
        default=None,
        help="use given proxy in session network tests",
    )


def pytest_collection_modifyitems(config: Config, items: List[pytest.Item]) -> None:
    for item in items:
        if not hasattr(item, "module"):  # e.g.: DoctestTextfile
            continue

        if item.get_closest_marker("search") and not config.getoption("--run-search"):
            item.add_marker(pytest.mark.skip("pip search test skipped"))

        if "CI" in os.environ:
            # Mark network tests as flaky
            if item.get_closest_marker("network") is not None:
                item.add_marker(pytest.mark.flaky(reruns=3, reruns_delay=2))

        if item.get_closest_marker("incompatible_with_test_venv") and config.getoption(
            "--use-venv"
        ):
            item.add_marker(pytest.mark.skip("Incompatible with test venv"))
        if (
            item.get_closest_marker("incompatible_with_venv")
            and sys.prefix != sys.base_prefix
        ):
            item.add_marker(pytest.mark.skip("Incompatible with venv"))

        if item.get_closest_marker("incompatible_with_sysconfig") and _USE_SYSCONFIG:
            item.add_marker(pytest.mark.skip("Incompatible with sysconfig"))

        # "Item" has no attribute "module"
        module_file = item.module.__file__  # type: ignore[attr-defined]
        module_path = os.path.relpath(
            module_file, os.path.commonprefix([__file__, module_file])
        )

        module_root_dir = module_path.split(os.pathsep)[0]
        if (
            module_root_dir.startswith("functional")
            or module_root_dir.startswith("integration")
            or module_root_dir.startswith("lib")
        ):
            item.add_marker(pytest.mark.integration)
        elif module_root_dir.startswith("unit"):
            item.add_marker(pytest.mark.unit)
        else:
            raise RuntimeError(f"Unknown test type (filename = {module_path})")


@pytest.fixture(scope="session", autouse=True)
def resolver_variant(request: pytest.FixtureRequest) -> Iterator[str]:
    """Set environment variable to make pip default to the correct resolver."""
    resolver = request.config.getoption("--resolver")

    # Handle the environment variables for this test.
    features = set(os.environ.get("PIP_USE_FEATURE", "").split())
    deprecated_features = set(os.environ.get("PIP_USE_DEPRECATED", "").split())

    if resolver == "legacy":
        deprecated_features.add("legacy-resolver")
    else:
        deprecated_features.discard("legacy-resolver")

    env = {
        "PIP_USE_FEATURE": " ".join(features),
        "PIP_USE_DEPRECATED": " ".join(deprecated_features),
    }
    with patch.dict(os.environ, env):
        yield resolver


@pytest.fixture(scope="session")
def tmpdir_factory(
    request: pytest.FixtureRequest, tmpdir_factory: pytest.TempdirFactory
) -> Iterator[pytest.TempdirFactory]:
    """Modified `tmpdir_factory` session fixture
    that will automatically cleanup after itself.
    """
    yield tmpdir_factory
    if not request.config.getoption("--keep-tmpdir"):
        shutil.rmtree(
            tmpdir_factory.getbasetemp(),
            ignore_errors=True,
        )


@pytest.fixture
def tmpdir(request: pytest.FixtureRequest, tmpdir: py.path.local) -> Iterator[Path]:
    """
    Return a temporary directory path object which is unique to each test
    function invocation, created as a sub directory of the base temporary
    directory. The returned object is a ``tests.lib.path.Path`` object.

    This uses the built-in tmpdir fixture from pytest itself but modified
    to return our typical path object instead of py.path.local as well as
    deleting the temporary directories at the end of each test case.
    """
    assert tmpdir.isdir()
    yield Path(str(tmpdir))
    # Clear out the temporary directory after the test has finished using it.
    # This should prevent us from needing a multiple gigabyte temporary
    # directory while running the tests.
    if not request.config.getoption("--keep-tmpdir"):
        tmpdir.remove(ignore_errors=True)


@pytest.fixture(autouse=True)
def isolate(tmpdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Isolate our tests so that things like global configuration files and the
    like do not affect our test results.

    We use an autouse function scoped fixture because we want to ensure that
    every test has it's own isolated home directory.
    """

    # TODO: Figure out how to isolate from *system* level configuration files
    #       as well as user level configuration files.

    # Create a directory to use as our home location.
    home_dir = os.path.join(str(tmpdir), "home")
    os.makedirs(home_dir)

    # Create a directory to use as a fake root
    fake_root = os.path.join(str(tmpdir), "fake-root")
    os.makedirs(fake_root)

    if sys.platform == "win32":
        # Note: this will only take effect in subprocesses...
        home_drive, home_path = os.path.splitdrive(home_dir)
        monkeypatch.setenv("USERPROFILE", home_dir)
        monkeypatch.setenv("HOMEDRIVE", home_drive)
        monkeypatch.setenv("HOMEPATH", home_path)
        for env_var, sub_path in (
            ("APPDATA", "AppData/Roaming"),
            ("LOCALAPPDATA", "AppData/Local"),
        ):
            path = os.path.join(home_dir, *sub_path.split("/"))
            monkeypatch.setenv(env_var, path)
            os.makedirs(path)
    else:
        # Set our home directory to our temporary directory, this should force
        # all of our relative configuration files to be read from here instead
        # of the user's actual $HOME directory.
        monkeypatch.setenv("HOME", home_dir)
        # Isolate ourselves from XDG directories
        monkeypatch.setenv(
            "XDG_DATA_HOME",
            os.path.join(
                home_dir,
                ".local",
                "share",
            ),
        )
        monkeypatch.setenv(
            "XDG_CONFIG_HOME",
            os.path.join(
                home_dir,
                ".config",
            ),
        )
        monkeypatch.setenv("XDG_CACHE_HOME", os.path.join(home_dir, ".cache"))
        monkeypatch.setenv(
            "XDG_RUNTIME_DIR",
            os.path.join(
                home_dir,
                ".runtime",
            ),
        )
        monkeypatch.setenv(
            "XDG_DATA_DIRS",
            os.pathsep.join(
                [
                    os.path.join(fake_root, "usr", "local", "share"),
                    os.path.join(fake_root, "usr", "share"),
                ]
            ),
        )
        monkeypatch.setenv(
            "XDG_CONFIG_DIRS",
            os.path.join(
                fake_root,
                "etc",
                "xdg",
            ),
        )

    # Configure git, because without an author name/email git will complain
    # and cause test failures.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "pip")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "distutils-sig@python.org")

    # We want to disable the version check from running in the tests
    monkeypatch.setenv("PIP_DISABLE_PIP_VERSION_CHECK", "true")

    # Make sure tests don't share a requirements tracker.
    monkeypatch.delenv("PIP_BUILD_TRACKER", False)

    # FIXME: Windows...
    os.makedirs(os.path.join(home_dir, ".config", "git"))
    with open(os.path.join(home_dir, ".config", "git", "config"), "wb") as fp:
        fp.write(b"[user]\n\tname = pip\n\temail = distutils-sig@python.org\n")


@pytest.fixture(autouse=True)
def scoped_global_tempdir_manager(request: pytest.FixtureRequest) -> Iterator[None]:
    """Make unit tests with globally-managed tempdirs easier

    Each test function gets its own individual scope for globally-managed
    temporary directories in the application.
    """
    if "no_auto_tempdir_manager" in request.keywords:
        ctx = nullcontext
    else:
        ctx = global_tempdir_manager

    with ctx():
        yield


@pytest.fixture(scope="session")
def pip_src(tmpdir_factory: pytest.TempdirFactory) -> Path:
    def not_code_files_and_folders(path: str, names: List[str]) -> Iterable[str]:
        # In the root directory...
        if path == SRC_DIR:
            # ignore all folders except "src"
            folders = {
                name for name in names if os.path.isdir(os.path.join(path, name))
            }
            to_ignore = folders - {"src"}
            # and ignore ".git" if present (which may be a file if in a linked
            # worktree).
            if ".git" in names:
                to_ignore.add(".git")
            return to_ignore

        # Ignore all compiled files and egg-info.
        ignored = set()
        for pattern in ("__pycache__", "*.pyc", "pip.egg-info"):
            ignored.update(fnmatch.filter(names, pattern))
        return ignored

    pip_src = Path(str(tmpdir_factory.mktemp("pip_src"))).joinpath("pip_src")
    # Copy over our source tree so that each use is self contained
    shutil.copytree(
        SRC_DIR,
        pip_src.resolve(),
        ignore=not_code_files_and_folders,
    )
    return pip_src


def _common_wheel_editable_install(
    tmpdir_factory: pytest.TempdirFactory, common_wheels: Path, package: str
) -> Path:
    wheel_candidates = list(common_wheels.glob(f"{package}-*.whl"))
    assert len(wheel_candidates) == 1, wheel_candidates
    install_dir = Path(str(tmpdir_factory.mktemp(package))) / "install"
    Wheel(wheel_candidates[0]).install_as_egg(install_dir)
    (install_dir / "EGG-INFO").rename(install_dir / f"{package}.egg-info")
    assert compileall.compile_dir(str(install_dir), quiet=1)
    return install_dir


@pytest.fixture(scope="session")
def setuptools_install(
    tmpdir_factory: pytest.TempdirFactory, common_wheels: Path
) -> Path:
    return _common_wheel_editable_install(tmpdir_factory, common_wheels, "setuptools")


@pytest.fixture(scope="session")
def wheel_install(tmpdir_factory: pytest.TempdirFactory, common_wheels: Path) -> Path:
    return _common_wheel_editable_install(tmpdir_factory, common_wheels, "wheel")


@pytest.fixture(scope="session")
def coverage_install(
    tmpdir_factory: pytest.TempdirFactory, common_wheels: Path
) -> Path:
    return _common_wheel_editable_install(tmpdir_factory, common_wheels, "coverage")


def install_egg_link(
    venv: VirtualEnvironment, project_name: str, egg_info_dir: Path
) -> None:
    with open(venv.site / "easy-install.pth", "a") as fp:
        fp.write(str(egg_info_dir.resolve()) + "\n")
    with open(venv.site / (project_name + ".egg-link"), "w") as fp:
        fp.write(str(egg_info_dir) + "\n.")


@pytest.fixture(scope="session")
def virtualenv_template(
    request: pytest.FixtureRequest,
    tmpdir_factory: pytest.TempdirFactory,
    pip_src: Path,
    setuptools_install: Path,
    coverage_install: Path,
) -> Iterator[VirtualEnvironment]:

    venv_type: VirtualEnvironmentType
    if request.config.getoption("--use-venv"):
        venv_type = "venv"
    else:
        venv_type = "virtualenv"

    # Create the virtual environment
    tmpdir = Path(str(tmpdir_factory.mktemp("virtualenv")))
    venv = VirtualEnvironment(tmpdir.joinpath("venv_orig"), venv_type=venv_type)

    # Install setuptools and pip.
    install_egg_link(venv, "setuptools", setuptools_install)
    pip_editable = Path(str(tmpdir_factory.mktemp("pip"))) / "pip"
    shutil.copytree(pip_src, pip_editable, symlinks=True)
    # noxfile.py is Python 3 only
    assert compileall.compile_dir(
        str(pip_editable),
        quiet=1,
        rx=re.compile("noxfile.py$"),
    )
    subprocess.check_call(
        [venv.bin / "python", "setup.py", "-q", "develop"], cwd=pip_editable
    )

    # Install coverage and pth file for executing it in any spawned processes
    # in this virtual environment.
    install_egg_link(venv, "coverage", coverage_install)
    # zz prefix ensures the file is after easy-install.pth.
    with open(venv.site / "zz-coverage-helper.pth", "a") as f:
        f.write("import coverage; coverage.process_startup()")

    # Drop (non-relocatable) launchers.
    for exe in os.listdir(venv.bin):
        if not (
            exe.startswith("python")
            or exe.startswith("libpy")  # Don't remove libpypy-c.so...
        ):
            (venv.bin / exe).unlink()

    # Enable user site packages.
    venv.user_site_packages = True

    # Rename original virtualenv directory to make sure
    # it's not reused by mistake from one of the copies.
    venv_template = tmpdir / "venv_template"
    venv.move(venv_template)
    yield venv


@pytest.fixture(scope="session")
def virtualenv_factory(
    virtualenv_template: VirtualEnvironment,
) -> Callable[[Path], VirtualEnvironment]:
    def factory(tmpdir: Path) -> VirtualEnvironment:
        return VirtualEnvironment(tmpdir, virtualenv_template)

    return factory


@pytest.fixture
def virtualenv(
    virtualenv_factory: Callable[[Path], VirtualEnvironment], tmpdir: Path
) -> Iterator[VirtualEnvironment]:
    """
    Return a virtual environment which is unique to each test function
    invocation created inside of a sub directory of the test function's
    temporary directory. The returned object is a
    ``tests.lib.venv.VirtualEnvironment`` object.
    """
    yield virtualenv_factory(tmpdir.joinpath("workspace", "venv"))


@pytest.fixture
def with_wheel(virtualenv: VirtualEnvironment, wheel_install: Path) -> None:
    install_egg_link(virtualenv, "wheel", wheel_install)


class ScriptFactory(Protocol):
    def __call__(
        self, tmpdir: Path, virtualenv: Optional[VirtualEnvironment] = None
    ) -> PipTestEnvironment:
        ...


@pytest.fixture(scope="session")
def script_factory(
    virtualenv_factory: Callable[[Path], VirtualEnvironment], deprecated_python: bool
) -> ScriptFactory:
    def factory(
        tmpdir: Path, virtualenv: Optional[VirtualEnvironment] = None
    ) -> PipTestEnvironment:
        if virtualenv is None:
            virtualenv = virtualenv_factory(tmpdir.joinpath("venv"))
        return PipTestEnvironment(
            # The base location for our test environment
            tmpdir,
            # Tell the Test Environment where our virtualenv is located
            virtualenv=virtualenv,
            # Do not ignore hidden files, they need to be checked as well
            ignore_hidden=False,
            # We are starting with an already empty directory
            start_clear=False,
            # We want to ensure no temporary files are left behind, so the
            # PipTestEnvironment needs to capture and assert against temp
            capture_temp=True,
            assert_no_temp=True,
            # Deprecated python versions produce an extra deprecation warning
            pip_expect_warning=deprecated_python,
        )

    return factory


@pytest.fixture
def script(
    tmpdir: Path,
    virtualenv: VirtualEnvironment,
    script_factory: Callable[[Path, Optional[VirtualEnvironment]], PipTestEnvironment],
) -> PipTestEnvironment:
    """
    Return a PipTestEnvironment which is unique to each test function and
    will execute all commands inside of the unique virtual environment for this
    test function. The returned object is a
    ``tests.lib.PipTestEnvironment``.
    """
    return script_factory(tmpdir.joinpath("workspace"), virtualenv)


@pytest.fixture(scope="session")
def common_wheels() -> Path:
    """Provide a directory with latest setuptools and wheel wheels"""
    return DATA_DIR.joinpath("common_wheels")


@pytest.fixture(scope="session")
def shared_data(tmpdir_factory: pytest.TempdirFactory) -> TestData:
    return TestData.copy(Path(str(tmpdir_factory.mktemp("data"))))


@pytest.fixture
def data(tmpdir: Path) -> TestData:
    return TestData.copy(tmpdir.joinpath("data"))


class InMemoryPipResult:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout


class InMemoryPip:
    def pip(self, *args: str) -> InMemoryPipResult:
        orig_stdout = sys.stdout
        stdout = io.StringIO()
        sys.stdout = stdout
        try:
            returncode = pip_entry_point(list(args))
        except SystemExit as e:
            returncode = e.code or 0
        finally:
            sys.stdout = orig_stdout
        return InMemoryPipResult(returncode, stdout.getvalue())


@pytest.fixture
def in_memory_pip() -> InMemoryPip:
    return InMemoryPip()


@pytest.fixture(scope="session")
def deprecated_python() -> bool:
    """Used to indicate whether pip deprecated this Python version"""
    return sys.version_info[:2] in []


CertFactory = Callable[[], str]


@pytest.fixture(scope="session")
def cert_factory(tmpdir_factory: pytest.TempdirFactory) -> CertFactory:
    # Delay the import requiring cryptography in order to make it possible
    # to deselect relevant tests on systems where cryptography cannot
    # be installed.
    from tests.lib.certs import make_tls_cert, serialize_cert, serialize_key

    def factory() -> str:
        """Returns path to cert/key file."""
        output_path = Path(str(tmpdir_factory.mktemp("certs"))) / "cert.pem"
        # Must be Text on PY2.
        cert, key = make_tls_cert("localhost")
        with open(str(output_path), "wb") as f:
            f.write(serialize_cert(cert))
            f.write(serialize_key(key))

        return str(output_path)

    return factory


class MockServer:
    def __init__(self, server: _MockServer) -> None:
        self._server = server
        self._running = False
        self.context = ExitStack()

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def host(self) -> str:
        return self._server.host

    def set_responses(self, responses: Iterable["WSGIApplication"]) -> None:
        assert not self._running, "responses cannot be set on running server"
        self._server.mock.side_effect = responses

    def start(self) -> None:
        assert not self._running, "running server cannot be started"
        self.context.enter_context(server_running(self._server))
        self.context.enter_context(self._set_running())

    @contextmanager
    def _set_running(self) -> Iterator[None]:
        self._running = True
        try:
            yield
        finally:
            self._running = False

    def stop(self) -> None:
        assert self._running, "idle server cannot be stopped"
        self.context.close()

    def get_requests(self) -> List[Dict[str, str]]:
        """Get environ for each received request."""
        assert not self._running, "cannot get mock from running server"
        # Legacy: replace call[0][0] with call.args[0]
        # when pip drops support for python3.7
        return [call[0][0] for call in self._server.mock.call_args_list]


@pytest.fixture
def mock_server() -> Iterator[MockServer]:
    server = make_mock_server()
    test_server = MockServer(server)
    with test_server.context:
        yield test_server


@pytest.fixture
def utc() -> Iterator[None]:
    # time.tzset() is not implemented on some platforms, e.g. Windows.
    tzset = getattr(time, "tzset", lambda: None)
    with patch.dict(os.environ, {"TZ": "UTC"}):
        tzset()
        yield
    tzset()


@pytest.fixture
def proxy(request: pytest.FixtureRequest) -> str:
    return request.config.getoption("proxy")
