from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import load_config
from src.filters.relevance import RelevanceFilter
from src.models import (
    Category,
    DirectLink,
    GitHubMetadata,
    ScrapedItem,
    SourceType,
)
from src.notifiers.markdown_export import update_daily_markdown_report
from src.scrapers.forum_scraper import _extract_forum_content
from src.scrapers.web_dork_scraper import WebDorkScraper
from src.storage import Storage
from src.utils.link_extractor import extract_candidate_links, is_forum_url

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def _make_filter() -> RelevanceFilter:
    cfg = load_config(CONFIG_PATH)
    return RelevanceFilter(cfg)


def test_blacklist_rejects_malware_and_clickbait() -> None:
    rf = _make_filter()
    spam_items = [
        ScrapedItem(
            id="test:1",
            source=SourceType.GITHUB_REPO,
            title="GTA6-PC-Free-Crack-Keygen",
            url="https://github.com/scammer/gta6-crack",
            summary="GTA 6 free crack with password in description on mediafire",
        ),
        ScrapedItem(
            id="test:2",
            source=SourceType.REDDIT,
            title="[r/emulation] GTA 6 installer.exe survey keygen",
            url="https://reddit.com/r/emulation/comments/fake",
            summary="Complete survey to get mod menu",
        ),
    ]
    for item in spam_items:
        res = rf.evaluate(item)
        assert res.rejected is True
        assert res.relevance_score == 0
        assert res.rejection_reason and "Blacklist" in res.rejection_reason


def test_strict_gta6_mode_rejects_off_topic_and_non_technical() -> None:
    rf = _make_filter()
    now = datetime.now(timezone.utc)

    off_topic = ScrapedItem(
        id="test:offtopic",
        source=SourceType.GITHUB_REPO,
        title="someone/generic-ps4-emu",
        url="https://github.com/someone/generic-ps4-emu",
        summary="PS4 emulator written in C++ using Vulkan",
        updated_at=now,
        github_meta=GitHubMetadata(
            repo_full_name="someone/generic-ps4-emu",
            primary_language="C++",
            languages={"C++": 50000},
            root_files=["CMakeLists.txt", "src"],
            owner_login="someone",
            owner_created_at=now - timedelta(days=500),
            owner_public_repos=10,
            inspected=True,
        ),
    )
    res_off = rf.evaluate(off_topic)
    assert res_off.rejected is True
    assert res_off.rejection_reason and "Hors-cible" in res_off.rejection_reason

    hype_only = ScrapedItem(
        id="test:hype",
        source=SourceType.REDDIT,
        title="[r/GTA6] Trailer 2 hype discussion in Leonida!",
        url="https://reddit.com/r/GTA6/comments/hype1",
        summary="Can't wait to drive around Vice City on launch day!",
        published_at=now,
        score_engagement=500,
    )
    res_hype = rf.evaluate(hype_only)
    assert res_hype.rejected is True
    assert res_hype.rejection_reason and "sans contexte technique" in res_hype.rejection_reason


def test_github_legitimacy_rejects_new_account_and_readme_only() -> None:
    rf = _make_filter()
    now = datetime.now(timezone.utc)

    new_acct = ScrapedItem(
        id="test:new_acct",
        source=SourceType.GITHUB_REPO,
        title="fakeuser/GTA6Recomp",
        url="https://github.com/fakeuser/GTA6Recomp",
        summary="GTA 6 static recompilation",
        updated_at=now,
        github_meta=GitHubMetadata(
            repo_full_name="fakeuser/GTA6Recomp",
            primary_language="C++",
            languages={"C++": 12000},
            root_files=["README.md", "src"],
            owner_login="fakeuser",
            owner_created_at=now - timedelta(days=5),
            owner_public_repos=1,
            owner_followers=0,
            inspected=True,
        ),
    )
    assert rf.evaluate(new_acct).rejected is True

    exe_only = ScrapedItem(
        id="test:exe",
        source=SourceType.GITHUB_REPO,
        title="user/gta6-recomp-bin",
        url="https://github.com/user/gta6-recomp-bin",
        summary="GTA 6 recompilation",
        updated_at=now,
        github_meta=GitHubMetadata(
            repo_full_name="user/gta6-recomp-bin",
            primary_language="C++",
            languages={"C++": 2000},
            root_files=["README.md", "launcher.exe"],
            owner_login="user",
            owner_created_at=now - timedelta(days=400),
            owner_public_repos=10,
            inspected=True,
        ),
    )
    res_exe = rf.evaluate(exe_only)
    assert res_exe.rejected is True
    assert res_exe.rejection_reason and "binaires/archives" in res_exe.rejection_reason


def test_legitimate_recomp_archive_and_alive_direct_link_score_max() -> None:
    rf = _make_filter()
    now = datetime.now(timezone.utc)

    # Archive .7z sur forum technique avec symbols.toml vivant (HTTP 200) -> accepté + score maximal
    forum_item = ScrapedItem(
        id="test:forum_recomp",
        source=SourceType.FORUM,
        title="[Forum:gbatemp.net] GTA6Recomp — PS5 ELF/SPRX static recompilation & symbols.toml",
        url="https://gbatemp.net/threads/gta6recomp-ps5-elf-sprx.999999/",
        summary=(
            "Working static recompilation config for Grand Theft Auto VI (RAGE 9) "
            "using elf2cpp, DX12/Vulkan shaders and symbols.toml."
        ),
        updated_at=now - timedelta(hours=2),
        direct_links=[
            DirectLink(
                url="https://github.com/team/GTA6Recomp/releases/download/v0.1/symbols.toml",
                filename="symbols.toml",
                link_type="recomp_config",
                http_status=200,
                content_length=24576,
                is_alive=True,
            ),
            DirectLink(
                url="https://archive.org/download/gta6recomp-tools/gta6recomp_v0.1.7z",
                filename="gta6recomp_v0.1.7z",
                link_type="archive",
                http_status=200,
                content_length=45_000_000,
                is_alive=True,
            ),
        ],
    )
    res = rf.evaluate(forum_item)
    assert res.rejected is False
    assert res.category == Category.DECOMP
    assert res.score_breakdown["direct_links"] == 22
    assert res.relevance_score >= 90


def test_link_extractor_and_forum_parser() -> None:
    html = """
    <html>
      <head><title>GTA6Recomp Thread</title></head>
      <body>
        <h1>GTA6Recomp — RAGE 9 Static Recompilation</h1>
        <time datetime="2026-11-18T12:00:00Z">Yesterday</time>
        <div class="bbWrapper">
          Grab the config here:
          <a href="https://github.com/dev/GTA6Recomp/releases/download/v1/symbols.toml">symbols.toml</a>
          and mirror:
          <a href="https://archive.org/download/gta6recomp/build.7z">build.7z</a>
          and repo:
          <a href="https://codeberg.org/dev/GTA6Recomp">Codeberg Mirror</a>
        </div>
      </body>
    </html>
    """
    title, body, latest_dt = _extract_forum_content(html)
    assert "GTA6Recomp" in title
    assert "symbols.toml" in body
    assert latest_dt is not None

    links = extract_candidate_links(body, html=html, base_url="https://gbatemp.net/threads/1/")
    filenames = {dl.filename for dl in links}
    assert "symbols.toml" in filenames
    assert "build.7z" in filenames
    assert is_forum_url("https://gtaforums.com/topic/12345/") is True
    assert is_forum_url("https://github.com/user/repo") is False


def test_web_dorks_generation() -> None:
    cfg = load_config(CONFIG_PATH)
    import httpx

    with httpx.Client():
        scraper = WebDorkScraper(cfg, httpx.AsyncClient())
        dorks = scraper.build_dorks()
        assert any("site:gtaforums.com" in d for d in dorks)
        assert any("site:archive.org" in d for d in dorks)
        assert any("filetype:toml" in d for d in dorks)


def test_storage_direct_links_and_daily_markdown(tmp_path: Path) -> None:
    db_file = tmp_path / "tracker.db"
    storage = Storage(db_file)
    rf = _make_filter()
    now = datetime.now(timezone.utc)

    item = rf.evaluate(
        ScrapedItem(
            id="test:decomp_dl",
            source=SourceType.GITLAB,
            title="[GitLab] dev/GTA6Recomp",
            url="https://gitlab.com/dev/GTA6Recomp",
            summary="GTA 6 static recompilation using elf2cpp and RAGE9 symbols.toml.",
            published_at=now,
            direct_links=[
                DirectLink(
                    url="https://gitlab.com/dev/GTA6Recomp/-/raw/main/symbols.toml",
                    filename="symbols.toml",
                    link_type="recomp_config",
                    http_status=200,
                    content_length=16384,
                    is_alive=True,
                )
            ],
        )
    )
    assert not item.rejected

    first_save = storage.save_new_items([item])
    assert len(first_save) == 1
    assert len(storage.save_new_items([item])) == 0

    top = storage.get_top_items(limit=10)
    assert len(top) == 1
    assert len(top[0].direct_links) == 1
    assert top[0].direct_links[0].filename == "symbols.toml"
    assert top[0].direct_links[0].is_alive is True

    dl_rows = storage.get_direct_links(limit=20, alive_only=True)
    assert len(dl_rows) == 1

    daily_md = update_daily_markdown_report(
        top,
        tmp_path / "reports",
        direct_links_table=dl_rows,
        total_scraped=10,
        total_rejected=9,
        total_stored=1,
    )
    assert daily_md.exists()
    assert daily_md.name == f"{now.strftime('%Y-%m-%d')}.md"
    content = daily_md.read_text(encoding="utf-8")
    assert "## Fichiers & Liens directs détectés" in content
    assert "[symbols.toml](https://gitlab.com/dev/GTA6Recomp/-/raw/main/symbols.toml)" in content
    assert "16.0 KB" in content


