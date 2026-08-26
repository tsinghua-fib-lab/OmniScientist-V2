from omni.core.named_paths import iter_named_absolute_paths


def test_iter_named_absolute_paths_picks_source_dir(tmp_path) -> None:  # noqa: ANN001
    root = tmp_path / "sourcecode"
    root.mkdir()
    text = f"请仔细分析架构，对标codex 的源码（源码目录 {root} ）实现"
    found = iter_named_absolute_paths(text)
    assert found == [root.resolve()]


def test_iter_named_absolute_paths_skips_missing() -> None:
    assert iter_named_absolute_paths("see /definitely/not/a/real/omni/path/xyz") == []
