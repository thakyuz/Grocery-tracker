from grocery_deals.cli import main


def test_demo_command(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROCERY_OUT_DIR", str(tmp_path / "out"))
    main(["--db", str(tmp_path / "demo.db"), "demo"])
    out = capsys.readouterr().out
    assert "Your items on sale" in out
    assert list((tmp_path / "out").glob("digest-*.html"))
    main(["--db", str(tmp_path / "demo.db"), "top", "--limit", "5"])
    assert "of your top 5 items" in capsys.readouterr().out