def test_simple_status_inference_from_page_and_quarantine_reader(tmp_path: Path) -> None:
    import zipfile

    from src.sandbox.vm_downloader import (
        delete_quarantine_file,
        generate_inspection_sandbox_wsb,
        list_quarantine_files,
        read_quarantine_file_safely,
    )
    from src.utils.status_analyzer import infer_simple_status

    now = datetime.now(timezone.utc)

    # 1. Un outil RPF en WIP ne doit PAS être marqué Playable même si le README mentionne 60fps
    rpf_item = ScrapedItem(
        id="test:rpf:1",
        source=SourceType.GITHUB_REPO,
        title="dev/RPF9-Explorer",
        url="https://github.com/dev/RPF9-Explorer",
        summary="RPF9 archive extractor for GTA 6",
        published_at=now,
    )
    assert (
        infer_simple_status(
            rpf_item,
            "# RPF9 Explorer\n## Status\nWork in progress. Not playable (asset tool only).",
        )
        == "Outil RPF (WIP)"
    )

    # 2. Un émulateur qui tourne réellement à 60 fps en jeu -> "Playable 60 FPS"
    emu_item = ScrapedItem(
        id="test:emu:60",
        source=SourceType.GITHUB_REPO,
        title="dev/GTA6-Recomp",
        url="https://github.com/dev/GTA6-Recomp",
        summary="GTA 6 static recompilation",
        published_at=now,
    )
    assert (
        infer_simple_status(emu_item, "## Status\nGame is now playable and running at 60 fps!")
        == "Playable 60 FPS"
    )

    # 3. Persistance SQLite + export Markdown
    storage = Storage(tmp_path / "status_test.db")
    rpf_item.relevance_score = 75
    rpf_item.playability_status = "Outil RPF (WIP)"
    storage.save_new_items([rpf_item])
    loaded = storage.get_item_by_id(rpf_item.id)
    assert loaded is not None and loaded.playability_status == "Outil RPF (WIP)"
    md = update_daily_markdown_report([loaded], tmp_path / "reports")
    assert "`Outil RPF (WIP)`" in md.read_text(encoding="utf-8")

    # 4. Inspection VM (.wsb) + lecture sécurisée des fichiers en quarantaine (texte, ZIP, binaire)
    target = tmp_path / "GTA6_Sandbox_Downloads"
    wsb = generate_inspection_sandbox_wsb(target)
    assert wsb.exists()
    assert "<Networking>Disable</Networking>" in wsb.read_text(encoding="utf-8")

    qdir = target / "quarantine"
    (qdir / "symbols.toml.quarantine").write_text("entry = 0x140001000\n", encoding="utf-8")
    with zipfile.ZipFile(qdir / "pack.zip.quarantine", "w") as zf:
        zf.writestr("config/symbols.toml", "ok = true")
    (qdir / "fake.bin.quarantine").write_bytes(b"MZ\x90\x00\x03\x00\x00\x00")

    listed = list_quarantine_files(target)
    assert len(listed) == 3

    txt_res = read_quarantine_file_safely(target, "quarantine", "symbols.toml.quarantine")
    assert txt_res["ok"] and txt_res["mode"] == "text" and "0x140001000" in txt_res["content"]

    zip_res = read_quarantine_file_safely(target, "quarantine", "pack.zip.quarantine")
    assert zip_res["ok"] and zip_res["mode"] == "zip_listing" and "config/symbols.toml" in zip_res["content"]

    bin_res = read_quarantine_file_safely(target, "quarantine", "fake.bin.quarantine")
    assert bin_res["ok"] and bin_res["mode"] == "hexdump" and "4D 5A" in bin_res["content"]

    del_res = delete_quarantine_file(target, "quarantine", "fake.bin.quarantine")
    assert del_res["ok"] is True
    assert len(list_quarantine_files(target)) == 2


def test_forks_ai_spam_and_milestones() -> None:
    from src.notifiers.alerts import is_milestone_item

    # 1. Déduplication fork sans commit original + détection faux dépôt IA (README énorme / code vide)
    rf = _make_filter()
    now = datetime.now(timezone.utc)

    unchanged_fork = ScrapedItem(
        id="test:fork0",
        source=SourceType.GITHUB_REPO,
        title="cloneuser/GTA6Recomp",
        url="https://github.com/cloneuser/GTA6Recomp",
        summary="GTA 6 static recompilation",
        updated_at=now,
        github_meta=GitHubMetadata(
            repo_full_name="cloneuser/GTA6Recomp",
            primary_language="C++",
            languages={"C++": 100000},
            root_files=["CMakeLists.txt", "src"],
            owner_login="cloneuser",
            owner_created_at=now - timedelta(days=500),
            owner_public_repos=10,
            is_fork=True,
            fork_ahead_by=0,
            inspected=True,
        ),
    )
    res_fork = rf.evaluate(unchanged_fork)
    assert res_fork.rejected is True
    assert res_fork.rejection_reason and "fork" in res_fork.rejection_reason

    ai_spam_repo = ScrapedItem(
        id="test:aispam",
        source=SourceType.GITHUB_REPO,
        title="spammer/GTA6-RAGE9-Decomp",
        url="https://github.com/spammer/GTA6-RAGE9-Decomp",
        summary="GTA 6 RAGE9 static recompilation and PS5 emulator",
        updated_at=now,
        github_meta=GitHubMetadata(
            repo_full_name="spammer/GTA6-RAGE9-Decomp",
            primary_language="Python",
            languages={"Python": 700},
            root_files=["README.md", "main.py"],
            owner_login="spammer",
            owner_created_at=now - timedelta(days=500),
            owner_public_repos=10,
            readme_bytes=12000,
            inspected=True,
        ),
    )
    res_spam = rf.evaluate(ai_spam_repo)
    assert res_spam.rejected is True
    assert res_spam.rejection_reason and "spam IA" in res_spam.rejection_reason

    # 3. Alertes sur vrais caps (milestones) uniquement
    normal_item = ScrapedItem(
        id="test:m0",
        source=SourceType.GITHUB_REPO,
        title="dev/repo",
        url="https://github.com/dev/repo",
        playability_status="Code / Projet",
    )
    assert is_milestone_item(normal_item) is False

    milestone_item = ScrapedItem(
        id="test:m1",
        source=SourceType.GITHUB_REPO,
        title="dev/repo",
        url="https://github.com/dev/repo",
        playability_status="Load the world",
    )
    assert is_milestone_item(milestone_item) is True

    toml_item = ScrapedItem(
        id="test:m2",
        source=SourceType.GITHUB_CODE,
        title="dev/repo",
        url="https://github.com/dev/repo",
        playability_status="Recomp (WIP)",
        direct_links=[
            DirectLink(
                url="https://github.com/dev/repo/blob/main/symbols.toml",
                filename="symbols.toml",
                link_type="recomp_config",
                http_status=200,
                is_alive=True,
            )
        ],
    )
    assert is_milestone_item(toml_item) is True


def test_vm_sandbox_generator_and_drive_listing(tmp_path: Path) -> None:
    from src.sandbox.vm_downloader import (
        _detect_magic,
        generate_windows_sandbox_files,
        list_local_drives,
    )

    drives = list_local_drives()
    assert len(drives) >= 1
    assert all(d.free_gb >= 0 for d in drives)

    wsb_path, ps_path = generate_windows_sandbox_files(
        "https://github.com/dev/GTA6Recomp/releases/download/v0.1/symbols.toml",
        tmp_path / "GTA6_Sandbox_Downloads",
    )
    assert wsb_path.exists()
    assert ps_path.exists()
    wsb_xml = wsb_path.read_text(encoding="utf-8")
    assert "<ClipboardRedirection>Disable</ClipboardRedirection>" in wsb_xml
    assert "<ProtectedClient>Enable</ProtectedClient>" in wsb_xml
    assert "vm_fetch.ps1" in wsb_xml

    assert _detect_magic(b"MZ\x90\x00").startswith("PE/Windows Executable")
    assert _detect_magic(b"\x7fELF\x02\x01").startswith("ELF Binary")
    assert _detect_magic(b"PK\x03\x04").startswith("ZIP Archive")


def test_download_tracking_and_new_version_detection(tmp_path: Path) -> None:
    import gui
    from src.utils.boot_tester import run_boot_and_compile_test

    storage = Storage(tmp_path / "dl_track.db")
    t1 = datetime.now(timezone.utc) - timedelta(hours=5)

    item = ScrapedItem(
        id="test:dltrack:1",
        source=SourceType.GITHUB_REPO,
        title="dev/GTA6Recomp",
        url="https://github.com/dev/GTA6Recomp",
        summary="GTA 6 static recompilation",
        updated_at=t1,
        relevance_score=85,
        direct_links=[
            DirectLink(
                url="https://github.com/dev/GTA6Recomp/releases/download/v1/symbols.toml",
                filename="symbols.toml",
                link_type="recomp_config",
                http_status=200,
                content_length=1024,
                is_alive=True,
            )
        ],
    )
    storage.save_new_items([item])
    storage.record_download(
        url=item.direct_links[0].url,
        filename="symbols.toml.quarantine",
        saved_path=str(tmp_path / "symbols.toml.quarantine"),
        size_bytes=1024,
        item_id=item.id,
        item_updated_at=t1.isoformat(),
    )
    dl_url, dl_item = storage.get_download_history_map()
    assert item.id in dl_item
    assert item.direct_links[0].url in dl_url

    # Test suppression automatique de la version antérieure dans un dossier de rôle
    qdir = tmp_path / "by_role" / "3_Recompilateur" / "quarantine"
    qdir.mkdir(parents=True)
    old_f = qdir / "1000_old.quarantine"
    new_f = qdir / "2000_new.quarantine"
    old_f.write_text("old", encoding="utf-8")
    new_f.write_text("new", encoding="utf-8")
    removed = gui.replace_previous_role_files(qdir, new_f)
    assert removed == ["1000_old.quarantine"]
    assert not old_f.exists() and new_f.exists()

    # Test Boot & Compilation report
    report = run_boot_and_compile_test(tmp_path)
    assert 0 <= report.readiness_pct <= 100
    assert len(report.checks) >= 6


