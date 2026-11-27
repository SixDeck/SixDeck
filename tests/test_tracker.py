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
