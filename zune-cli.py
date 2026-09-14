#!/usr/bin/env python3
"""zune-cli — sync music/videos/playlists to a Microsoft Zune over USB MTP (no Windows VM).

The Zune uses the WMDRM (MTPZ) variant of MTP: after opening a session you must
complete a RSA + AES-CMAC authentication handshake before the device will serve
storage/object operations.  The wire protocol here (little-endian MTP containers,
the two-bulk-write DATA phase, and the MTPZ handshake) is the exact layer that the
Zune actually speaks; it was verified live against a first-gen Zune 30.

Commands:
  list                 list music and video on the device
  push --audio PATH    convert+push audio
  push --video PATH    convert+push video
  playlist NAME TRACKS create a .pla playlist
  eject                close sessions
"""

import argparse
import datetime
import hashlib
import os
import re
import secrets
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import usb.core
    import usb.util
    from Crypto.Cipher import AES as _AES
except ImportError:
    sys.exit("Error: pyusb + pycryptodome not installed. Run:  pip install pyusb pycryptodome")

# ── Helpers ───────────────────────────────────────────────────────────────────

def _u16(v): return struct.pack(">H", v)
def _u32(v): return struct.pack(">I", v)
def _le32(v): return struct.pack("<I", v)
def _u64(v): return struct.pack(">Q", v)

def _u16b(data, off):
    v, = struct.unpack_from(">H", data, off)
    return v, off + 2

def _u32b(data, off):
    v, = struct.unpack_from(">I", data, off)
    return v, off + 4

def _u64b(data, off):
    v, = struct.unpack_from(">Q", data, off)
    return v, off + 8


def _mtp_str_e(s):
    """Encode an MTP string: u8 char count (incl null) + UTF-16LE."""
    return struct.pack("<B", len(s) + 1) + (s + "\0").encode("utf-16-le")


def _mtp_str_d(data, off):
    """Decode an MTP string, returning (string, new_offset)."""
    n = data[off]
    if n == 0:
        return "", off + 1
    raw = data[off + 1:off + 1 + n * 2]
    s = raw.decode("utf-16-le")
    if s.endswith("\0"):
        s = s[:-1]
    return s, off + 1 + n * 2


# ── Constants ──────────────────────────────────────────────────────────────────

FMT = {"MP3": 0x3009, "WMA": 0xB901, "AAC": 0xB903,
       "WMV": 0xB981, "MP4": 0xB982, "JPEG": 0x3801, "CoverArt": 0x3802,
       "Assoc": 0x3001, "AbstractAlbum": 0xBA03, "Playlist": 0xBA05}

EXT_FMT = {
    ".mp3": FMT["MP3"], ".wma": FMT["WMA"], ".aac": FMT["AAC"], ".m4a": FMT["AAC"],
    ".mp4": FMT["MP4"], ".m4v": FMT["MP4"], ".wmv": FMT["WMV"],
    ".avi": None, ".mov": None, ".mkv": None,
    ".jpg": FMT["JPEG"], ".jpeg": FMT["JPEG"],
}

ZUNE_PIDS = {0x063E: "Zune HD", 0x0710: "Zune 30"}

# Standard little-endian MTP opcodes (the Zune speaks these)
OP = {
    "GetDeviceInfo": 0x1001, "OpenSession": 0x1002, "CloseSession": 0x1003,
    "GetStorageIDs": 0x1004, "GetObjectHandles": 0x1007, "GetObjectInfo": 0x1008,
    "DeleteObject": 0x100B, "SendObjectInfo": 0x100C, "SendObject": 0x100D,
    "SendWMDRMPDReq": 0x9212, "GetWMDRMPDResp": 0x9213,
    "EnableTrustedOps": 0x9214, "EndTrustedAppSession": 0x9216,
}
C_OK = 0x2001
PROP_NAME = 0xDC44
PROP_OBJECTREFS = 0xDC41
PROP_ARTIST = 0xDC46
PROP_ALBUM = 0xDC9A
PROP_TRACK = 0xDC8B

# ── MTPZ crypto (the libmtp-zune handshake, verified live on a Zune 30) ────────