def test_dumps_pillar_categorization() -> None:
    rf = _make_filter()
    now = datetime.now(timezone.utc)

    dumper_repo = ScrapedItem(
        id="test:dump:1",
        source=SourceType.GITHUB_REPO,
        title="dev/PS5-RAGE9-Dumper",
        url="https://github.com/dev/PS5-RAGE9-Dumper",
        summary="PS5 dumper & FSELF/ELF dump tool for GTA 6 RAGE9 symbol dump and shader dump analysis.",
        updated_at=now,
        github_meta=GitHubMetadata(
            repo_full_name="dev/PS5-RAGE9-Dumper",
            primary_language="C++",
            languages={"C++": 80000},
            root_files=["CMakeLists.txt", "src"],
            owner_login="dev",
            owner_created_at=now - timedelta(days=500),
            owner_public_repos=10,
            inspected=True,
        ),
    )
    res = rf.evaluate(dumper_repo)
    assert res.rejected is False
    assert res.category == Category.DUMPS
    assert res.relevance_score >= 70


def test_role_and_dependencies_classifier() -> None:
    from src.utils.role_classifier import classify_role_and_deps

    recomp_item = ScrapedItem(
        id="test:rc:1",
        source=SourceType.GITHUB_REPO,
        title="dev/GTA6Recomp",
        url="https://github.com/dev/GTA6Recomp",
        summary="Static recompiler using LLVM, C++ CMake and symbols.toml for game_scarlett_master",
        category=Category.DECOMP,
    )
    track, role, deps = classify_role_and_deps(recomp_item)
    assert track == "PC Natif / Recomp"
    assert role in {"1. Portage / Exe PC", "3. Recompilateur Statique"}
    assert "LLVM" in deps
    assert "Xbox Scarlett" in deps
    assert "symbols.toml" in deps

    emu_item = ScrapedItem(
        id="test:rc:2",
        source=SourceType.GITHUB_REPO,
        title="dev/shadPS4-GTA6",
        url="https://github.com/dev/shadPS4-GTA6",
        summary="PS5 / Orbis emulator layer with Vulkan and GNMX shaders",
        category=Category.EMULATION,
    )
    track2, role2, deps2 = classify_role_and_deps(emu_item)
    assert track2 == "Exécution Dump / Ému"
    assert role2 == "2. Émulateur / Runtime"
    assert "Vulkan" in deps2
    assert "PS5 / Orbis (ELF/SPRX)" in deps2

    xbox_item = ScrapedItem(
        id="test:rc:3",
        source=SourceType.GITHUB_REPO,
        title="DesignWithTommy/Neonbridge",
        url="https://github.com/DesignWithTommy/Neonbridge",
        summary="Xbox Series Scarlett compatibility runtime implementing d3d12_xs.dll and xgameruntime",
        category=Category.EMULATION,
    )
    track3, role3, deps3 = classify_role_and_deps(xbox_item)
    assert track3 == "Exécution Dump / Ému"
    assert role3 == "2. Émulateur / Runtime"
    assert "DX12 / DXIL" in deps3
    assert "Xbox Scarlett" in deps3


def test_dual_boot_scores_curated_discovery_and_reclassify(tmp_path: Path) -> None:
    import gui
    from src.utils.boot_tester import run_boot_and_compile_test
    from src.utils.curated_discovery import extract_github_repos_from_text

    # 1. Double score Boot Test (Recomp PC vs Ému Xbox Series)
    report = run_boot_and_compile_test(tmp_path)
    assert 0 <= report.recomp_readiness_pct <= 100
    assert 0 <= report.xbox_readiness_pct <= 100
    assert any(c.track == "xbox" for c in report.checks)
    assert any(c.track == "recomp" for c in report.checks)

    # 2. Extraction des dépôts curatés (awesome-gta6 + pagiii)
    sample_md = """
    - [Neonbridge](https://github.com/DesignWithTommy/Neonbridge)
    - [Flamingo](https://github.com/realblastex/flamingo.git)
    - [Topics](https://github.com/topics/gta6)
    const repos = ["fxceless/victory-fork", "uiliannn/VItoriaBr"];
    """
    repos = extract_github_repos_from_text(sample_md, include_quoted_slugs=True)
    assert "DesignWithTommy/Neonbridge" in repos
    assert "realblastex/flamingo" in repos
    assert "fxceless/victory-fork" in repos
    assert "uiliannn/VItoriaBr" in repos
    assert all("topics/" not in r for r in repos)

    # 3. Reclassification de la base SQLite + filtre de voie
    storage = Storage(tmp_path / "reclass.db")
    storage.save_new_items(
        [
            ScrapedItem(
                id="test:rec:1",
                source=SourceType.GITHUB_REPO,
                title="DesignWithTommy/Neonbridge",
                url="https://github.com/DesignWithTommy/Neonbridge",
                summary="Xbox Series d3d12_xs runtime",
                relevance_score=80,
            ),
            ScrapedItem(
                id="test:rec:2",
                source=SourceType.GITHUB_REPO,
                title="dev/GTA6Recomp",
                url="https://github.com/dev/GTA6Recomp",
                summary="Static recompiler LLVM symbols.toml",
                relevance_score=85,
            ),
        ]
    )
    res = storage.reclassify_all_items()
    assert res["updated"] == 2
    assert res["xbox_count"] == 1
    assert res["recomp_count"] == 1
    assert gui.TrackerController._track_matches("Exécution Dump / Ému", "xbox") is True
    assert gui.TrackerController._track_matches("PC Natif / Recomp", "xbox") is False
    assert gui.TrackerController._track_matches("PC Natif / Recomp", "recomp") is True


def test_portwatch_parser_and_scoring() -> None:
    from src.scrapers.portwatch_scraper import parse_portwatch_payload

    sample_tracker = {
        "updated": "2026-11-25T22:09:30Z",
        "efforts": [
            {
                "id": "avatarmanz",
                "name": "AvatarManz",
                "repo": "avatarmanz/VIctory-7900XTX",
                "tier": "tracked",
                "gpu": ["AMD"],
                "approach": "Compatibility layer",
                "summary": "Playable at 16 fps",
                "gta": {
                    "loads": 1,
                    "boots": 1,
                    "menu": 1,
                    "world": 1,
                    "playable": 1,
                    "story": 0.5,
                    "render": 0.8,
                    "fps": 16,
                    "stability": 0.9,
                },
            }
        ],
        "tools": [
            {
                "name": "rpf9-explorer",
                "repo": "breadbbbb/rpf9-explorer",
                "kind": "Archive explorer",
                "desc": "RPF9 viewer",
            }
        ],
        "news": [{"date": "2026-11-25", "category": "Port", "title": "Test", "dek": "Dek"}],
    }
    sample_pinned = {"show": True, "text": "16 fps build live"}
    items, snap = parse_portwatch_payload(sample_tracker, sample_pinned)
    assert len(items) == 2
    assert snap["leaderboard"][0]["name"] == "AvatarManz"
    assert snap["leaderboard"][0]["score_pct"] >= 75.0
    assert snap["pinned"]["text"] == "16 fps build live"
    assert len(snap["tools"]) == 1


def test_downloads_dir_defaults_to_launcher_dir_and_is_modifiable(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import httpx
    import gui

    launcher_dir = tmp_path / "SixDeckApp"
    launcher_dir.mkdir(parents=True)
    monkeypatch.setattr(gui, "BASE_DIR", launcher_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", launcher_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", launcher_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "PORTWATCH_SNAPSHOT", launcher_dir / "data" / "portwatch_snapshot.json")
    monkeypatch.setattr(gui, "DISCOVERED_CACHE", launcher_dir / "data" / "discovered_repos.json")
    # Avoid background network warmup in test
    monkeypatch.setattr(gui.TrackerController, "_warmup_boot_cache", lambda self: None)

    cfg = load_config(CONFIG_PATH)
    ctrl = gui.TrackerController(cfg)

    # 1. Par défaut : même chemin que le launcher (BASE_DIR), jamais "D:\"
    assert ctrl.downloads_dir == launcher_dir
    assert ctrl.settings["downloads_dir"] == str(launcher_dir)
    assert ctrl.mods.mods_dir == launcher_dir / "Mods"
    assert ctrl._resolve_downloads_root() == launcher_dir

    state = ctrl.get_cockpit_state()
    assert state["downloads_dir"] == str(launcher_dir)
    assert state["default_downloads_dir"] == str(launcher_dir)
    assert state["mods_dir"] == str(launcher_dir / "Mods")
    assert state["components_dir"] == str(launcher_dir / "by_role")

    # 2. Modification du chemin d'installation
    custom_dir = tmp_path / "CustomDownloads"
    res = ctrl.update_launcher_settings({"downloads_dir": str(custom_dir)})
    assert res["ok"] is True
    assert ctrl.downloads_dir == custom_dir
    assert ctrl.settings["downloads_dir"] == str(custom_dir)
    assert ctrl.mods.mods_dir == custom_dir / "Mods"
    assert (custom_dir / "Mods").is_dir()
    assert ctrl._resolve_downloads_root() == custom_dir

    # Persistance dans launcher_settings.json
    reloaded_settings = gui.TrackerController._load_launcher_settings()
    assert reloaded_settings["downloads_dir"] == str(custom_dir)

    # 3. Téléchargement d'un composant dans le dossier personnalisé + rollback N-1
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"entry = 0x140001000\n"))

    async def _do_dl() -> dict:
        async with httpx.AsyncClient(transport=transport) as client:
            return await ctrl._download_and_replace_async(
                url="https://github.com/4lwayz/portwatch-vi/releases/download/v1/symbols.toml",
                role="4. Symboles / Config",
                client=client,
                replace_old=True,
            )

    dl_res = asyncio.run(_do_dl())
    assert dl_res["ok"] is True
    saved_p = Path(dl_res["saved_path"])
    assert saved_p.exists()
    assert custom_dir in saved_p.parents
    assert "by_role" in saved_p.parts

    # Vérifier qu'un second téléchargement place le précédent en _backup et que rollback_role le restaure
    backup_dir = saved_p.parent.parent / "_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / "old_symbols.toml.quarantine").write_text("old", encoding="utf-8")
    state_custom = ctrl.get_cockpit_state()
    role_row = next(r for r in state_custom["roles_table"] if r["role"] == "4. Symboles / Config")
    assert role_row["has_backup"] is True
    rb_res = ctrl.rollback_role("4. Symboles / Config")
    assert rb_res["ok"] is True
    assert (saved_p.parent / "old_symbols.toml.quarantine").exists()

    # 4. Réinitialisation au chemin par défaut du launcher (chaîne vide)
    reset_res = ctrl.update_launcher_settings({"downloads_dir": ""})
    assert reset_res["ok"] is True
    assert ctrl.downloads_dir == launcher_dir
    assert ctrl.mods.mods_dir == launcher_dir / "Mods"


