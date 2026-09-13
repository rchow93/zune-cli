"""Shim: imports everything from zune-cli.py (with hyphen).

Python cannot import files with hyphens directly.  This shim
re-exports the entire zune-cli module so `from zune_cli import ...` works.
"""

import importlib.util
import sys
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "zune_cli", str(Path(__file__).resolve().parent / "zune-cli.py"),
)
_zune_cli = importlib.util.module_from_spec(_spec)
sys.modules["zune_cli"] = _zune_cli
_spec.loader.exec_module(_zune_cli)

# Re-export everything the module defines
_mtp_str_d_names = {
    "_u16", "_u32", "_le16", "_le32", "_mtp_str_e", "_mtp_str_d",
    "_parse_jpeg_sof", "_pixel_dimensions", "_encode_obj_info",
    "connect", "connect_auth", "_parse_device", "find_device",
    "find_ffmpeg", "convert_audio", "convert_video",
    "expand_path", "sync_audio", "sync_photos", "sync_video",
    "sync_playlist", "create_abstract_album", "sync_album",
    "push_cover_art", "verify_metadata", "fix_metadata", "transcribe",
    "delete_objects", "list_files",
}
for _name in dir(_zune_cli):
    if not _name.startswith("_") or _name in _mtp_str_d_names:
        globals()[_name] = getattr(_zune_cli, _name)
