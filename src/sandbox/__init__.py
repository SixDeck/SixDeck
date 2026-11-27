"""Module de téléchargement sécurisé (VM locale et quarantaine)."""

from src.sandbox.vm_downloader import (
    DriveInfo,
    DownloadReport,
    detect_available_backends,
    generate_inspection_sandbox_wsb,
    generate_windows_sandbox_files,
    get_public_ip,
    list_local_drives,
    list_quarantine_files,
    quarantine_download,
    read_quarantine_file_safely,
    run_safe_download_cli,
)

__all__ = [
    "DriveInfo",
    "DownloadReport",
    "detect_available_backends",
    "generate_inspection_sandbox_wsb",
    "generate_windows_sandbox_files",
    "get_public_ip",
    "list_local_drives",
    "list_quarantine_files",
    "quarantine_download",
    "read_quarantine_file_safely",
    "run_safe_download_cli",
]