def test_download_progress_callback_and_endpoint(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    import httpx
    import gui
    from src.sandbox.vm_downloader import quarantine_download

    payload = b"A" * 2048
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            content=payload,
            headers={"Content-Length": str(len(payload))},
        )
    )
    events: list[dict] = []

    async def _run_dl() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            rep = await quarantine_download(
                "https://example.org/mod_pack.zip",
                tmp_path,
                client,
                run_defender=False,
                on_progress=events.append,
            )
            assert rep.saved_path is not None and rep.saved_path.exists()

    asyncio.run(_run_dl())
    stages = [e["stage"] for e in events]
    assert "connecting" in stages
    assert "downloading" in stages
    assert "verifying" in stages
    assert stages[-1] == "done"
    assert events[-1]["percent"] == 100.0
    assert events[-1]["downloaded_bytes"] == len(payload)
    assert events[-1]["total_bytes"] == len(payload)

    # Vérifier l'endpoint HTTP /api/download-progress
    launcher_dir = tmp_path / "App"
    launcher_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", launcher_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", launcher_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", launcher_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "PORTWATCH_SNAPSHOT", launcher_dir / "data" / "portwatch_snapshot.json")
    monkeypatch.setattr(gui, "DISCOVERED_CACHE", launcher_dir / "data" / "discovered_repos.json")
    monkeypatch.setattr(gui.TrackerController, "_warmup_boot_cache", lambda self: None)

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH))
    ctrl._set_download_progress(
        "mod:open-vi-rpf9",
        {"kind": "mod", "id": "open-vi-rpf9", "stage": "downloading", "percent": 42.5, "downloaded_bytes": 425, "total_bytes": 1000},
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), gui.make_handler(ctrl))
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/download-progress") as resp:
            body = json.loads(resp.read())
        assert body["ok"] is True
        assert body["downloads"]["mod:open-vi-rpf9"]["percent"] == 42.5
        ctrl._clear_download_progress("mod:open-vi-rpf9")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/download-progress") as resp:
            body2 = json.loads(resp.read())
        assert body2["downloads"] == {}
    finally:
        srv.shutdown()
        srv.server_close()


def test_sandbox_provenance_whitelist_fork_and_recent_repo(tmp_path: Path, monkeypatch) -> None:
    from datetime import datetime, timedelta, timezone
    from src.sandbox.provenance import evaluate_provenance

    # 1. Auteur dans la liste blanche -> trusted
    ok_res = evaluate_provenance("https://github.com/4lwayz/portwatch-vi")
    assert ok_res.trusted is True
    assert ok_res.requires_confirmation is False
    assert ok_res.risk_level == "trusted"

    # 2. Auteur inconnu + fork + dépôt créé il y a 2 jours -> high risk + confirmation obligatoire
    recent_iso = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    bad_res = evaluate_provenance(
        "https://github.com/untrusted_dev/mystery-fork",
        repo_metadata={"fork": True, "created_at": recent_iso},
        min_repo_age_days=14,
    )
    assert bad_res.trusted is False
    assert bad_res.requires_confirmation is True
    assert bad_res.risk_level == "high"
    assert len(bad_res.reasons) >= 3

    # 3. Hôte hors GitHub -> confirmation obligatoire
    ext_res = evaluate_provenance("https://random-mirror.example.org/build.zip")
    assert ext_res.requires_confirmation is True
    assert ext_res.risk_level == "high"

    # 4. Vérification intégrée dans TrackerController._download_and_replace_async
    import asyncio
    import httpx
    import gui

    launcher_dir = tmp_path / "ProvApp"
    launcher_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", launcher_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", launcher_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", launcher_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui.TrackerController, "_warmup_boot_cache", lambda self: None)

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH))
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"ok-content"))

    async def _attempt(confirm: bool) -> dict:
        async with httpx.AsyncClient(transport=transport) as client:
            return await ctrl._download_and_replace_async(
                url="https://github.com/unknown_author/test-tool/releases/download/v1/tool.zip",
                role="5. Outil RPF / RAGE 9",
                client=client,
                confirm_untrusted=confirm,
            )

    # By default (enforce_provenance=False), downloads proceed without pre-download warning popups
    assert ctrl.settings["enforce_provenance"] is False
    direct_ok = asyncio.run(_attempt(False))
    assert direct_ok["ok"] is True

    # When opt-in provenance pinning is enabled, unknown authors require explicit confirmation
    ctrl.update_launcher_settings({"enforce_provenance": True})
    blocked = asyncio.run(_attempt(False))
    assert blocked["ok"] is False
    assert blocked.get("requires_confirmation") is True

    allowed = asyncio.run(_attempt(True))
    assert allowed["ok"] is True

    # Adding the owner to the whitelist lets downloads pass without confirmation even when strict mode is ON
    ctrl.trust_github_owner("unknown_author")
    trusted_now = asyncio.run(_attempt(False))
    assert trusted_now["ok"] is True


def test_mods_rpf_conflict_detection_and_non_destructive_staging(tmp_path: Path) -> None:
    from src.utils.mods_manager import ModsManager

    mods_dir = tmp_path / "Mods"
    mods_dir.mkdir()
    mod_a = mods_dir / "ModA_Textures"
    mod_b = mods_dir / "ModB_Overhaul"
    (mod_a / "x64").mkdir(parents=True)
    (mod_b / "x64").mkdir(parents=True)
    (mod_a / "x64" / "update.rpf").write_bytes(b"RPF9_MOD_A")
    (mod_a / "unique_a.rpf").write_bytes(b"RPF9_ONLY_A")
    (mod_b / "x64" / "update.rpf").write_bytes(b"RPF9_MOD_B")

    mgr = ModsManager(tmp_path / "mods_state.json", mods_dir)
    state = mgr.get_state()
    assert len(state["installed"]) == 2
    conflicts = state["conflicts"]
    assert len(conflicts) == 1
    assert conflicts[0]["is_rpf"] is True
    assert conflicts[0]["target"] == "x64/update.rpf"
    assert conflicts[0]["winner"] == "ModB_Overhaul"

    # Déploiement non destructif dans mod_stage (symlink ou hardlink, originaux intacts)
    stage_dir = tmp_path / "mod_stage"
    dep = mgr.deploy_non_destructive(stage_dir, vanilla=False)
    assert dep["ok"] is True
    assert dep["mode"] == "modded"
    assert dep["deployed_files"] == 3
    assert (stage_dir / "x64" / "update.rpf").read_bytes() == b"RPF9_MOD_B"
    assert (mod_a / "x64" / "update.rpf").read_bytes() == b"RPF9_MOD_A"
    assert (dep["methods"]["symlink"] + dep["methods"]["hardlink"]) >= 1

    # Mode Vanilla -> purge instantanée de mod_stage sans toucher aux mods ni au dump
    van = mgr.deploy_non_destructive(stage_dir, vanilla=True)
    assert van["ok"] is True
    assert van["mode"] == "vanilla"
    assert not stage_dir.exists()
    assert (mod_a / "x64" / "update.rpf").exists()


def test_support_bundle_and_strict_anonymization(tmp_path: Path) -> None:
    import zipfile
    from src.utils.support_bundle import anonymize_text, generate_support_bundle

    raw_sample = (
        "\x1b[31mERROR\x1b[0m path=C:\\Users\\AliceSecret\\Desktop\\dump "
        "linux=/home/bob_dev/SixDeck token=ghp_1234567890abcdefghij1234567890 "
        "GITHUB_TOKEN=supersecretvalue999 "
        "https://discord.com/api/webhooks/123456/secretTokenXYZ"
    )
    clean = anonymize_text(raw_sample, extra_secrets=["my_private_vt_key_42"], username="AliceSecret")
    assert "\x1b[" not in clean
    assert "AliceSecret" not in clean
    assert "bob_dev" not in clean
    assert "ghp_1234567890" not in clean
    assert "supersecretvalue999" not in clean
    assert "secretTokenXYZ" not in clean
    assert "C:\\Users\\***" in clean
    assert "/home/***" in clean

    log_file = tmp_path / "last_launch.log"
    log_file.write_text(
        "\x1b[32mINFO\x1b[0m Loading C:\\Users\\JohnDoe\\Games\\dump\n"
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\n",
        encoding="utf-8",
    )
    out = generate_support_bundle(
        tmp_path / "bundles",
        settings={
            "install_dir": "C:\\Users\\JohnDoe\\Games\\dump",
            "gpu_profile": "NVIDIA",
            "selected_runtime": "cool_fox (Direct)",
            "resolution": "1920x1080",
        },
        system_memory={"total_ram_gb": 32.0, "free_ram_gb": 18.0, "total_commit_gb": 40.0, "free_commit_gb": 22.0},
        mods_installed=[{"id": "m1", "name": "TestMod", "version": "1.0", "enabled": True, "risk": "assets"}],
        mod_conflicts=[],
        game_log_path=log_file,
        launcher_logs=["Opened /home/johndoe/SixDeck with my_custom_secret_token"],
        portable_mode=True,
        extra_secrets=["my_custom_secret_token"],
    )
    assert out["ok"] is True
    assert "JohnDoe" not in out["markdown"]
    assert "ghp_abcdefgh" not in out["markdown"]
    assert "my_custom_secret_token" not in out["markdown"]

    zp = Path(out["zip_path"])
    assert zp.exists()
    with zipfile.ZipFile(zp, "r") as zf:
        names = set(zf.namelist())
        assert {"REPORT.md", "environment.json", "settings_sanitized.json", "mods.json", "game_logs_clean.txt", "launcher_logs_clean.txt"} <= names
        glog = zf.read("game_logs_clean.txt").decode("utf-8")
        assert "JohnDoe" not in glog and "\x1b[" not in glog and "ghp_abcdefgh" not in glog
        llog = zf.read("launcher_logs_clean.txt").decode("utf-8")
        assert "my_custom_secret_token" not in llog and "johndoe" not in llog.lower()


