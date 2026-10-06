import json
import xml.etree.ElementTree as ET

from cimkit_git.compare import compare
from cimkit_git.explode import explode
from cimkit_git.pack import pack


def test_explode_pack_is_semantically_identical(tmp_path, simple_aprx, exploded):
    packed = pack(str(exploded), str(tmp_path / "roundtrip.aprx"))
    assert compare(str(simple_aprx), str(packed)) is False


def test_double_explode_is_stable(tmp_path, simple_aprx, exploded):
    packed = pack(str(exploded), str(tmp_path / "roundtrip.aprx"))
    second = explode(str(packed), str(tmp_path / "second.aprx.src"))

    first_files = {
        f.relative_to(exploded).as_posix(): f.read_bytes()
        for f in exploded.rglob("*") if f.is_file()
    }
    second_files = {
        f.relative_to(second).as_posix(): f.read_bytes()
        for f in second.rglob("*") if f.is_file()
    }

    assert set(first_files) == set(second_files)

    def _strip(elem):
        if elem.text and not elem.text.strip():
            elem.text = None
        if elem.tail and not elem.tail.strip():
            elem.tail = None
        for child in elem:
            _strip(child)

    for name in first_files:
        if name.endswith(".json"):
            assert json.loads(first_files[name]) == json.loads(second_files[name]), \
                f"{name} differs after double explode"
        elif name.endswith(".xml"):
            ra = ET.fromstring(first_files[name].decode())
            rb = ET.fromstring(second_files[name].decode())
            _strip(ra)
            _strip(rb)
            assert ET.tostring(ra) == ET.tostring(rb), f"{name} differs after double explode"
        else:
            assert first_files[name] == second_files[name], f"{name} differs after double explode"