def _mgf1_sha1(seed, length):
    out = b""
    for i in range((length + 19) // 20):
        out += hashlib.sha1(seed + struct.pack(">I", i)).digest()
    return out[:length]


def _rsa_priv(data, n, d):
    return pow(int.from_bytes(data, "big"), d, n).to_bytes(128, "big")


def _aes_cmac(key, data):
    def e(b):
        return _AES.new(key, _AES.MODE_ECB).encrypt(b)

    def dbl(x):
        hi = x[0] & 0x80
        o = bytearray(((int.from_bytes(x, "big") << 1) & ((1 << 128) - 1)).to_bytes(16, "big"))
        if hi:
            o[15] ^= 0x87
        return bytes(o)

    k1 = dbl(e(b"\x00" * 16))
    k2 = dbl(k1)
    n = max(1, (len(data) + 15) // 16)
    last = data[(n - 1) * 16:]
    if len(last) == 16:
        block = bytes(a ^ b for a, b in zip(last, k1))
    else:
        pad = last + b"\x80" + b"\x00" * (15 - len(last))
        block = bytes(a ^ b for a, b in zip(pad, k2))
    x = b"\x00" * 16
    for i in range(n - 1):
        x = e(bytes(a ^ b for a, b in zip(x, data[i * 16:(i + 1) * 16])))
    return e(bytes(a ^ b for a, b in zip(x, block)))


_MTPZ_LINE_NAMES = ("public exponent", "encryption key", "modulus", "private key", "certificates")
_MTPZ_HELP = "See step 4 (\"MTPZ key file\") in README.md for the one-line download command."


def _load_mtpz_keys():
    """MTPZ keys from the libmtp-format key file: five hex lines — public exponent, encryption
    key, modulus, private key, certificates. Uses $MTPZ_DATA if set; otherwise ~/.mtpz-data,
    then .mtpz-data in the folder this script lives in (the cloned repo)."""
    env = os.environ.get("MTPZ_DATA")
    candidates = ([Path(env).expanduser()] if env else
                  [Path.home() / ".mtpz-data", Path(__file__).resolve().parent / ".mtpz-data"])
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        looked = "\n".join(f"  - {p}" for p in candidates)
        note = "  ($MTPZ_DATA is set, so only that path is checked)\n" if env else ""
        sys.exit(f"MTPZ key file not found. Looked for:\n{looked}\n{note}{_MTPZ_HELP}")
    try:
        text = path.read_text(errors="replace")
    except OSError as e:
        sys.exit(f"Can't read MTPZ key file {path}: {e.strerror or e}\n{_MTPZ_HELP}")
    lines = [ln.strip() for ln in text.lstrip("\ufeff").splitlines() if ln.strip()]
    if lines and lines[0].startswith("<"):
        sys.exit(f"MTPZ key file {path} is a web page (HTML), not the key file.\n"
                 f"Delete it and download the raw file instead. {_MTPZ_HELP}")
    if len(lines) < 5:
        sys.exit(f"MTPZ key file {path} should have 5 lines of hex "
                 f"({', '.join(_MTPZ_LINE_NAMES)}) but has {len(lines)}.\n"
                 f"It's incomplete or damaged; re-download it. {_MTPZ_HELP}")

    def _hex(i):
        try:
            return bytes.fromhex(lines[i])
        except ValueError:
            sys.exit(f"MTPZ key file {path}: line {i + 1} ({_MTPZ_LINE_NAMES[i]}) isn't valid hex.\n"
                     f"The file is damaged; re-download it. {_MTPZ_HELP}")

    names = ("MTPZ_ENCRYPTION_KEY", "MTPZ_MODULUS", "MTPZ_PRIVATE_KEY", "MTPZ_CERTIFICATES")
    out = {n: _hex(i) for i, n in enumerate(names, 1)}
    out["MTPZ_PUBLIC_EXPONENT"] = lines[0]
    return out


def _build_app_cert_msg(certs, n, d):
    rand = secrets.token_bytes(16)
    pre = bytes([0x02, 0x01, 0x01, 0x00, 0x00]) + struct.pack(">H", len(certs)) \
        + certs + b"\x00\x10" + rand
    inner = hashlib.sha1(pre[2:]).digest()
    h = hashlib.sha1(b"\x00" * 8 + inner).digest()
    mask = _mgf1_sha1(h, 107)
    o = bytearray(128)
    o[106] = 0x01
    o[107:127] = h
    for i in range(107):
        o[i] ^= mask[i]
    o[0] &= 0x7F
    o[127] = 0xBC
    return pre + b"\x01\x00\x80" + _rsa_priv(bytes(o), n, d), rand


def _parse_dev_resp(resp, sent_rand, n, d):
    if not (resp[0] == 0x02 and resp[1] == 0x02 and resp[3] == 0x80):
        raise RuntimeError(f"bad device response header {list(resp[:4])}")
    dec = bytearray(_rsa_priv(resp[4:132], n, d))
    sm = _mgf1_sha1(bytes(dec[21:128]), 20)
    for i in range(20):
        dec[1 + i] ^= sm[i]
    dm = _mgf1_sha1(bytes(dec[1:21]), 107)
    for i in range(107):
        dec[21 + i] ^= dm[i]
    key = bytes(dec[112:128])
    blen = struct.unpack_from(">H", resp, 134)[0]
    if not blen or blen % 16:
        raise RuntimeError(f"bad AES block length {blen}")
    pt = _AES.new(key, _AES.MODE_CBC, iv=b"\x00" * 16).decrypt(resp[136:136 + blen])
    o = 1
    cl = int.from_bytes(pt[o:o + 4], "big")
    o += 4 + cl
    rl = struct.unpack_from(">H", pt, o)[0]
    o += 2
    if pt[o:o + rl] != sent_rand:
        raise RuntimeError("device echoed random mismatch")
    o += rl
    drl = struct.unpack_from(">H", pt, o)[0]
    o += 2 + drl
    o += 1
    sl = struct.unpack_from(">H", pt, o)[0]
    o += 2 + sl
    o += 1
    ml = struct.unpack_from(">H", pt, o)[0]
    o += 2
    return pt[o:o + ml], key


def _build_confirmation(mac_hash):
    return bytes([0x02, 0x03, 0x00, 0x10]) + _aes_cmac(mac_hash[:16], b"\x00" * 15 + b"\x01")


# ── USB transport: claim the MTP interface and un-stall its bulk endpoints ────

class Transport:
    ZUNE_VID = 0x045E

    def __init__(self, dev):
        self.dev = dev
        self.EP_OUT = None
        self.EP_IN = None
        self._setup()

    def _setup(self):
        try:
            if self.dev.is_kernel_driver_active(0):
                try:
                    self.dev.detach_kernel_driver(0)
                except Exception:
                    pass
        except usb.core.USBError:
            pass
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass
        cfg = self.dev.get_active_configuration()
        mtp_iface = None
        for intf in cfg:
            if (intf.bInterfaceClass == 6 and intf.bInterfaceSubClass == 1
                    and intf.bInterfaceProtocol == 1):
                mtp_iface = intf
                break
        if mtp_iface is None:
            # Some Zune firmwares report the MTP interface as class 0 (defined at
            # the endpoint level). Fall back to the interface whose endpoints match
            # the MTP topology: a bulk OUT, a bulk IN, and an interrupt IN.
            def _mtp_sig(intf):
                atts = [(ep.bmAttributes, ep.bEndpointAddress & 0x80) for ep in intf]
                return (any(a == 2 and not d for a, d in atts)
                        and any(a == 2 and d for a, d in atts)
                        and any(a == 3 and d for a, d in atts))
            mtp_iface = next((intf for intf in cfg if _mtp_sig(intf)), None)
        if mtp_iface is None:
            sys.exit("Error: MTP interface not found.")
        usb.util.claim_interface(self.dev, mtp_iface.bInterfaceNumber)
        self.EP_OUT = None
        self.EP_IN = None
        for ep in mtp_iface:
            a, t = ep.bEndpointAddress, ep.bmAttributes
            if t == 2 and not (a & 0x80):       # bulk OUT
                self.EP_OUT = a
            elif t == 2 and (a & 0x80):          # bulk IN
                self.EP_IN = a
        if self.EP_OUT is None or self.EP_IN is None:
            sys.exit("Error: MTP bulk endpoints not found.")
        # A stalled bulk endpoint (left by an interrupted session) makes the Zune
        # silently ignore new writes and never answer; clearing the halt unblocks it.
        for ep in (self.EP_OUT, self.EP_IN):
            try:
                self.dev.clear_halt(ep)
            except Exception:
                try:
                    self.dev.backend.clear_halt(self.dev._ctx.handle, ep)
                except Exception:
                    pass

    def close(self):
        try:
            usb.util.release_interface(self.dev, 0)
        except Exception:
            pass


# ── MTP container layer (little-endian, matches the device) ───────────────────

class MTP:
    CMD, DATA, RESP, EVT = 1, 2, 3, 4

    def __init__(self, dev, out_ep, in_ep):
        self.dev, self.out, self.in_ = dev, out_ep, in_ep
        self.tx = 0

    def build(self, typ, code, params=(), tx=None):
        b = b"".join(struct.pack("<I", p) for p in params)
        return struct.pack("<IHHI", 12 + len(b), typ, code,
                           self.tx if tx is None else tx) + b

    def write(self, data):
        self.dev.write(self.out, data, 10000)

    def read(self, n):
        # one transfer: returns whatever arrived (up to n bytes); callers loop
        return self.dev.read(self.in_, n, 30000)

    def send_cmd(self, opcode, params=()):
        self.tx += 1
        self.write(self.build(self.CMD, opcode, params))

    def send_data(self, opcode, data):
        # Zune quirk: DATA containers are two separate bulk writes (header, payload)
        self.write(struct.pack("<IHHI", 12 + len(data), self.DATA, opcode, self.tx))
        if data:  # empty objects (albums/playlists) are header-only
            self.write(data)

    def recv_data(self):
        head = self.read(512)
        total = struct.unpack_from("<I", head)[0]
        buf = head
        while len(buf) < total:
            buf += self.read(total - len(buf))
        typ, code = struct.unpack_from("<HH", buf, 4)
        return typ, code, bytes(buf[12:total])

    def recv_resp(self):
        typ, code, payload = self.recv_data()
        if typ != self.RESP:
            raise RuntimeError(f"expected RESPONSE, got type {typ:#06x}")
        if code != C_OK:
            raise RuntimeError(f"MTP error {code:#06x}")
        return payload

    def open_session(self):
        self.write(self.build(self.CMD, OP["OpenSession"], (1,), tx=0))
        self.tx = 0
        self.recv_resp()

    def close_session(self):
        self.send_cmd(OP["CloseSession"])
        try:
            self.recv_resp()
        except Exception:
            pass

    def set_device_prop(self, code, s):
        encoded = struct.pack("<B", len(s) + 1) + (s + "\0").encode("utf-16-le")
        self.send_cmd(0x1016, (code,))
        self.send_data(0x1016, encoded)
        self.recv_resp()

    def set_device_prop_value(self, handle, prop_code, data_type, data):
        self.send_cmd(0x1015, (handle, prop_code, data_type))
        self.send_data(0x1015, data)
        self.recv_resp()

    # MTPZ session
    def send_mtpz(self, data):
        self.send_cmd(OP["SendWMDRMPDReq"])
        self.send_data(OP["SendWMDRMPDReq"], data)
        self.recv_resp()

    def get_mtpz(self):
        self.send_cmd(OP["GetWMDRMPDResp"])
        typ, code, payload = self.recv_data()
        if typ != self.DATA:
            raise RuntimeError(f"getMtpz: got type {typ:#06x}")
        self.recv_resp()
        return payload

    def reset_mtpz(self):
        self.send_cmd(OP["EndTrustedAppSession"])
        self.recv_resp()

    def enable_trusted(self, mac_hash):
        cm = _aes_cmac(mac_hash[:16], mac_hash[16:20])
        h1, h2, h3, h4 = struct.unpack(">4I", cm[:16])
        self.send_cmd(OP["EnableTrustedOps"], (h1, h2, h3, h4))
        self.recv_resp()

    # object operations (data-returning commands: DATA then RESPONSE)
    def get_storage_ids(self):
        self.send_cmd(OP["GetStorageIDs"])
        typ, code, payload = self.recv_data()
        if typ != self.DATA:
            raise RuntimeError(f"GetStorageIDs: got type {typ:#06x}")
        self.recv_resp()
        return payload

    def get_object_handles(self, storage, fmt, parent):
        self.send_cmd(OP["GetObjectHandles"], (storage, fmt, parent))
        typ, code, payload = self.recv_data()
        if typ != self.DATA:
            raise RuntimeError(f"GetObjectHandles (format {fmt:#06x}): device answered "
                               f"with code {code:#06x} instead of a handle list")
        self.recv_resp()
        return payload

    def get_object_info(self, handle):
        self.send_cmd(OP["GetObjectInfo"], (handle,))
        typ, code, payload = self.recv_data()
        if typ != self.DATA:
            raise RuntimeError(f"GetObjectInfo: got type {typ:#06x}")
        self.recv_resp()
        return payload

    def send_object_info(self, parent, dataset):
        self.send_cmd(OP["SendObjectInfo"], (self.session_storage, parent))
        self.send_data(OP["SendObjectInfo"], dataset)
        body = self.recv_resp()
        if len(body) >= 12:  # response params: storageID, parentHandle, objectHandle
            return struct.unpack_from("<I", body, 8)[0]
        if len(body) >= 4:
            return struct.unpack_from("<I", body, 0)[0]
        return None

    def send_object_data(self, handle, chunk):
        self.send_cmd(OP["SendObject"])  # takes no parameters (the Zune rejects one: 0x2006)
        self.send_data(OP["SendObject"], chunk)
        self.recv_resp()

    def delete_object(self, handle):
        self.send_cmd(OP["DeleteObject"], (handle,))
        self.recv_resp()

    # session_storage is set by authenticate()/connect()
    session_storage = 0x00010001


# ── High-level client ─────────────────────────────────────────────────────────

class MTPClient:
    def __init__(self, mtp, storage_id):
        self.transport = mtp
        self.mtp = mtp
        self.storage_id = storage_id
        self.root_handle = 0     # top-level objects have ParentObject == 0

    @staticmethod
    def authenticate(mtp):
        """Open the session and run the MTPZ RSA+CMAC handshake."""
        keys = _load_mtpz_keys()
        n = int.from_bytes(keys["MTPZ_MODULUS"], "big")
        d = int.from_bytes(keys["MTPZ_PRIVATE_KEY"], "big")
        certs = keys["MTPZ_CERTIFICATES"]
        mtp.open_session()
        mtp.set_device_prop(0xD406, "libmtp/Sajid Anwar - MTPZClassDriver")
        mtp.reset_mtpz()
        msg, rand = _build_app_cert_msg(certs, n, d)
        mtp.send_mtpz(msg)
        resp = mtp.get_mtpz()
        mac_hash, _key = _parse_dev_resp(resp, rand, n, d)
        mtp.send_mtpz(_build_confirmation(mac_hash))
        mtp.enable_trusted(mac_hash)

    def enumerate_objects(self, fmt_code, parent):
        payload = self.mtp.get_object_handles(self.storage_id, fmt_code, parent)
        handles = []
        for i in range(4, len(payload) - 3, 4):  # payload[0:4] is the array count
            h = struct.unpack_from("<I", payload, i)[0]
            if h:
                handles.append(h)
        return handles

    def get_obj_info(self, handle):
        body = self.mtp.get_object_info(handle)
        off = 0
        storage_id, off = struct.unpack_from("<I", body, off), off + 4
        obj_format, off = struct.unpack_from("<H", body, off), off + 2
        prot, off = struct.unpack_from("<H", body, off), off + 2
        size, off = struct.unpack_from("<I", body, off), off + 4
        off += 26                       # thumb/image fields
        parent, off = struct.unpack_from("<I", body, off), off + 4
        off += 10                       # associationType(2)/Desc(4), sequenceNumber(4)
        filename, off = _mtp_str_d(body, off)
        info = {
            "handle": handle,
            "storage": storage_id[0],
            "format": obj_format[0],
            "protection": prot[0],
            "size": size[0],
            "parent": parent[0],
            "filename": filename,
        }
        return info

    def create_file(self, parent, fmt, filename, data, title=None, w=0, h=0, depth=0):
        now = datetime.datetime.now().strftime("%Y:%m:%d %H:%M:%S")
        # Standard MTP ObjectInfo dataset (little-endian).
        # 52-byte fixed header: parent at byte 38, filename at byte 52 (MTP spec)
        ds = struct.pack("<IHHIHIIIIIIIHII", self.storage_id, fmt, 0, len(data),
                         0, 0, 0, 0, w, h, depth, parent, 0, 0, 0)  # w/h/depth: imagePixWidth/Height, bitDepth
        ds += _mtp_str_e(filename) + b"\x00" + b"\x00"  # filename, empty capture/modified dates
        return self.mtp.send_object_info(parent, ds)

    def push_data(self, handle, data):
        # One SendObject: a single DATA container whose payload is streamed in
        # 16KB writes. Large writes can stall the Zune.
        BATCH = 16 * 1024
        m = self.mtp
        m.send_cmd(OP["SendObject"])
        total = 12 + len(data)
        m.write(struct.pack("<IHHI", total, m.DATA, OP["SendObject"], m.tx))
        for off in range(0, len(data), BATCH):
            m.write(data[off:off + BATCH])
        if total % 512 == 0:
            m.write(b"")  # zero-length packet ends the transfer
        m.recv_resp()

    def delete(self, handle):
        try:
            self.mtp.delete_object(handle)
        except Exception:
            pass

    def set_prop(self, handle, prop_code, value):
        if isinstance(value, bytes) and prop_code == PROP_OBJECTREFS:
            # SetObjectReferences (0x9811): u32 count + u32 handles, little-endian.
            # Callers pack handles with _u32 (big-endian).
            vals = [struct.unpack_from(">I", value, i)[0] for i in range(0, len(value) - 3, 4)]
            op, params = 0x9811, (handle,)
            data = _le32(len(vals)) + b"".join(_le32(v) for v in vals)
        else:
            # SetObjectPropValue (0x9804)
            op, params = 0x9804, (handle, prop_code)
            if isinstance(value, str):
                data = _mtp_str_e(value)
            elif prop_code == PROP_TRACK:
                data = struct.pack("<H", value)  # Track is uint16
            else:
                data = _le32(value)
        self.mtp.send_cmd(op, params)
        self.mtp.send_data(op, data)
        self.mtp.recv_resp()

    def close(self):
        try:
            self.transport.close()
        except Exception:
            pass


# ── File Operations ───────────────────────────────────────────────────────────

def find_ffmpeg():
    import shutil
    p = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    if not os.path.exists(p):
        p = "/usr/local/bin/ffmpeg"
    if not os.path.exists(p):
        sys.exit("Error: ffmpeg not found.  brew install ffmpeg")
    return p


def convert_audio(path, ffmpeg):
    p = Path(path)
    if p.suffix.lower() in (".mp3",):
        try:
            from mutagen.id3 import ID3
            if ID3(path).version >= (2, 4, 0):  # the Zune ignores ID3v2.4: push a v2.3 copy (tags only)
                import shutil
                tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
                tmp.close()
                shutil.copyfile(path, tmp.name)
                ID3(tmp.name).save(v2_version=3)
                return tmp.name, Path(tmp.name).read_bytes(), tmp.name
        except Exception:
            pass  # no/unreadable tags: push as-is
        return path, p.read_bytes(), None  # (path, data, tmpfile)

    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    subprocess.check_call(
        [ffmpeg, "-i", path, "-b:a", "320k", "-map_metadata", "0", "-id3v2_version", "3", "-y", tmp.name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
    )
    data = Path(tmp.name).read_bytes()
    return tmp.name, data, tmp.name  # (path, data, cleanup_path)


def convert_video(path, subtitles, ffmpeg):
    p = Path(path)
    if p.suffix.lower() == ".wmv":
        return path, p.read_bytes(), None

    tmp = tempfile.NamedTemporaryFile(suffix=".wmv", delete=False)
    tmp.close()

    # 320x240 letterboxed (never stretched), max 30fps. -ignore_editlist: some MP4 downloads
    # otherwise decode only the first ~3s. Settings verified playing on a Zune 30 (fw 03.30).
    vf = "scale=320:240:force_original_aspect_ratio=decrease:force_divisible_by=2,pad=320:240:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    if subtitles:
        vf = f"subtitles='{subtitles}'," + vf
    cmd = [ffmpeg, "-ignore_editlist", "1", "-i", path,
           "-vf", vf, "-fpsmax", "30",
           "-c:v", "wmv2", "-b:v", "384k",
           "-minrate", "192k", "-maxrate", "512k", "-bufsize", "512k",
           "-c:a", "wmav2", "-b:a", "128k",
           "-ar", "44100", "-ac", "2",
           "-map_metadata", "-1"]
    cmd.extend(["-y", tmp.name])

    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=4 * 3600)  # full movies take a while
    data = Path(tmp.name).read_bytes()
    return tmp.name, data, tmp.name


def expand_path(path_str):
    p = Path(path_str)
    if p.is_file():
        if p.name.lower().endswith(".url"):
            return []  # Skip Windows Internet shortcut files
        return [str(p)]
    elif p.is_dir():
        files = []
        for root, _, names in os.walk(p):
            for n in sorted(names):
                fp = Path(root) / n
                if fp.is_file() and n.lower().endswith(".url"):
                    continue  # Skip Windows Internet shortcut files
                if fp.is_file():
                    files.append(str(fp))
        return files
    sys.exit(f"Error: '{path_str}' not found")


# ── JPEG Helpers ───────────────────────────────────────────────────────────────

def _parse_jpeg_sof(data: bytes):
    """Parse Start-of-Frame (SOF0) segment to get (width, height). Returns (w, h) or None."""
    soi = data.find(b"\xff\xd8")
    if soi < 0:
        return None
    off = soi + 2  # skip SOI
    while off < len(data) - 1:
        if data[off] != 0xFF:
            off += 1
            continue
        marker = data[off + 1]
        if marker == 0xD9:  # EOI — past any SOF we care about
            return None
        if marker == 0xD0 or marker == 0xD1 or marker == 0xD4 or marker == 0xD9:
            off += 2  # RSTn and other no-length markers
            continue
        if off + 4 >= len(data):
            return None
        seg_len = struct.unpack_from(">H", data, off + 2)[0]
        if marker == 0xC0:  # SOF0 (baseline DCT) — the one the Zune expects
            height = struct.unpack_from(">H", data, off + 5)[0]
            width = struct.unpack_from(">H", data, off + 7)[0]
            return width, height
        off += 2 + seg_len  # skip this segment
    return None


def _pixel_dimensions(w, h):
    """Pack (width, height) into 4 bytes for the ObjectInfo dataset.

    Zune firmware expects these 4 bytes after the 'thumb/image'
    fields (offsets 42–45 in the 52-byte ObjectInfo payload).
    """
    return struct.pack("<I", (h << 16) | w)


# ── Auth / Session Management ─────────────────────────────────────────────────

_ACTIVE_SESSIONS = {}


def connect(device):
    """Claim the device, run MTPZ auth, and return an authenticated MTPClient."""
    t = Transport(device)
    if t in _ACTIVE_SESSIONS:
        return _ACTIVE_SESSIONS[t]

    mtp = MTP(device, t.EP_OUT, t.EP_IN)
    MTPClient.authenticate(mtp)

    # Determine the media storage id
    payload = mtp.get_storage_ids()
    if len(payload) < 8:
        sys.exit("Error: Device has no storage.")
    sid = struct.unpack_from("<I", payload, 4)[0]  # payload[0:4] is the array count
    mtp.session_storage = sid

    client = MTPClient(mtp, sid)
    _ACTIVE_SESSIONS[t] = client
    return client


def disconnect(client):
    try:
        client.transport.close()
    except Exception:
        pass


# ── Sync Functions ────────────────────────────────────────────────────────────

def _track_tags(path):
    """Title/artist/album/track from the file's ID3 tags (title falls back to the file stem)."""
    tags = {"title": Path(path).stem}
    try:
        from mutagen.id3 import ID3
        id3 = ID3(path)
    except Exception:
        return tags
    for key, frame in (("title", "TIT2"), ("artist", "TPE1"), ("album", "TALB"), ("album_artist", "TPE2")):
        if frame in id3 and str(id3[frame]).strip():
            tags[key] = str(id3[frame]).strip()
    if "TRCK" in id3:
        try:
            tags["track"] = int(str(id3["TRCK"]).split("/")[0])
        except ValueError:
            pass
    return tags


def sync_audio(paths, client, ffmpeg, on_progress=None):
    """Push audio files; returns the device handles of the pushed tracks, in order."""
    audio_exts = {".mp3", ".wav", ".m4a", ".flac"}
    total = 0
    pushed = 0
    pushed_handles = []

    for path in paths:
        p = Path(path)
        if p.suffix.lower() not in audio_exts:
            print(f"  skip {p.name}")
            continue
        total += 1
        tags = _track_tags(path)
        # The Zune names MP3 objects "<Name property>.mp3", so match on the title
        device_name = f"{tags['title']}.mp3"

        # Check if file already exists
        handles = client.enumerate_objects(FMT["MP3"], client.root_handle)
        existing = None
        for h in handles:
            info = client.get_obj_info(h)
            if info["filename"].lower() == device_name.lower():
                existing = h
                break

        if existing:
            client.delete(existing)

        converted_path, data, cleanup = convert_audio(path, ffmpeg)

        filename = p.stem + ".mp3"  # always store as .mp3
        handle = client.create_file(client.root_handle, FMT["MP3"], filename, data, title=p.stem)
        client.push_data(handle, data)
        # Set the metadata the Zune displays (it also derives the object's file name from Name)
        for prop, value in ((PROP_NAME, tags["title"]), (PROP_ARTIST, tags.get("artist")),
                            (PROP_ALBUM, tags.get("album")), (0xDC9B, tags.get("album_artist")),  # AlbumArtist
                            (PROP_TRACK, tags.get("track"))):
            if value is None:
                continue
            try:
                client.set_prop(handle, prop, value)
            except RuntimeError as e:
                print(f"    (could not set property {prop:#06x}: {e})")
        pushed_handles.append(handle)
        pushed += 1

        if on_progress:
            on_progress(pushed, total, p.name)
        print(f"  ✓ {p.name}")

        # Cleanup temp file
        if cleanup:
            try:
                os.unlink(cleanup)
            except Exception:
                pass

    print(f"\n  Pushed {pushed}/{total} audio files.\n")
    return pushed_handles


def _root_folder(client, name):
    """Handle of a top-level device folder (Music, Video, Pictures, Playlists, ...)."""
    for h in client.enumerate_objects(FMT["Assoc"], client.root_handle):
        try:
            info = client.get_obj_info(h)
        except Exception:
            continue
        if info["filename"] == name and info.get("parent", 0) == 0:
            return h
    sys.exit(f"  No '{name}' folder on the device")


def sync_video(paths, subtitles_map, client, ffmpeg, on_progress=None):
    video_exts = {".mp4", ".m4v", ".avi", ".mov", ".mkv", ".wmv"}
    total = 0
    pushed = 0

    for path in paths:
        p = Path(path)
        if p.suffix.lower() not in video_exts:
            print(f"  skip {p.name}")
            continue
        total += 1

        sub = subtitles_map.get(str(p))

        video_dir = _root_folder(client, "Video")
        handles = client.enumerate_objects(FMT["WMV"], client.root_handle)
        existing = None
        for h in handles:
            info = client.get_obj_info(h)
            if info["filename"].lower() == (p.stem + ".wmv").lower():  # stored name, not the source's
                existing = h
                break

        if existing:
            client.delete(existing)

        converted_path, data, cleanup = convert_video(path, sub, ffmpeg)

        filename = p.stem + ".wmv"  # always store as .wmv
        handle = client.create_file(video_dir, FMT["WMV"], filename, data, title=p.stem, w=320, h=240)
        client.push_data(handle, data)
        try:
            client.set_prop(handle, PROP_NAME, p.stem)  # title shown under Videos
        except Exception as e:
            print(f"    (could not set name: {e})")
        pushed += 1
        print(f"  ✓ {p.name} → {filename}")

        if cleanup:
            try:
                os.unlink(cleanup)
            except Exception:
                pass

    print(f"\n  Pushed {pushed}/{total} video files.\n")


def sync_playlist(name, track_paths, client, ffmpeg, track_handles=None):
    playlist_name = f"{name}.pla"

    # Find or create Playlists folder
    handles = client.enumerate_objects(FMT["Assoc"], client.root_handle)
    pl_dir = None
    for h in handles:
        info = client.get_obj_info(h)
        if info["filename"] == "Playlists":
            pl_dir = h
            break

    if not pl_dir:
        pl_dir = client.create_folder(client.root_handle, "Playlists")

    # Find or create .pla object
    pla_handles = client.enumerate_objects(FMT["Playlist"], pl_dir)
    pla = None
    for h in pla_handles:
        info = client.get_obj_info(h)
        if info["filename"].lower() == playlist_name.lower():
            pla = h
            break

    if pla:
        client.delete(pla)
        pla = None

    if not pla:
        pla = client.create_file(pl_dir, FMT["Playlist"], playlist_name, b"")
        client.mtp.send_object_data(pla, b"")  # commit the empty object
        try:
            client.set_prop(pla, PROP_NAME, name)  # Name property
        except RuntimeError as e:
            print(f"  (could not set playlist Name: {e})")

    # Resolve track handles (or use the ones the caller just pushed)
    audio_exts = {".mp3", ".wav", ".m4a", ".flac"}
    refs = b"".join(_u32(h) for h in track_handles or [])
    for tp in ([] if track_handles else track_paths):
        p = Path(tp)
        if p.suffix.lower() not in audio_exts:
            continue
        handles = client.enumerate_objects(FMT["MP3"], client.root_handle)
        for h in handles:
            info = client.get_obj_info(h)
            if info["filename"].lower() == p.name.lower():
                refs += _u32(h)
                break

    if refs:
        client.set_prop(pla, PROP_OBJECTREFS, refs)  # ObjectReferences

    print(f"  Playlist '{name}' created with {len(refs) // 4 if refs else 0} tracks.\n")


def _zune_photo(path):
    """Fit an image to exactly the Zune 30 screen (320x240, or 240x320 portrait), letterboxed,
    as a baseline JPEG without EXIF. Returns (jpeg_bytes, width, height). HEIC goes through macOS sips."""
    import io
    from PIL import Image, ImageOps
    src, tmp = str(path), None
    if Path(path).suffix.lower() in (".heic", ".heif"):
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False).name
        subprocess.check_call(["sips", "-s", "format", "jpeg", src, "--out", tmp],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        src = tmp
    try:
        im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")  # honour camera rotation
    finally:
        if tmp:
            os.unlink(tmp)
    box = (320, 240) if im.width >= im.height else (240, 320)
    im.thumbnail(box, Image.LANCZOS)
    canvas = Image.new("RGB", box, (0, 0, 0))
    canvas.paste(im, ((box[0] - im.width) // 2, (box[1] - im.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=90, progressive=False, optimize=False, subsampling=2)
    return buf.getvalue(), box[0], box[1]


def sync_photos(paths, client, resize=0, on_progress=None):
    """Push JPEG/PNG/BMP images to the device's Pictures folder.

    Handles:
      - Image format detection (JPEG, PNG, BMP)
      - Pixel dimension extraction from JPEG SOF0 segment
      - --resize MAXPX downscales JPEGs before push
      - Name property set on the file so the Zune displays the filename
      - Deduplication by filename
    """
    image_exts = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".heic", ".heif"}
    total = 0
    pushed = 0

    for path in paths:
        p = Path(path)
        if p.suffix.lower() not in image_exts:
            print(f"  skip {p.name}")
            continue
        total += 1

        # Every image becomes an exact-screen baseline JPEG: large/progressive JPEGs list a
        # thumbnail but "can't be played" (verified on a Zune 30)
        try:
            data, iw, ih = _zune_photo(p)
        except Exception as e:
            print(f"  skip {p.name} ({e})")
            continue
        cleanup = None
        pics_dir = _root_folder(client, "Pictures")

        # Check for duplicates (by filename)
        pic_handles = client.enumerate_objects(FMT["JPEG"], pics_dir)
        pic_handles += client.enumerate_objects(FMT["Assoc"], pics_dir)
        existing = None
        for h in pic_handles:
            info = client.get_obj_info(h)
            if info["filename"].lower() == (p.stem + ".jpg").lower():
                existing = h
                break

        if existing:
            print(f"  = {p.name} (already on device, skipped)")
            if cleanup:
                try:
                    os.unlink(cleanup)
                except Exception:
                    pass
            continue

        # Create the file
        filename = p.stem + ".jpg"  # always pushed as JPEG
        obj_fmt = FMT["JPEG"]  # JPEG format code for the image
        handle = client.create_file(pics_dir, obj_fmt, filename, data, title=p.stem, w=iw, h=ih, depth=24)
        client.push_data(handle, data)
        pushed += 1
        print(f"  ✓ {p.name} ({len(data) / 1024:.0f} KB)")
        if on_progress:
            on_progress(pushed, total, p.name)

        # Clean up temp resize files
        if cleanup:
            try:
                os.unlink(cleanup)
            except Exception:
                pass

    print(f"\n  Pushed {pushed}/{total} images.\n")


def push_cover_art(album_path, client, explicit_path=None):
    """Find and push the largest JPEG as CoverArt (0x3802).

    Scans the album directory for .jpg/.jpeg files. If ``explicit_path``
    is given, use that file directly.  Otherwise pick the largest JPEG
    by file size.  The cover art is pushed to a "CoverArt" folder at
    the device root and the handle is returned so it can be linked to
    the Abstract Album.

    Args:
        album_path: Path to the album directory on disk.
        client: Authenticated MTPClient.
        explicit_path: Optional explicit path to a cover art JPEG.

    Returns:
        The device handle of the pushed cover art object, or None.
    """
    jpeg_exts = (".jpg", ".jpeg")
    album_dir = Path(album_path)

    # Resolve the candidate file
    candidate = None
    if explicit_path:
        ep = Path(explicit_path)
        if ep.is_file() and ep.suffix.lower() in jpeg_exts:
            candidate = ep
        else:
            print("  Warning: specified cover art is not a .jpg/.jpeg file, skipping.")

    if candidate is None:
        # Auto-detect: find all JPEGs, pick the largest by file size
        jpg_files = []
        for root, _, names in os.walk(album_dir):
            for n in sorted(names):
                if n.lower().endswith(jpeg_exts):
                    fp = Path(root) / n
                    if fp.is_file():
                        jpg_files.append(fp)

        if not jpg_files:
            print("  No cover art found (no .jpg/.jpeg files in album).")
            return None
        candidate = max(jpg_files, key=lambda p: p.stat().st_size)

    print(f"  Cover art: {candidate.name} ({candidate.stat().st_size / 1024:.0f} KB)")

    # Create CoverArt folder at root (Assoc, then JPEG inside)
    coverart_handle = client.create_folder(client.root_handle, "CoverArt")
    data = candidate.read_bytes()
    handle = client.send_object_info(FMT["CoverArt"], coverart_handle,
                                     candidate.name, len(data))
    client.push_data(handle, data)
    print(f"  ✓ Cover art pushed (handle 0x{handle:08X}).")
    return handle


def create_abstract_album(album_name, track_handles, client):
    """Create an Abstract Album (0xBA03) object at the device root.

    The Zune 30 displays artist/album/song metadata during playback
    only if an Abstract Album object exists with a matching name.
    This function creates that object and links the tracks via
    ObjectReferences (0xDC98 / 0xDC41).

    Args:
        album_name: Human-readable name for the album (used for Name property)
        track_handles: List of MP3 file handles belonging to this album
        client: Authenticated MTPClient
    """
    if not track_handles:
        return None

    # Create the abstract album object (no data needed)
    handle = client.create_file(client.root_handle, FMT["AbstractAlbum"],
                                album_name, b"")
    client.mtp.send_object_data(handle, b"")  # commit the empty object

    # Set the Name property so the Zune can display the album title
    try:
        client.set_prop(handle, PROP_NAME, album_name)
    except RuntimeError as e:
        print(f"  (could not set album Name: {e})")

    # Set ObjectReferences to link the tracks
    refs = b"".join(_u32(h) for h in track_handles)
    client.set_prop(handle, PROP_OBJECTREFS, refs)

    return handle


def sync_album(folder, client, ffmpeg, name=None, cover_art=None):
    """Push all audio files in a folder, then create playlist + abstract album.

    This is the complete workflow for syncing an album with metadata display:
    1. Push every MP3/WAV/M4A/FLAC file in the folder
    2. Create (or update) a .pla playlist in the Playlists folder
    3. Push cover art (optional, 0x3802 CoverArt format)
    4. Create an Abstract Album object so the Zune displays metadata during playback

    The album name is derived from the folder name (split on first dash or first space),
    unless overridden by the `name` parameter.

    After syncing, the user should restart the Zune for metadata to appear.
    """
    album_path = Path(folder)
    if not album_path.is_dir():
        sys.exit(f"Error: '{folder}' not found")

    # Derive album name from folder name
    if name is None:
        raw = album_path.name
        # Split on dash or first space
        album_name = re.sub(r"^\d+\s*[-–—]\s*", "", raw).strip()
        if not album_name:
            album_name = raw
    else:
        album_name = name

    if name is None:  # the abstract album must match the tracks' ID3 album tag exactly
        first = next((f for f in sorted(album_path.rglob("*.mp3"))), None)
        album_name = (_track_tags(first).get("album") if first else None) or album_name
    print(f"  Album: {album_name}")
    print()

    # Step 1: Push all audio files (reuses sync_audio)
    track_paths = expand_path(str(album_path))
    audio_exts = {".mp3", ".wav", ".m4a", ".flac"}
    audio_paths = [p for p in track_paths if Path(p).suffix.lower() in audio_exts]

    if not audio_paths:
        sys.exit(f"Error: No audio files found in '{folder}'")

    print(f"  Pushing {len(audio_paths)} audio files...")
    track_handles = sync_audio(audio_paths, client, ffmpeg)

    # Step 2: Create playlist (reuses sync_playlist)
    print(f"  Creating playlist '{album_name}'...")
    sync_playlist(album_name, audio_paths, client, ffmpeg, track_handles=track_handles)

    # Step 3: Push cover art (auto-detect or explicit)
    cover_handle = push_cover_art(album_path, client, explicit_path=cover_art)

    # Step 4: Create abstract album
    print(f"  Creating abstract album '{album_name}'...")
    matched_handles = list(track_handles)

    # If cover art was found, include it in ObjectReferences
    if cover_handle is not None:
        matched_handles.append(cover_handle)

    handle = create_abstract_album(album_name, matched_handles, client)
    if handle:
        print(f"  Abstract album '{album_name}' created (handle 0x{handle:08X}).")
    else:
        print("  Warning: No tracks matched — abstract album not created.")

    print("\n  Restart the Zune for metadata to appear in Music → Albums.")
    print()


def list_files(client):
    audio_handles = client.enumerate_objects(FMT["MP3"], client.root_handle)
    video_handles = client.enumerate_objects(FMT["WMV"], client.root_handle)
    try:  # MP4 exists on the Zune HD; the Zune 30 rejects the MP4 format code
        video_handles += client.enumerate_objects(FMT["MP4"], client.root_handle)
    except RuntimeError:
        pass

    print("Files on device:")
    print("-" * 60)

    if audio_handles:
        print("\n  Music:")
        for h in audio_handles:
            info = client.get_obj_info(h)
            mb = info["size"] / 1024 / 1024
            print(f"    {info['filename']:40s} {mb:6.1f} MB")

    if video_handles:
        print("\n  Video:")
        for h in video_handles:
            info = client.get_obj_info(h)
            mb = info["size"] / 1024 / 1024
            print(f"    {info['filename']:40s} {mb:6.1f} MB")

    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def find_device():
    devs = usb.core.find(find_all=True, idVendor=0x045E)
    found = list(devs)
    if not found:
        sys.exit("No Zune device found. Connect it and try again.")
    dev = found[0]
    pid = dev.idProduct
    model = ZUNE_PIDS.get(pid, f"Zune (PID {pid:#06x})")
    return dev, model


def main():
    parser = argparse.ArgumentParser(
        description="Sync files to a Microsoft Zune device.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s push --audio ~/Music/song.mp3
  %(prog)s push --audio ~/Music/Album
  %(prog)s push --video ~/Videos/movie.mp4
  %(prog)s playlist "Favorites" song1.mp3 song2.mp3
  %(prog)s list
  %(prog)s eject
""")

    sub = parser.add_subparsers(dest="cmd", required=True)

    # push
    p_push = sub.add_parser("push")
    p_push.add_argument("--audio", nargs="+", metavar="PATH")
    p_push.add_argument("--video", nargs="+", metavar="PATH")
    p_push.add_argument("--subtitles", nargs="+", metavar="PATH", help="untested (future feature)")
    p_push.add_argument("--subtitles-on", action="store_true", help="untested (future feature)")
    p_push.add_argument("--subtitles-on-dir", metavar="DIR", help="untested (future feature)")

    # playlist
    p_pl = sub.add_parser("playlist")
    p_pl.add_argument("name")
    p_pl.add_argument("tracks", nargs="+", metavar="PATH")

    # list
    sub.add_parser("list")

    # eject
    sub.add_parser("eject")

    # verify-tags
    p_vt = sub.add_parser("verify-tags")
    p_vt.add_argument("--file", metavar="PATH", help="Local MP3 file to compare tags against")

    # fix-metadata
    p_fm = sub.add_parser("fix-metadata")
    p_fm.add_argument("--name", metavar="NAME", help="Album name to fix")

    # transcribe (Korean → ASCII)
    p_tr = sub.add_parser("transcribe")
    p_tr.add_argument("folder")
    p_tr.add_argument("--mapping", metavar="FILE", help="JSON mapping file (track_num → ascii_title)")
    p_tr.add_argument("--artist", default="", help="Artist name override")

    # delete (tracks / albums / playlists)
    p_del = sub.add_parser("delete")
    p_del.add_argument("target")
    p_del.add_argument("--type", choices=["tracks", "albums", "playlists"],
                       default="tracks", help="What to delete (default: tracks)")

    # album (full workflow: push + playlist + abstract album)
    p_album = sub.add_parser("album")
    p_album.add_argument("folder")
    p_album.add_argument("--name", metavar="NAME", help="Override album name")
    p_album.add_argument("--cover-art", metavar="PATH",
                         help="Explicit path to a cover art .jpg/.jpeg file")

    # photos
    p_ph = sub.add_parser("photos")
    p_ph.add_argument("folder")
    p_ph.add_argument("--resize", type=int, metavar="MAXPX", help="Downscale JPEGs above this pixel dimension")

    args = parser.parse_args()
    device, model = find_device()
    ffmpeg = find_ffmpeg()

    if args.cmd == "push":
        client = connect(device)

        # Build subtitle map
        subtitles_map = {}
        video_paths = []
        audio_paths = []

        if args.audio:
            for p in args.audio:
                audio_paths.extend(expand_path(p))

        if args.video:
            for p in args.video:
                video_paths.extend(expand_path(p))

        if args.subtitles:
            for srt in args.subtitles:
                base = Path(srt).stem
                for v in video_paths:
                    if Path(v).stem.lower() == base.lower():
                        subtitles_map[v] = srt
                        break

        if args.subtitles_on_dir:
            sdir = Path(args.subtitles_on_dir)
            if sdir.is_dir():
                for v in video_paths:
                    srt = sdir / f"{Path(v).stem}.srt"
                    if srt.exists():
                        subtitles_map[str(v)] = str(srt)

        if audio_paths:
            sync_audio(audio_paths, client, ffmpeg)

        if video_paths:
            sync_video(video_paths, subtitles_map, client, ffmpeg)

        client.close()

    elif args.cmd == "playlist":
        client = connect(device)
        tracks = []
        for p in args.tracks:
            tracks.extend(expand_path(p))
        sync_playlist(args.name, tracks, client, ffmpeg)
        client.close()

    elif args.cmd == "list":
        client = connect(device)
        list_files(client)
        client.close()

    elif args.cmd == "eject":
        print(f"Ejecting {model}...")
        for t, c in list(_ACTIVE_SESSIONS.items()):
            try:
                c.close()
            except Exception:
                pass
        _ACTIVE_SESSIONS.clear()
        print("Done.\n")

    elif args.cmd == "verify-tags":
        """Pull the first MP3 from the device and check its ID3 metadata."""
        client = connect(device)
        handles = client.enumerate_objects(FMT["MP3"], client.root_handle)
        if not handles:
            print("  No MP3 files found on device.\n")
            client.close()
            return

        # Pull the first (or specified) file
        if args.file:
            target = Path(args.file).name.lower()
            chosen = None
            for h in handles:
                info = client.get_obj_info(h)
                if info["filename"].lower() == target:
                    chosen = h
                    break
            if not chosen:
                sys.exit(f"Error: File '{args.file}' not found on device.")
        else:
            chosen = handles[0]

        info = client.get_obj_info(chosen)
        print(f"  Checking: {info['filename']} ({info['size']} bytes)\n")

        try:
            data = client.mtp.get_object(chosen)
        except Exception as e:
            print(f"  Error: Could not retrieve file: {e}")
            client.close()
            return

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(data)
            tmpfile = f.name

        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-print_format", "json",
                 "-show_format", tmpfile],
                capture_output=True, text=True, timeout=5)

            if result.returncode == 0:
                import json
                fmt = json.loads(result.stdout).get("format", {})
                tags = fmt.get("tags", {})

                print("  ID3 Metadata:")
                print("  " + "=" * 46)
                for key in ["title", "artist", "album", "track", "genre"]:
                    val = tags.get(key, "(missing)")
                    print(f"    {key:10s}: {val}")
                print("  " + "=" * 46)
            else:
                print("  ffprobe failed")

            Path(tmpfile).unlink(missing_ok=True)
        except Exception as e:
            print(f"  ffprobe error: {e}")

        client.close()

    elif args.cmd == "fix-metadata":
        """Set MTP object properties (Name, Artist, Album) on MP3 files."""
        client = connect(device)
        handles = client.enumerate_objects(FMT["MP3"], client.root_handle)
        if not handles:
            print("  No MP3 files found on device.\n")
            client.close()
            return

        album_name = args.name if args.name else None
        fixed = 0
        for h in handles:
            info = client.get_obj_info(h)
            try:
                # Try setting Name property
                name_val = info["filename"].replace(".mp3", "")
                client.set_prop(h, PROP_NAME, name_val)

                if album_name:
                    client.set_prop(h, PROP_ALBUM, album_name)
                fixed += 1
            except Exception:
                print(f"  skip {info['filename']} (property not supported)")

        print(f"\n  Fixed metadata on {fixed}/{len(handles)} files.\n")
        client.close()

    elif args.cmd == "transcribe":
        """Re-encode an album with ASCII metadata (for Korean/Chinese OSTs).

        Reads a JSON mapping file (track_number → ASCII title), re-encodes
        each track with ID3v2.3 Latin-1 tags containing the ASCII titles,
        then pushes to the device.
        """
        import json
        album_path = Path(args.folder)
        if not album_path.is_dir():
            sys.exit(f"Error: '{args.folder}' not found")

        # Load mapping file
        mapping = {}
        if args.mapping:
            map_path = Path(args.mapping)
            if map_path.is_file():
                mapping = json.loads(map_path.read_text())
            else:
                sys.exit(f"Error: Mapping file '{args.mapping}' not found")

        if not mapping:
            sys.exit("No mapping provided. Use --mapping mapping.json")

        # Load or create output directory in /tmp
        out_dir = Path(f"/tmp/zune-{int(time.time())}")
        out_dir.mkdir(parents=True, exist_ok=True)

        print(f"  Mapping: {args.folder}")
        if args.mapping:
            print(f"  Mapping file: {args.mapping}")
        print(f"  Output: {out_dir}\n")

        count = 0
        for src_file in sorted(album_path.glob("*.mp3")):
            filename = src_file.name
            # Extract track number: "01. Title.mp3" -> "01"
            stem = src_file.stem
            track_num = re.sub(r"[^0-9]", "", stem[:2])
            if len(track_num) > 2:
                track_num = track_num[:2]
            else:
                # Try to find digits in filename
                nums = re.findall(r"\d+", filename)
                track_num = nums[0].zfill(2) if nums else "00"

            ascii_title = mapping.get(track_num)
            if not ascii_title:
                # Try without padding
                unpadded = str(int(track_num))
                ascii_title = mapping.get(unpadded)
                track_num = unpadded.zfill(2)

            if not ascii_title:
                print(f"  ⚠️  {filename} - no mapping found")
                continue

            # Re-encode with ASCII metadata (ID3v2.3 Latin-1)
            out_file = out_dir / filename
            try:
                subprocess.check_call(
                    ["ffmpeg", "-i", str(src_file),
                     "-codec:a", "libmp3lame", "-q:a", "0",
                     "-id3v2_version", "3",
                     "-metadata", f"title={ascii_title}",
                     "-metadata", f"album={args.folder}",
                     "-metadata", f"artist={args.artist or album_path.name}",
                     "-metadata", f"track={track_num}",
                     "-write_xing", "0",
                     "-y", str(out_file)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                print(f"  ✓ {ascii_title}")
                count += 1
            except subprocess.CalledProcessError:
                print(f"  ✗ {filename} - encoding failed")

        print(f"\n  Re-encoded {count}/{len(list(album_path.glob('*.mp3')))} tracks")

        if count > 0:
            # Push the re-encoded files
            client = connect(device)
            print(f"\n  Pushing to device...")
            sync_audio([str(out_file) for out_file in sorted(out_dir.glob("*.mp3"))],
                       client, ffmpeg)
            client.close()
            print(f"  Cleaned up temporary files.")

        print()

    elif args.cmd == "delete":
        """Delete tracks, albums, or playlists from the device."""
        client = connect(device)
        target = args.target.lower()
        target_type = args.type

        if target_type == "tracks":
            # Delete MP3 files matching the filename
            handles = client.enumerate_objects(FMT["MP3"], client.root_handle)
            deleted = 0
            for h in handles:
                info = client.get_obj_info(h)
                if target in info["filename"].lower():
                    client.delete(h)
                    print(f"  deleted {info['filename']}")
                    deleted += 1
            print(f"\n  Deleted {deleted}/{len(handles)} tracks.\n")

        elif target_type == "albums":
            # Delete Abstract Album (0xBA03) objects matching the name
            handles = client.enumerate_objects(FMT["AbstractAlbum"], client.root_handle)
            deleted = 0
            for h in handles:
                info = client.get_obj_info(h)
                fname = info["filename"].replace(".abm", "").lower()
                if target in fname:
                    client.delete(h)
                    print(f"  deleted album '{info['filename']}'")
                    deleted += 1

            # Also delete associated MP3 files
            mp3_handles = client.enumerate_objects(FMT["MP3"], client.root_handle)
            for h in mp3_handles:
                info = client.get_obj_info(h)
                if target in info["filename"].lower():
                    client.delete(h)
                    print(f"  deleted {info['filename']}")
                    deleted += 1

            print(f"\n  Deleted {deleted} objects.\n")

        elif target_type == "playlists":
            # Delete Playlist (0xBA05) and Assoc (.pla) objects
            handles = client.enumerate_objects(FMT["Assoc"], client.root_handle)
            handles += client.enumerate_objects(FMT["Playlist"], client.root_handle)
            deleted = 0
            for h in handles:
                info = client.get_obj_info(h)
                if target in info["filename"].lower():
                    client.delete(h)
                    print(f"  deleted {info['filename']}")
                    deleted += 1
            print(f"\n  Deleted {deleted}/{len(handles)} playlists.\n")

        client.close()

    elif args.cmd == "album":
        """Full album sync workflow (push + playlist + abstract album)."""
        client = connect(device)
        ffmpeg = find_ffmpeg()
        sync_album(args.folder, client, ffmpeg, name=args.name,
                   cover_art=args.cover_art)
        client.close()

    elif args.cmd == "photos":
        """Push image files to the device's Pictures folder."""
        client = connect(device)
        paths = expand_path(args.folder)
        sync_photos(paths, client, resize=getattr(args, 'resize', 0))
        client.close()


if __name__ == "__main__":
    main()