def test_portable_mode_flag_and_relative_paths(tmp_path: Path, monkeypatch) -> None:
    import json
    import gui

    port_root = tmp_path / "USB_SixDeck"
    (port_root / "game").mkdir(parents=True)
    monkeypatch.setattr(gui, "BASE_DIR", port_root)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", port_root / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", port_root / "data" / "mods_state.json")
    monkeypatch.setattr(gui.TrackerController, "_warmup_boot_cache", lambda self: None)

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH))
    assert ctrl.is_portable_mode() is False

    res = ctrl.toggle_portable_mode(True)
    assert res["ok"] is True
    assert res["portable_mode"] is True
    assert (port_root / "portable.flag").exists()
    assert port_root in ctrl._saves_dir().parents
    assert port_root in ctrl._runtime_appdata_root().resolve().parents or ctrl._runtime_appdata_root().resolve() == (port_root / "portable_data").resolve()

    # Vérifier que launcher_settings.json sauvegarde bien des chemins relatifs
    saved_json = json.loads((port_root / "data" / "launcher_settings.json").read_text(encoding="utf-8"))
    assert saved_json["downloads_dir"] == "."
    assert saved_json["install_dir"] == "game"

    # Simuler le déplacement du dossier sur un autre disque (nouveau BASE_DIR)
    moved_root = tmp_path / "SDCard_SixDeck"
    import shutil
    shutil.copytree(port_root, moved_root)
    monkeypatch.setattr(gui, "BASE_DIR", moved_root)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", moved_root / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", moved_root / "data" / "mods_state.json")

    ctrl_moved = gui.TrackerController(gui.load_config(CONFIG_PATH))
    assert ctrl_moved.is_portable_mode() is True
    assert ctrl_moved.downloads_dir.resolve() == moved_root.resolve()
    assert Path(ctrl_moved.settings["install_dir"]).resolve() == (moved_root / "game").resolve()


def test_presets_export_import_share_code_and_gpu_ranking(tmp_path: Path, monkeypatch) -> None:
    import gui
    from src.utils.presets import export_preset, get_presets_for_gpu, parse_preset_input

    nv_list = get_presets_for_gpu("NVIDIA")
    amd_list = get_presets_for_gpu("AMD")
    assert nv_list[0]["gpu"] == "NVIDIA"
    assert amd_list[0]["gpu"] == "AMD"

    exp = export_preset(
        {"selected_runtime": "cool_fox (Direct)", "gpu_profile": "NVIDIA", "fps_limit": "120", "resolution": "1600x900", "render_scale": "0.85"},
        [{"id": "mod-b", "enabled": True}, {"id": "mod-a", "enabled": False}],
        name="Competitive 900p",
    )
    assert exp["share_code"].startswith("SIXDECK1:")

    parsed_code = parse_preset_input(exp["share_code"])
    assert parsed_code["name"] == "Competitive 900p"
    assert parsed_code["settings"]["fps_limit"] == "120"
    assert parsed_code["mods_order"][0] == {"id": "mod-b", "enabled": True}

    parsed_json = parse_preset_input(exp["json_text"])
    assert parsed_json["settings"]["resolution"] == "1600x900"

    # Application dans TrackerController
    app_dir = tmp_path / "PresetApp"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "list_local_drives", lambda: [])
    monkeypatch.setattr(gui.TrackerController, "_warmup_boot_cache", lambda self: None)

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH))
    apply_res = ctrl.apply_preset(raw_payload=exp["share_code"])
    assert apply_res["ok"] is True
    assert ctrl.settings["fps_limit"] == "120"
    assert ctrl.settings["resolution"] == "1600x900"


def test_dump_checksum_verifier_pinning_compat_saves_and_steam(tmp_path: Path, monkeypatch) -> None:
    import gui
    from src.storage import Storage, classify_compat_badge
    from src.utils.dump_verifier import build_local_manifest, compute_file_checksum, verify_dump_directory
    from src.utils.steam_shortcut import export_steam_shortcut

    # 1. Checksums (SHA-256, MD5, SHA-1, CRC32) + manifeste Valide / Corrompu / Incomplet
    dump_dir = tmp_path / "Dump"
    dump_dir.mkdir()
    exe = dump_dir / "game_scarlett_master.exe"
    rpf = dump_dir / "common.rpf"
    exe.write_bytes(b"MZ_FAKE_TEST_DATA")
    rpf.write_bytes(b"RPF9_ARCHIVE_PAYLOAD")

    for algo in ("sha256", "md5", "sha1", "crc32"):
        digest = compute_file_checksum(exe, algo)
        assert len(digest) >= 8

    manifest = build_local_manifest(dump_dir, algorithm="sha256")
    rep_ok = verify_dump_directory(dump_dir, manifest_data=manifest, algorithm="sha256")
    assert rep_ok.status == "valid"
    assert rep_ok.valid_count == 2

    # Corrompre un fichier
    rpf.write_bytes(b"CORRUPTED_DATA")
    rep_bad = verify_dump_directory(dump_dir, manifest_data=manifest, algorithm="sha256")
    assert rep_bad.status == "corrupted"
    assert rep_bad.corrupted_count == 1

    # Supprimer un fichier -> incomplet
    rpf.unlink()
    rep_inc = verify_dump_directory(dump_dir, manifest_data=manifest, algorithm="sha256")
    assert rep_inc.status == "incomplete"
    assert rep_inc.missing_count == 1

    # 2. Suivi compatibilité SQLite + badges standardisés (anglais)
    st = Storage(tmp_path / "compat.db")
    st.record_runtime_compat("cool_fox", "Perfect", fps=60, crashed=False)
    cmap = st.get_runtime_compat_map()
    assert cmap["cool_fox"]["fps"] == 60
    assert classify_compat_badge("Playable 60 FPS", score_pct=92, fps=60)["label"] == "Perfect"
    assert classify_compat_badge("Playable 25 FPS", score_pct=65, fps=25)["label"] == "Playable"
    assert classify_compat_badge("Load the world", score_pct=40)["label"] == "In-Game"
    assert classify_compat_badge("Boots / Menu", score_pct=18)["label"] == "Menus"
    assert classify_compat_badge("Crash on startup")["label"] == "Crash"

    # 3. Pinning de version + rétention backup + Cloud Sync
    app_dir = tmp_path / "SixDeckFull"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "list_local_drives", lambda: [])
    monkeypatch.setattr(gui.TrackerController, "_warmup_boot_cache", lambda self: None)

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH))
    ctrl.toggle_portable_mode(True)
    pin_res = ctrl.toggle_pin_role("2. Émulateur / Runtime", item_id="rt1", title="cool_fox", version="v1.2", pinned=True)
    assert pin_res["ok"] is True and pin_res["pinned"] is True
    assert "2. Émulateur / Runtime" in ctrl.settings["pinned_roles"]

    # Cloud Sync des sauvegardes + rétention configurable
    (ctrl._saves_dir() / "slot1.sav").write_bytes(b"SAVE_DATA_1")
    ctrl.update_launcher_settings({"save_backup_retention": 2, "save_sync_dir": str(tmp_path / "CloudDrive")})
    b1 = ctrl.backup_saves()
    assert b1["ok"] is True
    sync_res = ctrl.sync_saves_to_cloud()
    assert sync_res["ok"] is True
    assert (tmp_path / "CloudDrive" / "SixDeck_Saves" / "slot1.sav").exists()

    # 4. Export raccourci Steam VDF
    steam_cfg = tmp_path / "steam_userdata" / "12345" / "config"
    s_res = export_steam_shortcut(
        app_name="SixDeck",
        exe_path=app_dir / "SixDeck.exe",
        start_dir=app_dir,
        steam_config_dir=steam_cfg,
    )
    assert s_res["ok"] is True
    assert (steam_cfg / "shortcuts.vdf").is_file()
    raw_vdf = (steam_cfg / "shortcuts.vdf").read_bytes()
    assert b"SixDeck" in raw_vdf and raw_vdf.endswith(b"\x08\x08")


def test_legacy_drive_d_never_fails_and_falls_back_to_downloads_dir(tmp_path: Path, monkeypatch) -> None:
    import gui

    app_dir = tmp_path / "NoDriveDApp"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "list_local_drives", lambda: [])
    monkeypatch.setattr(gui.TrackerController, "_warmup_boot_cache", lambda self: None)

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH))
    resolved = ctrl._resolve_downloads_root(drive="D")
    assert resolved is not None
    assert resolved == app_dir
    boot = ctrl.run_boot_test(drive="D")
    assert boot["ok"] is True


