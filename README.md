# zune-cli — Simple Zune Sync for Mac

Sync music, video, and playlists to a Microsoft Zune 30 (model 1089) from macOS.
Pure Python. No Electron, no npm, no Windows VM.

> **Status (2026-09-12):** verified end-to-end on a real Zune 30 (model 1089, firmware 03.30):
> - ✅ Music — MP3 (WAV/FLAC/M4A converted to MP3) with title/artist/track shown on the device
> - ✅ Albums — abstract album + playlist, so the album appears under Music → Albums
> - ✅ Photos — JPEG/PNG/HEIC/… fitted to the 320×240 screen (open full-screen, usable as background)
> - ✅ Video — MP4/MOV/MKV/AVI converted to 320×240 WMV
> - ⚠️ The Zune's font has no Korean/Chinese/Japanese glyphs — use Latin titles (see below)

> **macOS USB access:** talking to the Zune's bulk endpoints requires the terminal
> to be allowed under **System Settings → Privacy & Security → USB** (or run with
> `sudo`). Without it, writes are silently dropped and reads time out.

## Requirements

1. **Python 3.9+**
2. **ffmpeg** (audio/video conversion) and **libusb** (USB backend for pyusb)
   ```
   brew install ffmpeg libusb
   ```
3. **This repo and its Python packages**
   ```
   git clone https://github.com/rchow93/zune-cli.git
   cd zune-cli
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **MTPZ key file** — the Zune only talks to software that completes Microsoft's MTPZ
   handshake. The keys are **not** included in this repo: put a libmtp-format key file
   (the `.mtpz-data` file used by [libmtp-zune](https://github.com/kbhomes/libmtp-zune))
   at `~/.mtpz-data`, or point the `MTPZ_DATA` environment variable at it. It holds five
   hex lines: public exponent, encryption key, modulus, private key, certificates.
5. **Check the connection** — plug in the Zune, then from the repo folder (venv active):
   ```
   python zune-cli.py list
   ```
   If it lists the device's files, you're ready to sync.

## Usage

### Push audio files
```bash
python zune-cli.py push --audio ~/Music/song.mp3
python zune-cli.py push --audio ~/Music/Album
```

### Push video files (auto-converts to WMV)
```bash
python zune-cli.py push --video ~/Videos/movie.mp4
python zune-cli.py push --video ~/Videos/
```

### Push video with subtitles (burned in)
```bash
# Match SRT files by name: movie.mp4 + movie.srt
python zune-cli.py push --video ~/Videos/ --subtitles-on

# Or specify SRT files explicitly
python zune-cli.py push --video ~/Videos/movie.mp4 --subtitles ~/Videos/movie.srt

