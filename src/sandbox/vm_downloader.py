from __future__ import annotations

import hashlib
import os
import re
import shutil
import string
import subprocess
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from src.storage import Storage

console = Console()

IP_CHECK_URLS = (
    "https://api4.ipify.org?format=json",
    "https://ipv4.icanhazip.com",
    "https://api.ipify.org?format=json",
)


async def get_public_ip(client: httpx.AsyncClient | None = None, timeout: float = 6.0) -> str:
    """Récupère l'adresse IP publique courante."""
    async with httpx.AsyncClient(
        follow_redirects=True,
        limits=httpx.Limits(max_keepalive_connections=0),
        headers={"Connection": "close", "User-Agent": "gta6-port-tracker/3.5"},
    ) as fresh_client:
        for url in IP_CHECK_URLS:
            try:
                resp = await fresh_client.get(url, timeout=timeout)
                if resp.status_code == 200:
                    text = resp.text.strip()
                    if text.startswith("{"):
                        return str(resp.json().get("ip", "")).strip()
                    return text.splitlines()[0].strip()
            except Exception:
                continue
    return ""

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
GITHUB_BLOB_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)$", re.IGNORECASE
)
GITHUB_REPO_ROOT_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", re.IGNORECASE
)


def resolve_download_url(url: str) -> str:
    """Convertit les pages web GitHub en vrais liens de téléchargement (raw ou archive .zip)."""
    cleaned = url.strip()
    m_blob = GITHUB_BLOB_RE.match(cleaned)
    if m_blob:
        owner, repo, ref, fpath = m_blob.groups()
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{fpath}"
    m_repo = GITHUB_REPO_ROOT_RE.match(cleaned)
    if m_repo:
        owner, repo = m_repo.groups()
        return f"https://github.com/{owner}/{repo}/archive/HEAD.zip"
    return cleaned

MAGIC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"MZ", "PE/Windows Executable (EXE/DLL)"),
    (b"\x7fELF", "ELF Binary (Linux/PS4/PS5)"),
    (b"PK\x03\x04", "ZIP Archive"),
    (b"7z\xbc\xaf\x27\x1c", "7-Zip Archive"),
    (b"\x1f\x8b", "GZIP / tar.gz Archive"),
    (b"Rar!\x1a\x07", "RAR Archive"),
    (b"RPF", "Rockstar RPF Archive"),
]


@dataclass
class DriveInfo:
    letter: str
    root: str
    total_gb: float
    free_gb: float


@dataclass
class DownloadReport:
    url: str
    backend: str
    target_dir: Path
    saved_path: Path | None = None
    wsb_path: Path | None = None
    size_bytes: int = 0
    sha256: str = ""
    detected_magic: str = "Inconnu / Texte"
    suspicious_mismatch: bool = False
    defender_status: str = "Non exécuté"
    virustotal_status: str = "Non configuré"
    ip_used: str = ""
    notes: str = ""


def list_local_drives() -> list[DriveInfo]:
    """Liste tous les disques locaux disponibles avec leur espace libre en Go."""
    drives: list[DriveInfo] = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                try:
                    usage = shutil.disk_usage(root)
                    drives.append(
                        DriveInfo(
                            letter=letter,
                            root=root,
                            total_gb=round(usage.total / (1024**3), 1),
                            free_gb=round(usage.free / (1024**3), 1),
                        )
                    )
                except OSError:
                    continue
    else:
        usage = shutil.disk_usage("/")
        drives.append(
            DriveInfo(
                letter="/",
                root="/",
                total_gb=round(usage.total / (1024**3), 1),
                free_gb=round(usage.free / (1024**3), 1),
            )
        )
    return drives