def test_github_atom_feeds_zero_rest_quota() -> None:
    import asyncio
    import httpx
    from src.utils.github_atom import fetch_github_commits_atom, fetch_github_releases_atom

    releases_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>v2.1.0 Stable</title>
        <link href="https://github.com/SixDeck/SixDeck/releases/tag/v2.1.0"/>
        <updated>2026-11-27T18:00:00Z</updated>
        <author><name>sixdeck-dev</name></author>
        <content type="html">&lt;p&gt;Added Atom feeds &amp; mirrors.&lt;/p&gt;</content>
      </entry>
    </feed>"""
    commits_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <id>tag:github.com,2008:Grit::Commit/abcdef1234567890abcdef1234567890abcdef12</id>
        <title>Add mirror fallback support</title>
        <link href="https://github.com/SixDeck/SixDeck/commit/abcdef1234567890abcdef1234567890abcdef12"/>
        <updated>2026-11-27T19:30:00Z</updated>
        <author><name>dev1</name></author>
      </entry>
    </feed>"""

    seen_urls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen_urls.append(str(req.url))
        if str(req.url).endswith("/releases.atom"):
            return httpx.Response(200, text=releases_xml)
        if str(req.url).endswith("/commits/main.atom"):
            return httpx.Response(200, text=commits_xml)
        return httpx.Response(404)

    async def _run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            rels = await fetch_github_releases_atom("SixDeck/SixDeck", client)
            coms = await fetch_github_commits_atom("SixDeck/SixDeck", client)
            assert len(rels) == 1
            assert rels[0]["tag"] == "v2.1.0"
            assert "Atom feeds" in rels[0]["notes"]
            assert len(coms) == 1
            assert coms[0]["sha"] == "abcdef1"
            assert coms[0]["message"] == "Add mirror fallback support"

    asyncio.run(_run())
    assert all("api.github.com" not in u for u in seen_urls)


def test_package_registries_mod_hubs_and_alt_git_mirror_fallback(tmp_path: Path) -> None:
    import asyncio
    import httpx
    from src.utils.alt_git_mirrors import (
        detect_forge_kind,
        download_with_mirror_fallback,
        get_mirrors_for_url,
        resolve_forge_archive_url,
    )
    from src.utils.mod_hubs import fetch_gamebanana_mods, fetch_nexus_mods, parse_modding_rss
    from src.utils.mods_manager import ModsManager
    from src.utils.package_registries import resolve_registry_package

    # 1. Forges Alt-Git + résolution d'URLs
    assert detect_forge_kind("https://codeberg.org/dev/tool") == "codeberg"
    assert detect_forge_kind("https://gitlab.com/org/sub/tool") == "gitlab"
    assert detect_forge_kind("https://git.sr.ht/~alice/tool") == "sourcehut"
    assert detect_forge_kind("https://git.myhost.org/team/tool", gitea_hosts=["https://git.myhost.org"]) == "gitea"
    assert resolve_forge_archive_url("https://git.sr.ht/~alice/tool").endswith("/archive/HEAD.tar.gz")
    assert resolve_forge_archive_url("https://codeberg.org/dev/tool").endswith("/archive/HEAD.zip")
    assert resolve_forge_archive_url("https://gitlab.com/org/tool").endswith("/-/archive/main/tool-main.zip")

    mirrors = get_mirrors_for_url(
        "https://github.com/Ahmet-Dedeler/bcpack-rs",
        {"Ahmet-Dedeler/bcpack-rs": ["https://codeberg.org/community/bcpack-rs"]},
    )
    assert mirrors == ["https://codeberg.org/community/bcpack-rs"]

    # 2. Bascule automatique GitHub (404) -> miroir Codeberg (200)
    def mirror_handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "github.com" in u:
            return httpx.Response(404)
        if "codeberg.org" in u:
            return httpx.Response(200, content=b"PK\x03\x04MIRROR_OK")
        if "crates.io/api/v1/crates/bcpack-rs" in u:
            return httpx.Response(200, json={"crate": {"max_stable_version": "0.4.2", "description": "BC7 codec", "downloads": 1200}})
        if "pypi.org/pypi/rage-tools/json" in u:
            return httpx.Response(200, json={"info": {"version": "1.3.0", "summary": "RAGE Python CLI"}, "urls": [{"packagetype": "bdist_wheel", "url": "https://files.pythonhosted.org/rage_tools-1.3.0.whl"}]})
        if "api.nuget.org/v3-flatcontainer/openrage.core/index.json" in u:
            return httpx.Response(200, json={"versions": ["1.0.0", "1.1.0-beta1", "1.2.0"]})
        if "api.nexusmods.com" in u:
            return httpx.Response(200, json=[{"mod_id": 42, "name": "Vice HD Pack", "author": "ModderX", "version": "2.0", "summary": "HD textures"}])
        if "gamebanana.com/apiv11" in u:
            return httpx.Response(200, json={"_aRecords": [
                {"_idRow": 777, "_sName": "GTA VI Leonida Neon Cars", "_sProfileUrl": "https://gamebanana.com/mods/777", "_aSubmitter": {"_sName": "GBUser"}},
                {"_idRow": 888, "_sName": "Unrelated Minecraft Skin", "_sProfileUrl": "https://gamebanana.com/mods/888", "_aSubmitter": {"_sName": "Other"}},
            ]})
        return httpx.Response(404)

    async def _run_all() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(mirror_handler)) as client:
            rep, used_url, used_fallback = await download_with_mirror_fallback(
                "https://github.com/Ahmet-Dedeler/bcpack-rs",
                mirrors,
                tmp_path,
                client,
            )
            assert used_fallback is True
            assert "codeberg.org" in used_url
            assert rep.saved_path is not None and rep.saved_path.exists()

            c_info = await resolve_registry_package("crates.io", "bcpack-rs", client)
            p_info = await resolve_registry_package("pypi", "rage-tools", client)
            n_info = await resolve_registry_package("nuget", "OpenRage.Core", client)
            assert c_info is not None and c_info["version"] == "0.4.2"
            assert p_info is not None and p_info["version"] == "1.3.0" and p_info["download_url"].endswith(".whl")
            assert n_info is not None and n_info["version"] == "1.2.0" and n_info["download_url"].endswith(".nupkg")

            nx = await fetch_nexus_mods("gtavi", "fake-api-key", client)
            gb = await fetch_gamebanana_mods("GTA", client)
            assert len(nx) == 1 and nx[0]["source_hub"] == "Nexus Mods"
            assert len(gb) == 1 and gb[0]["source_hub"] == "GameBanana"

            rss_items = parse_modding_rss(
                "<rss><channel><item><title>Leonida Map Tool</title><link>https://mods.example.org/1</link><description>Map viewer</description></item></channel></rss>",
                portal_name="ModPortal",
            )
            assert len(rss_items) == 1 and rss_items[0]["source_hub"] == "ModPortal"

            # Intégration dans ModsManager.get_state()
            mgr = ModsManager(tmp_path / "mods_state.json", tmp_path / "Mods")
            st = mgr.get_state(hub_items=[*nx, *gb, *rss_items], registry_versions={"bcpack-rs": c_info})
            ws_by_id = {w["id"]: w for w in st["workshop"]}
            assert ws_by_id["bcpack-rs"]["version"] == "v0.4.2"
            assert ws_by_id["bcpack-rs"]["source_hub"] == "CRATES.IO"
            assert any(w.get("source_hub") == "Nexus Mods" for w in st["workshop"])
            assert any(w.get("source_hub") == "GameBanana" for w in st["workshop"])

    asyncio.run(_run_all())


def test_mod_deletion_and_game_linking_verification(tmp_path: Path) -> None:
    from src.utils.mods_manager import ModsManager

    game_dir = tmp_path / "GameDump"
    game_dir.mkdir()
    orig_exe = game_dir / "game_scarlett_master.exe"
    orig_rpf = game_dir / "common.rpf"
    orig_exe.write_bytes(b"ORIGINAL_EXE")
    orig_rpf.write_bytes(b"ORIGINAL_RPF")

    mods_dir = tmp_path / "Mods"
    stage_dir = tmp_path / "mod_stage"
    mod_folder = mods_dir / "Leonida_Lighting"
    mod_folder.mkdir(parents=True)
    (mod_folder / "lighting.rpf").write_bytes(b"MOD_LIGHTING_RPF")

    mgr = ModsManager(tmp_path / "mods_state.json", mods_dir)

    # 1. Link mods to game non-destructively & verify
    link_res = mgr.link_mods_to_game(game_dir, stage_dir, vanilla=False)
    assert link_res["ok"] is True
    assert link_res["game_linked"] is True
    assert (game_dir / "sixdeck_mods" / "lighting.rpf").read_bytes() == b"MOD_LIGHTING_RPF"
    # Original game dump files remain 100% untouched
    assert orig_exe.read_bytes() == b"ORIGINAL_EXE"
    assert orig_rpf.read_bytes() == b"ORIGINAL_RPF"

    v_linked = mgr.verify_game_links(game_dir, stage_dir, vanilla=False)
    assert v_linked["status"] == "linked"
    assert v_linked["linked_count"] == 1
    assert v_linked["per_mod"]["leonida_lighting"]["label"] == "Linked to Game"

    # 2. Vanilla Mode immediately unlinks <game_dir>/sixdeck_mods
    van_res = mgr.link_mods_to_game(game_dir, stage_dir, vanilla=True)
    assert van_res["game_linked"] is False
    assert not (game_dir / "sixdeck_mods").exists()
    assert orig_rpf.read_bytes() == b"ORIGINAL_RPF"

    # 3. Re-link then delete mod -> removes entry, deletes files inside Mods/, and cleans game link
    mgr.link_mods_to_game(game_dir, stage_dir, vanilla=False)
    del_res = mgr.delete_mod("leonida_lighting", stage_dir=stage_dir, game_dir=game_dir, vanilla=False)
    assert del_res["ok"] is True
    assert del_res["files_removed"] is True
    assert not mod_folder.exists()
    assert not (game_dir / "sixdeck_mods").exists()
    assert mgr.sync_disk_mods() == []


