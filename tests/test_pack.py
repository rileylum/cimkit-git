import zipfile

from cimkit_git.pack import pack


def test_default_output_name(tmp_path, exploded):
    out = pack(str(exploded))
    assert out.name == "simple.aprx"


def test_custom_output_path(tmp_path, exploded):
    custom = tmp_path / "custom.aprx"
    out = pack(str(exploded), str(custom))
    assert out == custom
    assert out.exists()


def test_output_is_valid_zip(tmp_path, exploded):
    out = pack(str(exploded), str(tmp_path / "out.aprx"))
    assert zipfile.is_zipfile(out)


def test_file_count_matches(tmp_path, exploded, simple_aprx):
    with zipfile.ZipFile(simple_aprx) as zf:
        expected = len(zf.namelist())
    out = pack(str(exploded), str(tmp_path / "out.aprx"))
    with zipfile.ZipFile(out) as zf:
        assert len(zf.namelist()) == expected


def test_json_is_minified(tmp_path, exploded):
    out = pack(str(exploded), str(tmp_path / "out.aprx"))
    with zipfile.ZipFile(out) as zf:
        for name in zf.namelist():
            if name.endswith(".json"):
                data = zf.read(name).decode("utf-8")
                assert "\n" not in data, f"{name} is not minified"


def test_timestamps_are_dos_epoch(tmp_path, exploded):
    out = pack(str(exploded), str(tmp_path / "out.aprx"))
    with zipfile.ZipFile(out) as zf:
        for info in zf.infolist():
            assert info.date_time == (1980, 1, 1, 0, 0, 0), \
                f"{info.filename} has unexpected timestamp {info.date_time}"