def detect_available_backends() -> dict[str, str | None]:
    """Détecte les moteurs de VM / bac à sable locaux disponibles sur la machine."""
    wsb_path = Path(r"C:\Windows\System32\WindowsSandbox.exe")
    sandboxie_candidates = (
        Path(r"C:\Program Files\Sandboxie-Plus\Start.exe"),
        Path(r"C:\Program Files\Sandboxie\Start.exe"),
    )
    sandboxie_exe = next((str(p) for p in sandboxie_candidates if p.exists()), None)
    vbox_exe = shutil.which("VBoxManage") or (
        r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"
        if Path(r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe").exists()
        else None
    )

    wsl_installed = False
    if shutil.which("wsl"):
        try:
            proc = subprocess.run(
                ["wsl", "--list", "--quiet"],
                capture_output=True,
                timeout=4,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = proc.stdout.decode("utf-16-le", errors="ignore").strip()
            wsl_installed = proc.returncode == 0 and bool(out)
        except Exception:
            wsl_installed = False

    return {
        "windows_sandbox": str(wsb_path) if wsb_path.exists() else None,
        "sandboxie": sandboxie_exe,
        "virtualbox": vbox_exe,
        "docker": shutil.which("docker"),
        "wsl": shutil.which("wsl") if wsl_installed else None,
        "quarantine": "built-in (httpx + SHA-256 + magic-bytes + Defender)",
    }


def _safe_filename_from_url(url: str) -> str:
    path = unquote(urlparse(url).path or "")
    base = path.rstrip("/").split("/")[-1] if path.rstrip("/") else "download.bin"
    cleaned = SAFE_FILENAME_RE.sub("_", base).strip("._")
    return (cleaned or "download.bin")[:100]


def _detect_magic(header: bytes) -> str:
    for sig, label in MAGIC_SIGNATURES:
        if header.startswith(sig):
            return label
    return "Texte / Format inconnu"


def _run_windows_defender_scan(filepath: Path) -> str:
    """Lance un scan ciblé Windows Defender sur un fichier en quarantaine."""
    candidates = [
        Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe"),
        Path(
            os.environ.get("ProgramData", r"C:\ProgramData")
        )
        / "Microsoft"
        / "Windows Defender"
        / "Platform",
    ]
    mpcmd: Path | None = None
    if candidates[0].exists():
        mpcmd = candidates[0]
    elif candidates[1].exists():
        found = sorted(candidates[1].glob("*/MpCmdRun.exe"), reverse=True)
        if found:
            mpcmd = found[0]

    if mpcmd is None:
        return "MpCmdRun.exe introuvable (scan manuel recommandé)"

    try:
        proc = subprocess.run(
            [str(mpcmd), "-Scan", "-ScanType", "3", "-File", str(filepath), "-DisableRemediation"],
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode == 0:
            return "PROPRE (Windows Defender : aucune menace détectée)"
        if proc.returncode == 2:
            return "ALERTE : MENACE DÉTECTÉE par Windows Defender !"
        return f"Code retour Defender {proc.returncode}"
    except Exception as exc:
        return f"Scan Defender non terminé ({exc})"


def generate_windows_sandbox_files(
    url: str,
    target_dir: Path,
) -> tuple[Path, Path]:
    """Génère une VM locale Windows Sandbox (.wsb) durcie + script de téléchargement isolé.

    La VM ne partage ni le presse-papiers, ni les imprimantes, ni l'audio, ni le vGPU.
    Seul le dossier de quarantaine choisi sur le disque cible est monté.
    """
    incoming_dir = target_dir / "vm_incoming"
    scripts_dir = target_dir / "vm_scripts"
    incoming_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.mkdir(parents=True, exist_ok=True)

    filename = _safe_filename_from_url(url)
    ps_script_path = scripts_dir / "vm_fetch.ps1"
    wsb_path = target_dir / "gta6_safe_download.wsb"

    # Script exécuté À L'INTÉRIEUR de la VM Windows Sandbox éphémère
    ps_content = f"""$ErrorActionPreference = 'Stop'
$url = '{url.replace("'", "''")}'
$outDir = 'C:\\Users\\WDAGUtilityAccount\\Desktop\\Incoming'
$outFile = Join-Path $outDir '{filename}.quarantine'

Write-Host '=== GTA 6 Safe VM Downloader (Windows Sandbox) ===' -ForegroundColor Cyan
Write-Host "URL    : $url"
Write-Host "Sortie : $outFile"

Invoke-WebRequest -Uri $url -OutFile $outFile -UseBasicParsing -MaximumRedirection 5

$hash = (Get-FileHash -Path $outFile -Algorithm SHA256).Hash
$size = (Get-Item $outFile).Length
Write-Host "SHA256 : $hash" -ForegroundColor Green
Write-Host "Taille : $size octets"

$report = @(
    "URL=$url"
    "FILE=$outFile"
    "SIZE=$size"
    "SHA256=$hash"
    "DATE=$(Get-Date -Format o)"
)
$report | Set-Content -Path (Join-Path $outDir '{filename}.report.txt') -Encoding UTF8
Write-Host 'Téléchargement terminé en toute sécurité dans la VM. Vous pouvez fermer Windows Sandbox.' -ForegroundColor Yellow
Pause
"""
    ps_script_path.write_text(ps_content, encoding="utf-8")

    # Configuration XML Windows Sandbox (.wsb) durcie
    wsb_content = f"""<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>Enable</Networking>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <ProtectedClient>Enable</ProtectedClient>
  <PrinterRedirection>Disable</PrinterRedirection>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MemoryInMB>4096</MemoryInMB>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>{incoming_dir}</HostFolder>
      <SandboxFolder>C:\\Users\\WDAGUtilityAccount\\Desktop\\Incoming</SandboxFolder>
      <ReadOnly>false</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>{scripts_dir}</HostFolder>
      <SandboxFolder>C:\\Users\\WDAGUtilityAccount\\Desktop\\Scripts</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>powershell.exe -ExecutionPolicy Bypass -File C:\\Users\\WDAGUtilityAccount\\Desktop\\Scripts\\vm_fetch.ps1</Command>
  </LogonCommand>
</Configuration>
"""
    wsb_path.write_text(wsb_content, encoding="utf-8")
    return wsb_path, ps_script_path


async def check_virustotal_hash(
    sha256: str,
    api_key: str | None,
    client: httpx.AsyncClient,
) -> str:
    """Interroge VirusTotal uniquement par hash SHA-256 (jamais d'upload du fichier)."""
    if not api_key or not sha256:
        return "Non configuré (ajoutez VIRUSTOTAL_API_KEY dans .env)"
    try:
        resp = await client.get(
            f"https://www.virustotal.com/api/v3/files/{sha256}",
            headers={"x-apikey": api_key, "Accept": "application/json"},
            timeout=10.0,
        )
        if resp.status_code == 404:
            return "Inconnu de VirusTotal (hash jamais scanné)"
        if resp.status_code != 200:
            return f"Erreur API VirusTotal (HTTP {resp.status_code})"
        stats = (
            (resp.json().get("data") or {})
            .get("attributes", {})
            .get("last_analysis_stats", {})
        )
        mal = int(stats.get("malicious", 0))
        susp = int(stats.get("suspicious", 0))
        harmless = int(stats.get("harmless", 0)) + int(stats.get("undetected", 0))
        if mal > 0:
            return f"ALERTE : {mal} détection(s) malveillante(s), {susp} suspect(s) / {mal + susp + harmless} moteurs"
        if susp > 0:
            return f"PRUDENCE : {susp} moteur(s) suspect(s), 0 malveillant"
        return f"PROPRE : 0 menace sur {harmless} moteurs VirusTotal"
    except Exception as exc:
        return f"Erreur VirusTotal ({exc})"


async def quarantine_download(
    url: str,
    target_dir: Path,
    client: httpx.AsyncClient,
    *,
    max_size_mb: int = 512,
    run_defender: bool = True,
    virustotal_api_key: str | None = None,
) -> DownloadReport:
    """Télécharge en quarantaine stricte (.quarantine, jamais exécuté) avec SHA-256 + magic bytes + Defender + VirusTotal."""
    quarantine_dir = target_dir / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    download_url = resolve_download_url(url)
    filename = _safe_filename_from_url(download_url)
    if filename == "HEAD.zip":
        m_repo = GITHUB_REPO_ROOT_RE.match(url.strip())
        if m_repo:
            filename = f"{_safe_filename_from_url(m_repo.group(2))}.zip"
    ts = int(time.time())
    dest_path = quarantine_dir / f"{ts}_{filename}.quarantine"

    max_bytes = max_size_mb * 1024 * 1024
    hasher = hashlib.sha256()
    header_bytes = b""
    total_bytes = 0
    ip_used = await get_public_ip(client)

    async with client.stream(
        "GET",
        download_url,
        follow_redirects=True,
        timeout=60.0,
        headers={
            "User-Agent": "gta6-port-tracker/3.5 (quarantine-downloader)",
            "Connection": "close",
        },
    ) as resp:
        resp.raise_for_status()
        with dest_path.open("wb") as fh:
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                if not chunk:
                    continue
                if len(header_bytes) < 16:
                    header_bytes += chunk[: 16 - len(header_bytes)]
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    fh.close()
                    dest_path.unlink(missing_ok=True)
                    raise ValueError(
                        f"Fichier trop volumineux (> {max_size_mb} Mo) — téléchargement annulé par sécurité."
                    )
                hasher.update(chunk)
                fh.write(chunk)

    magic = _detect_magic(header_bytes)
    lower_name = filename.lower()
    suspicious_mismatch = (
        magic.startswith("PE/Windows Executable")
        and not lower_name.endswith((".exe", ".dll"))
    )

    defender_status = (
        _run_windows_defender_scan(dest_path) if run_defender and os.name == "nt" else "Ignoré"
    )
    sha256_hex = hasher.hexdigest()
    vt_status = await check_virustotal_hash(sha256_hex, virustotal_api_key, client)

    return DownloadReport(
        url=url,
        backend="quarantine",
        target_dir=target_dir,
        saved_path=dest_path,
        size_bytes=total_bytes,
        sha256=sha256_hex,
        detected_magic=magic,
        suspicious_mismatch=suspicious_mismatch,
        defender_status=defender_status,
        virustotal_status=vt_status,
        ip_used=ip_used,
        notes="Extension .quarantine ajoutée : impossible d'exécuter accidentellement par double-clic.",
    )


def generate_inspection_sandbox_wsb(target_dir: Path) -> Path:
    """Génère une VM Windows Sandbox (.wsb) dédiée à l'inspection en lecture seule des fichiers en quarantaine."""
    quarantine_dir = target_dir / "quarantine"
    incoming_dir = target_dir / "vm_incoming"
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    incoming_dir.mkdir(parents=True, exist_ok=True)

    wsb_path = target_dir / "gta6_inspect_quarantine.wsb"
    wsb_content = f"""<Configuration>
  <VGpu>Disable</VGpu>
  <Networking>Disable</Networking>
  <AudioInput>Disable</AudioInput>
  <VideoInput>Disable</VideoInput>
  <ProtectedClient>Enable</ProtectedClient>
  <PrinterRedirection>Disable</PrinterRedirection>
  <ClipboardRedirection>Disable</ClipboardRedirection>
  <MemoryInMB>4096</MemoryInMB>
  <MappedFolders>
    <MappedFolder>
      <HostFolder>{quarantine_dir}</HostFolder>
      <SandboxFolder>C:\\Users\\WDAGUtilityAccount\\Desktop\\Quarantine_RO</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
    <MappedFolder>
      <HostFolder>{incoming_dir}</HostFolder>
      <SandboxFolder>C:\\Users\\WDAGUtilityAccount\\Desktop\\VM_Incoming_RO</SandboxFolder>
      <ReadOnly>true</ReadOnly>
    </MappedFolder>
  </MappedFolders>
  <LogonCommand>
    <Command>explorer.exe C:\\Users\\WDAGUtilityAccount\\Desktop\\Quarantine_RO</Command>
  </LogonCommand>
</Configuration>
"""
    wsb_path.write_text(wsb_content, encoding="utf-8")
    return wsb_path


def list_quarantine_files(target_dir: Path) -> list[dict]:
    """Liste tous les fichiers présents en quarantaine et dans vm_incoming sur le disque choisi."""
    entries: list[dict] = []
    for folder_name in ("quarantine", "vm_incoming"):
        folder = target_dir / folder_name
        if not folder.exists():
            continue
        for fp in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if not fp.is_file():
                continue
            stat = fp.stat()
            head = b""
            try:
                with fp.open("rb") as fh:
                    head = fh.read(16)
            except OSError:
                pass
            entries.append(
                {
                    "folder": folder_name,
                    "name": fp.name,
                    "path": str(fp),
                    "size_bytes": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "magic": _detect_magic(head),
                }
            )
    return entries


def read_quarantine_file_safely(target_dir: Path, folder_name: str, filename: str) -> dict:
    """Lit un fichier en quarantaine SANS l'exécuter (texte, listing ZIP ou dump hexadécimal)."""
    if folder_name not in {"quarantine", "vm_incoming"}:
        return {"ok": False, "error": "Dossier non autorisé."}
    # Empêcher toute traversée de chemin
    safe_name = Path(filename).name
    fp = (target_dir / folder_name / safe_name).resolve()
    allowed_root = (target_dir / folder_name).resolve()
    if allowed_root not in fp.parents or not fp.exists() or not fp.is_file():
        return {"ok": False, "error": "Fichier introuvable en quarantaine."}

    stat = fp.stat()
    hasher = hashlib.sha256()
    with fp.open("rb") as fh:
        head = fh.read(32768)
        hasher.update(head)
        while chunk := fh.read(65536):
            hasher.update(chunk)

    magic = _detect_magic(head[:16])
    sha256 = hasher.hexdigest()

    # 1. Si c'est une archive ZIP -> lister le contenu sans extraire
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(fp, "r") as zf:
                lines = [
                    f"{info.filename}  ({info.file_size:,} octets)"
                    for info in zf.infolist()[:200]
                ]
            return {
                "ok": True,
                "name": safe_name,
                "size_bytes": stat.st_size,
                "sha256": sha256,
                "magic": magic,
                "mode": "zip_listing",
                "content": f"=== Contenu de l'archive ZIP ({len(lines)} fichiers, sans extraction) ===\n"
                + "\n".join(lines),
            }
        except Exception:
            pass

    # 2. Si c'est du texte lisible (pas d'octets nuls dans les premiers 4 Ko)
    sample = head[:4096]
    if b"\x00" not in sample:
        try:
            text_preview = head.decode("utf-8", errors="replace")
            return {
                "ok": True,
                "name": safe_name,
                "size_bytes": stat.st_size,
                "sha256": sha256,
                "magic": magic,
                "mode": "text",
                "content": text_preview,
            }
        except Exception:
            pass

    # 3. Binaire -> Dump Hex + ASCII des premiers 1024 octets
    dump_lines: list[str] = [f"=== Aperçu binaire Hex/ASCII (premiers {min(len(head), 1024)} octets) ==="]
    raw = head[:1024]
    for offset in range(0, len(raw), 16):
        chunk = raw[offset : offset + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        dump_lines.append(f"{offset:08X}  {hex_part:<47}  |{ascii_part}|")

    return {
        "ok": True,
        "name": safe_name,
        "size_bytes": stat.st_size,
        "sha256": sha256,
        "magic": magic,
        "mode": "hexdump",
        "content": "\n".join(dump_lines),
    }


def delete_quarantine_file(target_dir: Path, folder_name: str, filename: str) -> dict:
    """Supprime proprement un fichier en quarantaine."""
    if folder_name not in {"quarantine", "vm_incoming"}:
        return {"ok": False, "error": "Dossier non autorisé."}
    safe_name = Path(filename).name
    fp = (target_dir / folder_name / safe_name).resolve()
    allowed_root = (target_dir / folder_name).resolve()
    if allowed_root not in fp.parents or not fp.exists():
        return {"ok": False, "error": "Fichier introuvable."}
    fp.unlink()
    return {"ok": True, "deleted": safe_name}


def _run_docker_isolated_download(url: str, target_dir: Path) -> DownloadReport:
    """Télécharge le fichier via un conteneur Docker isolé (--cap-drop=ALL, read-only root)."""
    out_dir = target_dir / "vm_incoming"
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_filename_from_url(url)
    dest_name = f"{filename}.quarantine"

    cmd = [
        "docker",
        "run",
        "--rm",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "-v",
        f"{out_dir}:/downloads",
        "curlimages/curl:latest",
        "-L",
        "--max-filesize",
        "536870912",
        "-o",
        f"/downloads/{dest_name}",
        url,
    ]
    subprocess.run(cmd, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    dest_path = out_dir / dest_name
    data_head = dest_path.read_bytes()[:16] if dest_path.exists() else b""
    sha256 = hashlib.sha256(dest_path.read_bytes()).hexdigest() if dest_path.exists() else ""
    size_bytes = dest_path.stat().st_size if dest_path.exists() else 0

    return DownloadReport(
        url=url,
        backend="docker",
        target_dir=target_dir,
        saved_path=dest_path,
        size_bytes=size_bytes,
        sha256=sha256,
        detected_magic=_detect_magic(data_head),
        defender_status=_run_windows_defender_scan(dest_path) if os.name == "nt" else "N/A",
        notes="Téléchargé depuis un conteneur Docker isolé (--cap-drop=ALL, rootfs read-only).",
    )


async def run_safe_download_cli(
    storage: Storage,
    client: httpx.AsyncClient,
    *,
    url: str | None = None,
    drive: str | None = None,
    vm_mode: str = "auto",
) -> int:
    """Interface interactive Rich pour choisir le disque, la VM locale et le fichier à télécharger."""
    drives = list_local_drives()
    backends = detect_available_backends()

    # 1. Afficher les disques disponibles et choisir le disque cible
    drive_table = Table(title="Disques locaux disponibles", expand=False)
    drive_table.add_column("Disque", style="bold cyan")
    drive_table.add_column("Racine")
    drive_table.add_column("Espace libre", justify="right", style="bold green")
    drive_table.add_column("Total", justify="right")

    for d in drives:
        drive_table.add_row(d.letter, d.root, f"{d.free_gb} Go", f"{d.total_gb} Go")
    console.print(drive_table)

    valid_letters = {d.letter.upper(): d for d in drives}
    default_drive = max(drives, key=lambda d: d.free_gb).letter if drives else "C"

    if drive:
        chosen_key = drive.strip().rstrip(":\\/").upper()
        if chosen_key not in valid_letters:
            console.print(f"[red]Disque '{drive}' introuvable. Disques disponibles : {', '.join(valid_letters)}[/red]")
            return 1
    else:
        chosen_key = Prompt.ask(
            "Sur quel disque enregistrer la VM / les téléchargements sécurisés ?",
            choices=list(valid_letters.keys()),
            default=default_drive,
        )

    chosen_drive = valid_letters[chosen_key]
    target_dir = Path(chosen_drive.root) / "GTA6_Sandbox_Downloads"
    target_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]Dossier isolé configuré sur le disque {chosen_drive.letter}:[/green] {target_dir}")

    # 2. Choisir l'URL (depuis SQLite ou saisie libre)
    selected_url = url
    if not selected_url:
        dl_rows = storage.get_direct_links(limit=15)
        if dl_rows:
            ltable = Table(title="Liens directs détectés en base SQLite", expand=True)
            ltable.add_column("#", width=3)
            ltable.add_column("Fichier")
            ltable.add_column("Statut", width=8)
            ltable.add_column("Taille", width=10)
            ltable.add_column("URL")
            for idx, (dl, _title, _item_url, _score) in enumerate(dl_rows, start=1):
                st = "[green]200[/green]" if dl.is_alive else "[dim]N/A[/dim]"
                ltable.add_row(str(idx), dl.filename, st, dl.human_size or "—", dl.url)
            console.print(ltable)
            choice = Prompt.ask(
                "Entrez le numéro d'un lien ci-dessus ou collez directement une URL"
            ).strip()
            if choice.isdigit() and 1 <= int(choice) <= len(dl_rows):
                selected_url = dl_rows[int(choice) - 1][0].url
            else:
                selected_url = choice
        else:
            selected_url = Prompt.ask("Collez l'URL directe du fichier à télécharger en sécurité").strip()

    if not selected_url or not selected_url.startswith(("http://", "https://")):
        console.print("[red]URL invalide.[/red]")
        return 1

    # 3. Choisir le mode VM / Isolation
    console.print(
        Panel.fit(
            f"[bold]Moteurs d'isolation détectés :[/bold]\n"
            f"- Windows Sandbox (VM locale éphémère .wsb) : "
            f"{'[green]Prêt[/green]' if backends['windows_sandbox'] else '[yellow]Non activé (config .wsb générable)[/yellow]'}\n"
            f"- Docker (conteneur isolé) : "
            f"{'[green]Prêt[/green]' if backends['docker'] else '[dim]Non installé[/dim]'}\n"
            f"- WSL2 (VM Linux) : "
            f"{'[green]Prêt[/green]' if backends['wsl'] else '[dim]Non installé[/dim]'}\n"
            f"- Quarantine Safe Mode (sans exécution + SHA-256 + Defender) : [green]Prêt[/green]",
            border_style="cyan",
        )
    )

    mode = vm_mode
    if mode == "auto":
        if backends["windows_sandbox"]:
            mode = "windows_sandbox"
        elif backends["docker"]:
            mode = "docker"
        else:
            mode = "wsb_and_quarantine"

    # Générer systématiquement la config VM Windows Sandbox (.wsb) sur le disque choisi
    wsb_path, ps_path = generate_windows_sandbox_files(selected_url, target_dir)
    console.print(f"[cyan]Configuration VM locale Windows Sandbox générée :[/cyan] {wsb_path}")

    if mode == "windows_sandbox":
        if backends["windows_sandbox"]:
            console.print("[bold green]Lancement de la VM locale Windows Sandbox...[/bold green]")
            subprocess.Popen([backends["windows_sandbox"], str(wsb_path)])
            return 0
        console.print(
            "[yellow]Windows Sandbox n'est pas encore activé sur ce Windows.[/yellow]\n"
            "Pour l'activer (PowerShell Administrateur, puis redémarrer) :\n"
            "[bold]Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All[/bold]\n"
            f"Ensuite double-cliquez simplement sur : [bold]{wsb_path}[/bold]"
        )
        return 0

    if mode == "docker" and backends["docker"]:
        report = _run_docker_isolated_download(selected_url, target_dir)
    else:
        if mode == "wsb_and_quarantine":
            console.print(
                "[yellow]Aucun hyperviseur actif (Windows Sandbox / Docker / WSL2).[/yellow]\n"
                f"La VM [bold]{wsb_path.name}[/bold] est prête dès que vous activez Windows Sandbox.\n"
                "Téléchargement sécurisé en mode [bold]Quarantaine (.quarantine + SHA-256 + Defender)[/bold]..."
            )
        report = await quarantine_download(selected_url, target_dir, client)
        report.wsb_path = wsb_path

    # 4. Afficher le verdict sécurité
    verdict_color = "red" if (report.suspicious_mismatch or "MENACE" in report.defender_status) else "green"
    console.print(
        Panel(
            f"[bold]Fichier en quarantaine :[/bold] {report.saved_path}\n"
            f"[bold]Taille :[/bold] {report.size_bytes:,} octets\n"
            f"[bold]SHA-256 :[/bold] {report.sha256}\n"
            f"[bold]Type réel (magic bytes) :[/bold] {report.detected_magic}\n"
            f"[bold]Discordance suspecte :[/bold] {'[bold red]OUI — EXÉCUTABLE DÉGUISÉ ![/bold red]' if report.suspicious_mismatch else '[green]Non[/green]'}\n"
            f"[bold]Windows Defender :[/bold] {report.defender_status}\n"
            f"[bold]Config VM (.wsb) :[/bold] {wsb_path}\n"
            f"[dim]{report.notes}[/dim]",
            title="Rapport de Téléchargement Sécurisé",
            border_style=verdict_color,
        )
    )

    if report.suspicious_mismatch and report.saved_path and report.saved_path.exists():
        if Confirm.ask("Exécutable déguisé détecté ! Supprimer immédiatement ce fichier ?", default=True):
            report.saved_path.unlink(missing_ok=True)
            console.print("[bold red]Fichier suspect supprimé.[/bold red]")

    return 0