def test_multiple_profiles_isolation_and_crud(tmp_path: Path, monkeypatch) -> None:
    import gui

    app_dir = tmp_path / "ProfilesApp"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "PORTWATCH_SNAPSHOT", app_dir / "data" / "portwatch_snapshot.json")

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH), skip_warmup=True)
    ctrl.toggle_portable_mode(True)

    profs = ctrl.list_profiles()
    ids = {p["id"] for p in profs["profiles"]}
    assert {"default", "vanilla", "perf-60", "experimental"} <= ids
    assert ctrl.active_profile == "default"
    default_saves = ctrl._saves_dir()
    default_mods_state = ctrl.mods.state_path

    # Switch to Perf 60 FPS -> isolated settings, mods_state.json & saves folder
    sw = ctrl.switch_profile("perf-60")
    assert sw["ok"] is True
    assert ctrl.active_profile == "perf-60"
    assert ctrl.settings["resolution"] == "1600x900"
    assert ctrl.settings["render_scale"] == "0.85"
    assert ctrl.settings["console_profile"] == "series-s"
    assert ctrl._saves_dir() != default_saves
    assert ctrl.mods.state_path != default_mods_state

    # Create a custom profile, customize it, check CLI command, and delete it
    cr = ctrl.create_profile("Ultra 4K Photo", description="4K screenshots", clone_current=True)
    assert cr["ok"] is True
    assert ctrl.active_profile == "ultra-4k-photo"
    cli = ctrl.get_profile_cli_command("ultra-4k-photo")
    assert "--launch" in cli["command"] and "ultra-4k-photo" in cli["command"]

    # Built-in profiles cannot be deleted
    assert ctrl.delete_profile("default")["ok"] is False
    assert ctrl.delete_profile("vanilla")["ok"] is False

    # Custom profile can be deleted and falls back cleanly to Default
    dl = ctrl.delete_profile("ultra-4k-photo")
    assert dl["ok"] is True
    assert ctrl.active_profile == "default"


def test_cli_direct_launch_and_list_profiles(tmp_path: Path, monkeypatch, capsys) -> None:
    import gui
    import launcher_app

    app_dir = tmp_path / "CliApp"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")

    args = launcher_app.parse_cli_args(["--list-profiles"])
    rc = launcher_app.run_cli_headless(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "default" in out and "perf-60" in out and "vanilla" in out

    # Test --launch <profile> --safe-mode --offline invokes play_game(safe_mode=True) headlessly
    captured: dict = {}

    def _fake_play(self, *, safe_mode: bool = False) -> dict:
        captured["profile"] = self.active_profile
        captured["safe_mode"] = safe_mode
        captured["offline"] = self.is_offline_mode()
        return {"ok": True, "pid": 9999, "message": "Launched mock runtime"}

    monkeypatch.setattr(gui.TrackerController, "play_game", _fake_play)
    args2 = launcher_app.parse_cli_args(["--launch", "perf-60", "--safe-mode", "--offline"])
    rc2 = launcher_app.run_cli_headless(args2)
    assert rc2 == 0
    assert captured == {"profile": "perf-60", "safe_mode": True, "offline": True}


def test_safe_mode_launch_overrides_without_mutating_settings(tmp_path: Path, monkeypatch) -> None:
    import gui

    app_dir = tmp_path / "SafeApp"
    game_dir = app_dir / "game"
    rt_dir = app_dir / "runtimes" / "cool_fox"
    game_dir.mkdir(parents=True)
    rt_dir.mkdir(parents=True)
    (game_dir / "game_scarlett_master.exe").write_bytes(b"GAME")
    fake_rt = rt_dir / "victory.exe"
    fake_rt.write_bytes(b"RT")

    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH), skip_warmup=True)
    ctrl.update_launcher_settings(
        {
            "install_dir": str(game_dir),
            "selected_runtime": "cool_fox (Direct)",
            "last_good_runtime": "cool_fox (Direct)",
            "resolution": "2560x1440",
            "render_scale": "1.0",
            "ray_tracing": True,
            "vanilla_mode": False,
            "fps_limit": "144",
        }
    )

    spawned: dict = {}

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

        def wait(self):
            return 0

    def _fake_popen(cmd, **kwargs):
        spawned["cmd"] = list(cmd)
        spawned["env"] = dict(kwargs.get("env") or {})
        return _FakeProc()

    monkeypatch.setattr(gui.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    res = ctrl.play_game(safe_mode=True)
    assert res["ok"] is True
    assert res["safe_mode"] is True
    assert spawned["env"]["SIXDECK_SAFE_MODE"] == "1"
    assert spawned["env"]["VICTORY_OUTPUT"] == "1280x720"
    assert spawned["env"]["VICTORY_RENDER_SCALE"] == "0.75"
    assert spawned["env"]["VICTORY_RAYTRACING"] == "0"
    assert spawned["env"]["VICTORY_FPS_LIMIT"] == "30"
    assert "--console" in spawned["cmd"] and "series-s" in spawned["cmd"]
    # Normal settings must remain completely untouched
    assert ctrl.settings["resolution"] == "2560x1440"
    assert ctrl.settings["ray_tracing"] is True
    assert ctrl.settings["vanilla_mode"] is False


def test_system_optimizer_affinity_memory_and_power_plan() -> None:
    from src.utils.sys_optimizer import (
        compute_affinity_mask,
        cores_to_bitmask,
        prepare_system_for_launch,
        restore_system_after_launch,
        trim_process_memory,
    )

    assert compute_affinity_mask(24, "p_cores") == list(range(12))
    assert compute_affinity_mask(16, "ccd0") == list(range(8))
    assert compute_affinity_mask(8, "auto") == list(range(8))
    assert cores_to_bitmask([0, 1, 2, 3]) == 0b1111

    mem = trim_process_memory()
    assert "gc_collected" in mem
    ctx = prepare_system_for_launch(high_perf_power=False, trim_memory=True, low_memory=False)
    assert ctx["power_switched"] is False
    assert ctx["memory_trimmed"] is not None
    assert restore_system_after_launch(ctx) is False


def test_strict_offline_mode_blocks_all_network_requests(tmp_path: Path, monkeypatch) -> None:
    import gui

    app_dir = tmp_path / "OfflineApp"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH), skip_warmup=True)
    tog = ctrl.toggle_offline_mode(True)
    assert tog["ok"] is True and tog["offline_mode"] is True

    # All network-dependent operations must be blocked immediately with zero HTTP calls
    for res in (
        ctrl.sync_portwatch(),
        ctrl.check_updates_now(),
        ctrl.start(),
        ctrl.download_role_update(url="https://github.com/4lwayz/portwatch-vi", role="2. Émulateur / Runtime"),
        ctrl.install_workshop_mod("open-vi-rpf9"),
        ctrl.sync_mod_hubs_and_registries(),
        ctrl.sync_online_news(),
        ctrl.run_curated_discovery(),
        ctrl.get_whats_new("https://github.com/4lwayz/portwatch-vi"),
    ):
        assert res["ok"] is False
        assert res.get("offline") is True

    state = ctrl.get_cockpit_state()
    assert state["offline_mode"] is True


def test_mod_dependency_graph_blocks_missing_prereqs_and_dependents(tmp_path: Path) -> None:
    import json
    from src.utils.mods_manager import ModsManager

    mods_dir = tmp_path / "Mods"
    core_dir = mods_dir / "CoreHook"
    addon_dir = mods_dir / "WeatherAddon"
    core_dir.mkdir(parents=True)
    addon_dir.mkdir(parents=True)
    (core_dir / "hook.dll").write_bytes(b"DLL")
    (addon_dir / "weather.rpf").write_bytes(b"RPF")
    (addon_dir / "mod.json").write_text(
        json.dumps({"requires": {"mods": ["corehook"], "min_runtime": "1.2.0"}}),
        encoding="utf-8",
    )

    mgr = ModsManager(tmp_path / "mods_state.json", mods_dir)
    # Initialize both disabled first so we can test dependency blocking cleanly
    mods = mgr.sync_disk_mods()
    for m in mods:
        m["enabled"] = False
    mgr._save(mods)

    # 1. Enabling addon while corehook is disabled -> BLOCKED
    r1 = mgr.toggle("weatheraddon", True, runtime_version="v1.5.0")
    assert r1["ok"] is False
    assert r1.get("blocked_by_dependencies") is True
    assert any(x["id"] == "corehook" for x in r1.get("missing_mods", []))

    # 2. Enable corehook, then try enabling addon with too-old runtime -> BLOCKED
    assert mgr.toggle("corehook", True)["ok"] is True
    r2 = mgr.toggle("weatheraddon", True, runtime_version="v1.0.0")
    assert r2["ok"] is False
    assert r2.get("blocked_by_dependencies") is True
    assert "1.2.0" in r2.get("missing_runtime", "")

    # 3. Enable addon with compatible runtime -> SUCCEEDS
    r3 = mgr.toggle("weatheraddon", True, runtime_version="v1.4.2")
    assert r3["ok"] is True

    # 4. Disabling corehook while weatheraddon is active -> BLOCKED
    r4 = mgr.toggle("corehook", False, runtime_version="v1.4.2")
    assert r4["ok"] is False
    assert r4.get("blocked_by_dependencies") is True
    assert "WeatherAddon" in r4.get("required_by", [])

    # 5. Disable addon first, then disable corehook -> SUCCEEDS
    assert mgr.toggle("weatheraddon", False)["ok"] is True
    assert mgr.toggle("corehook", False)["ok"] is True

    # 6. Workshop cards expose dependency annotations (e.g. rage9-re requires openrage)
    st = mgr.get_state(runtime_version="v1.4.2")
    ws_map = {w["id"]: w for w in st["workshop"]}
    assert "openrage" in ws_map["rage9-re"]["requires"]["mods"]
    assert any(x["id"] == "openrage" for x in ws_map["rage9-re"]["missing_dependencies"])