# Or from a SRT directory
python zune-cli.py push --video ~/Videos/ --subtitles-on-dir ~/Videos/subs/
```

### Create a playlist on the device
```bash
python zune-cli.py playlist "My Favorites" ~/Music/song1.mp3 ~/Music/song2.mp3
python zune-cli.py playlist "Gym" ~/Music/Gym/
```

### Sync an album folder with metadata display
Pushes every audio file in a folder, then auto-creates:
1. A **playlist** (.pla file) for the Playlists folder
2. An **abstract album** object so the Zune displays artist/album/song metadata

```bash
python zune-cli.py album "~/Music/Artist - Album (Year)"
python zune-cli.py album "/path/to/Album"
```

**Important:** After syncing an album, **restart the Zune** for metadata to appear:
- Hold the power switch **OFF** until the device shuts down (not sleep)
- Wait 10 seconds
- Power back on and wait 1-2 minutes for the library to reindex
- Navigate to **Music → Albums → [Album Name]** to see the tracks with metadata

The abstract album is what makes the Zune display artist/album/track info during playback.
Its name is taken from the MP3s' album tag (falling back to the folder name); `--name` overrides it.
The Zune's font can't show Korean/Chinese/Japanese — retag with Latin titles first (see below).

### Push photos
```bash
python zune-cli.py photos ~/Pictures/Some\ Trip   # a folder, or a single file
```
Every image (JPEG, PNG, HEIC, GIF, TIFF, WebP, BMP) is converted to what the Zune 30 can
actually open: **exactly 320×240** (240×320 for portrait), letterboxed in black, turned upright
per the camera's rotation tag, saved as a **baseline JPEG** without EXIF. Full-size or
progressive JPEGs list a thumbnail but fail with "can't be played". Photos go into the
device's **Pictures** folder as `<name>.jpg`; ones already there are skipped. Your original
files are never modified. (`--resize` is accepted but ignored.)

### List files on device
```bash
python zune-cli.py list
```

### Eject device safely
```bash
python zune-cli.py eject
```

## How It Works

### USB & Protocol
- Connects to Zune over USB using `libusb` (via `pyusb`)
- Clears the Zune's bulk-endpoint halts (a stalled endpoint makes it silently ignore writes)
- Performs MTPZ (Zune extension) authentication — the RSA + AES-CMAC handshake, with
  the keys read from `~/.mtpz-data` (see Requirements)

### Audio & Metadata
- Converts audio (WAV, FLAC, M4A → MP3 320kbps) using ffmpeg, tagged ID3v2.3
- MP3s tagged ID3v2.4 (which the Zune ignores) are pushed as a v2.3 copy — tags only,
  no re-encode; your files are not modified
- Sets title/artist/track number on the device object (the Zune names the track after its title)
- Creates two playlist structures:
  1. **`.pla` (Playlist)** in the Playlists folder for the device's playlist view
  2. **Abstract Album (0xBA03)** at the root level so the Zune displays metadata during playback

### Why Two Playlists?
The Zune 30 only displays artist/album/song metadata during playback if an **Abstract Album object** exists with the same name as the album in the ID3 tags. Without this object, the device plays the files but shows no metadata. The abstract album object links the tracks and tells the device to use their ID3 tags for display.

### Video & Conversion
- Converts MP4/M4V/MOV/MKV/AVI → WMV (`wmv2` + `wmav2` 128k/44.1 kHz), **320×240 letterboxed**
  (widescreen is never stretched), frame rate capped at 30 fps
- `-b:v 384k`: plays smoothly; expect some blockiness on fast motion
- Reads input with `-ignore_editlist 1` — some MP4 downloads otherwise
  convert only their first ~3 seconds
- Stored in the device's **Video** folder; `.wmv` files are pushed as-is
- Embeds subtitles if requested (burned into video, not separate)

## Advanced: Korean/Non-Latin Album Metadata

The Zune 30 can't display Korean, Chinese, or other non-Latin characters on its UI, even though it plays the audio perfectly. To make these albums usable:

### Option 1: Transcribe with Mapping File (Repeatable)

Create a JSON file mapping track numbers to ASCII titles:
```json
{
  "01": "First Song",
  "02": "Second Song",
  "03": "Third Song (Romanized)",
  ...
}
```

Then transcribe the entire album:
```bash
python zune-cli.py transcribe /path/to/korean/album \
  --mapping /path/to/mapping.json \
  --artist "Album Artist Name"
```

This re-encodes all tracks with ASCII metadata and outputs a new folder. Then sync it:
```bash
python zune-cli.py album /path/to/transcribed/folder
```

### Option 2: Retag the files yourself

Edit the tags in any tag editor (Latin characters for title/artist/album), then sync the
folder with `python zune-cli.py album /path/to/folder` and restart the Zune.

### Example

```bash
# Step 1: Transcribe non-Latin titles to English/romanized
python zune-cli.py transcribe "/path/to/album" \
  --mapping "./mapping.json" \
  --artist "Artist Name"

# Step 2: Sync the output folder (auto-creates abstract album + playlist)
python zune-cli.py album "/path/to/transcribed/folder"

# Step 3: Restart the device and open Music → Albums
```

## Troubleshooting

### Metadata not displaying after restart
- **Cause:** Device hasn't reindexed yet. Restart again.
- **Cause:** Files don't have ID3 tags. Use `zune-cli.py verify-tags` to check.
- **Cause:** Zune 30 firmware quirk — old playlists (`.pla`) don't trigger metadata display.

### Syncing large albums is slow
- Normal. A 30-track album at ~10MB per track takes 5-10 minutes over USB.
- No way around this on 2006-era hardware.

### Device shows "Unknown" for artist/album but file plays
- This means no abstract album exists for that track.
- Sync the album folder with `python zune-cli.py album <folder>` (not `push --audio`), then restart.

### Korean/Chinese characters show as boxes on playback
- The ID3 tags are fine (ffmpeg handles this).
- Zune 30 firmware limitation — it can *play* the files but *display* is limited to ASCII.
- Audio plays correctly regardless of what's shown.

## Credits

- The MTPZ handshake follows [libmtp-zune](https://github.com/kbhomes/libmtp-zune).
- MTP/Zune protocol details were cross-checked against [Zune Explorer](https://github.com/NiceBeard/zune-explorer) (MIT).

## License

MIT — see [LICENSE](LICENSE). Not affiliated with or endorsed by Microsoft; "Zune" is a Microsoft trademark.
