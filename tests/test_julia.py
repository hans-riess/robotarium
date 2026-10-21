"""Looking up a Julia package's [julia] entry in the General registry (a stand-in for it)."""

import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler

import pytest

from robotarium import julia

from conftest import write


@pytest.fixture
def registry(tmp_path, monkeypatch):
    write(tmp_path / "registry" / "J" / "JSON3" / "Package.toml",
          'name = "JSON3"\nuuid = "0f8b85d8-7281-11e9-16c2-39a750bddbf1"\n'
          'repo = "https://github.com/quinnj/JSON3.jl.git"\n')
    write(tmp_path / "registry" / "J" / "JSON3" / "Versions.toml",
          '["1.9.0"]\ngit-tree-sha1 = "a"\n\n["1.14.3"]\ngit-tree-sha1 = "b"\n\n'
          '["1.15.0"]\ngit-tree-sha1 = "c"\nyanked = true\n\n["1.2.0"]\ngit-tree-sha1 = "d"\n')

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *arguments):
            pass

    server = HTTPServer(("127.0.0.1", 0),
                        partial(Quiet, directory=str(tmp_path / "registry")))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ROBOTARIUM_JULIA_REGISTRY", "http://127.0.0.1:%d" % server.server_port)
    yield
    server.shutdown()
    server.server_close()


def test_the_latest_unyanked_version_is_found(registry):
    package = julia.lookup("JSON3")
    assert package.uuid == "0f8b85d8-7281-11e9-16c2-39a750bddbf1"
    assert package.version == "1.14.3"
    assert julia.toml_entry(package) == ('{ name = "JSON3", uuid = '
                                         '"0f8b85d8-7281-11e9-16c2-39a750bddbf1", '
                                         'version = "1.14.3" },')


def test_an_unknown_package_is_named(registry):
    with pytest.raises(julia.JuliaToolError, match="not in the General registry"):
        julia.lookup("NoSuchPackage")