def test_pc_race_second_tracker_scraper_and_cockpit_integration(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import httpx
    import gui
    from src.scrapers.pc_race_scraper import (
        PC_RACE_DATA_URL,
        PcRaceScraper,
        compute_pc_race_score,
        load_pc_race_snapshot,
        parse_pc_race_payload,
    )
    from src.utils.curated_discovery import CURATED_SOURCES

    sample_payload = {
        "checked": "2026-11-28T00:17:00Z",
        "playable": "Yes, by one project's own report, unverified",
        "leader": "Vice-Tay",
        "headline": "Vice-Tay reports 30-minute playable session",
        "summary": "Summary of the race leaderboard.",
        "stages": [
            "Not started",
            "Loads",
            "Starts up",
            "Graphics device",
            "First frames",
            "Recognisable picture",
            "In-game",
            "Playable",
        ],
        "projects": [
            {
                "name": "Vice-Tay",
                "repo": "TaynanGT/Vice-Tay",
                "kind": "Fork of VIctory tuned on an RTX 4090",
                "stage": 7,
                "pos": 1,
                "changed": "2026-11-27T23:19:00Z",
                "status": "30 minutes in the world at median 21.1 fps.",
                "tested": {"os": ["Windows", "Linux"], "gpu": ["NVIDIA"]},
                "lanes": {
                    "picture": {"step": 3, "text": "good picture"},
                    "speed": {"low": 21.1, "high": 21.1, "text": "21.1 fps median"},
                    "stability": {"minutes": 30, "text": "30 min"},
                },
            },
            {
                "name": "PrivateFork",
                "repo": "secret/private-fork",
                "private": True,
                "stage": 5,
                "pos": "43=",
                "tested": {"os": ["Windows"], "gpu": ["AMD"]},
                "lanes": {"speed": {"low": 15, "high": 18}},
            },
        ],
        "watching": [{"name": "WatchProj", "repo": "dev/watchproj", "note": "Monitoring"}],
        "updates": [
            {
                "t": "2026-11-27T23:19:00Z",
                "title": "Vice-Tay status update",
                "points": ["First point about the build."],
            }
        ],
    }

    items, snap = parse_pc_race_payload(sample_payload)
    # Only public repo is added as ScrapedItem; both appear in leaderboard
    assert len(items) == 1
    assert items[0].title == "TaynanGT/Vice-Tay"
    assert items[0].extra["pc_race_verified"] is True
    assert items[0].extra["pc_race_stage"] == 7
    assert compute_pc_race_score(sample_payload["projects"][0]) >= 85.0
    assert len(snap["leaderboard"]) == 2
    assert snap["leaderboard"][0]["name"] == "Vice-Tay"
    assert snap["leaderboard"][1]["private"] is True
    assert snap["updates"][0]["title"] == "Vice-Tay status update"

    # Test PcRaceScraper.fetch with httpx.MockTransport + snapshot persistence
    snap_file = tmp_path / "pc_race_snapshot.json"
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=sample_payload)
        if str(req.url) == PC_RACE_DATA_URL
        else httpx.Response(404)
    )

    async def _run() -> list:
        async with httpx.AsyncClient(transport=transport) as client:
            scraper = PcRaceScraper(gui.load_config(CONFIG_PATH), client, snapshot_path=snap_file)
            return await scraper.fetch()

    fetched = asyncio.run(_run())
    assert len(fetched) == 1
    loaded = load_pc_race_snapshot(snap_file)
    assert loaded["leader"] == "Vice-Tay"
    assert len(loaded["leaderboard"]) == 2

    # Verify cockpit state exposes pc_race with compat_badge and Offline Mode blocks sync_pc_race
    app_dir = tmp_path / "PcRaceApp"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "PORTWATCH_SNAPSHOT", app_dir / "data" / "portwatch_snapshot.json")
    monkeypatch.setattr(gui, "PC_RACE_SNAPSHOT", snap_file)

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH), skip_warmup=True)
    cockpit = ctrl.get_cockpit_state()
    assert "pc_race" in cockpit
    assert len(cockpit["pc_race"]["leaderboard"]) == 2
    assert cockpit["pc_race"]["leaderboard"][0]["compat_badge"]["label"] == "Playable"

    ctrl.toggle_offline_mode(True)
    assert ctrl.sync_pc_race()["offline"] is True

    # Verify curated discovery & config.yaml include vuyp/gta6-pc-race
    assert any("vuyp/gta6-pc-race" in url for _, url in CURATED_SOURCES)
    cfg = gui.load_config(CONFIG_PATH)
    assert "vuyp/gta6-pc-race" in cfg.sources.github.watched_repos
    assert "vuyp" in cfg.sandbox_provenance.trusted_github_owners


def test_symbols_shaders_and_dumper_component_roles_populated(tmp_path: Path, monkeypatch) -> None:
    import gui

    app_dir = tmp_path / "AllRolesApp"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "PORTWATCH_SNAPSHOT", app_dir / "data" / "portwatch_snapshot.json")
    monkeypatch.setattr(gui, "PC_RACE_SNAPSHOT", app_dir / "data" / "pc_race_snapshot.json")

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH), skip_warmup=True)
    state = ctrl.get_cockpit_state()
    by_role = {r["role"]: r for r in state["roles_table"]}

    for required_role in ("4. Symboles / Config", "6. Shaders / GPU", "7. Dumper / ELF-SPRX"):
        assert required_role in by_role
        assert by_role[required_role]["best"] is not None, f"No candidate for {required_role}"
        assert by_role[required_role]["best"]["url"].startswith("https://github.com/")


def test_quickstart_flow_and_no_default_download_warnings(tmp_path: Path, monkeypatch) -> None:
    import gui

    app_dir = tmp_path / "QuickStartApp"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "PORTWATCH_SNAPSHOT", app_dir / "data" / "portwatch_snapshot.json")
    monkeypatch.setattr(gui, "PC_RACE_SNAPSHOT", app_dir / "data" / "pc_race_snapshot.json")

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH), skip_warmup=True)
    # 1. Defaults: Quick Start active on first launch & pre-download warnings OFF
    assert ctrl.settings["quickstart_completed"] is False
    assert ctrl.settings["enforce_provenance"] is False

    # 2. Step 1: Choose custom install path & GPU config
    custom_dl = tmp_path / "MyInstall"
    s1 = ctrl.update_launcher_settings({"downloads_dir": str(custom_dl), "gpu_profile": "AMD"})
    assert s1["ok"] is True
    assert ctrl.downloads_dir == custom_dl
    assert ctrl.gpu_profile == "AMD"

    # 3. Step 2: One-click install_all_config_components downloads GPU-matching components without warnings
    called_roles: list[str] = []

    def _fake_dl(self, *, url, role, downloads_dir=None, item_id=None, replace_old=True, confirm_untrusted=False, drive=""):
        assert confirm_untrusted is True
        called_roles.append(role)
        return {"ok": True, "role": role, "saved_path": str(custom_dl / "dummy.zip")}

    monkeypatch.setattr(gui.TrackerController, "download_role_update", _fake_dl)
    s2 = ctrl.install_all_config_components(gpu_profile="AMD")
    assert s2["ok"] is True
    assert s2["gpu_profile"] == "AMD"
    assert len(s2["installed"]) >= 3
    assert len(called_roles) == len(s2["installed"])

    # 4. Step 3: Skippable completion persists quickstart_completed=True
    ctrl.update_launcher_settings({"quickstart_completed": True})
    reloaded = gui.TrackerController._load_launcher_settings()
    assert reloaded["quickstart_completed"] is True


def test_import_local_component_files_auto_role_and_backup(tmp_path: Path, monkeypatch) -> None:
    import gui

    app_dir = tmp_path / "ImportCompApp"
    app_dir.mkdir()
    monkeypatch.setattr(gui, "BASE_DIR", app_dir)
    monkeypatch.setattr(gui, "LAUNCHER_SETTINGS_PATH", app_dir / "data" / "launcher_settings.json")
    monkeypatch.setattr(gui, "MODS_STATE_PATH", app_dir / "data" / "mods_state.json")
    monkeypatch.setattr(gui, "PORTWATCH_SNAPSHOT", app_dir / "data" / "portwatch_snapshot.json")
    monkeypatch.setattr(gui, "PC_RACE_SNAPSHOT", app_dir / "data" / "pc_race_snapshot.json")

    ctrl = gui.TrackerController(gui.load_config(CONFIG_PATH), skip_warmup=True)

    # Create local sample files outside downloads_dir (originals must remain untouched)
    src_dir = tmp_path / "UserFiles"
    src_dir.mkdir()
    sym_file = src_dir / "symbols.toml"
    sym_file.write_text("entry = 0x140001000\n", encoding="utf-8")
    xvd_file = src_dir / "xvdtool_v2.zip"
    xvd_file.write_bytes(b"PK\x03\x04FAKE_XVDTOOL_ZIP")
    shader_dir = src_dir / "rdna2_shaders_pack"
    shader_dir.mkdir()
    (shader_dir / "bridge.py").write_text("print('ok')\n", encoding="utf-8")

    # 1. Auto-detect roles on multi-file import (file + folder)
    res = ctrl.import_component_files(paths=[str(sym_file), str(xvd_file), str(shader_dir)], role="")
    assert res["ok"] is True
    assert res["count"] == 3
    roles_imported = {x["name"]: x["role"] for x in res["imported"]}
    assert roles_imported["symbols.toml"] == "4. Symboles / Config"
    assert roles_imported["xvdtool_v2.zip"] == "7. Dumper / ELF-SPRX"
    assert roles_imported["rdna2_shaders_pack"] == "6. Shaders / GPU"
    # Originals untouched
    assert sym_file.exists() and xvd_file.exists() and (shader_dir / "bridge.py").exists()

    # 2. Roles show up as installed in cockpit roles_table
    state = ctrl.get_cockpit_state()
    by_role = {r["role"]: r for r in state["roles_table"]}
    assert by_role["4. Symboles / Config"]["missing"] is False
    assert by_role["7. Dumper / ELF-SPRX"]["missing"] is False
    assert by_role["6. Shaders / GPU"]["missing"] is False

    # 3. Importing a newer file for the same role backs up the previous file into _backup (N-1 rollback)
    sym_v2 = src_dir / "symbols_v2.toml"
    sym_v2.write_text("entry = 0x140002000\n", encoding="utf-8")
    res2 = ctrl.import_component_files(paths=[str(sym_v2)], role="4. Symboles / Config")
    assert res2["ok"] is True
    state2 = ctrl.get_cockpit_state()
    row4 = next(r for r in state2["roles_table"] if r["role"] == "4. Symboles / Config")
    assert row4["has_backup"] is True
