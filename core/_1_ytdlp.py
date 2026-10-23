import os,sys
import glob
import json
import re
import subprocess
from core.utils import *

OUTPUT_DIR = "output"
INPUT_MANIFEST = "input_manifest.json"
GENERATED_AUDIO_NAMES = {"dub.mp3", "normalized_dub.wav"}
GENERATED_VIDEO_NAMES = {"output_sub.mp4", "output_dub.mp4"}

def sanitize_filename(filename, max_length=120):
    """Make a filename safe for all Windows/macOS/Linux filesystems.

    - Strips characters that are illegal on Windows (``<>:"/\\|?*``).
    - Collapses runs of whitespace into a single space and trims dots/spaces.
    - Truncates to ``max_length`` *characters* (not bytes), well under the 260-char
      Windows path-length limit even when prepended with long output directories
      and file extensions.
    """
    if not filename:
        return 'video'
    # Remove or replace illegal characters
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Collapse runs of whitespace (including em-space, tabs, newlines) to a single space
    filename = re.sub(r'\s+', ' ', filename)
    # Truncate by grapheme-aware slice — Python 3 strings are Unicode codepoints,
    # so slicing at max_length is safe for ASCII/Emoji/CJK alike.
    if len(filename) > max_length:
        filename = filename[:max_length].rstrip()
    # Ensure filename doesn't start or end with a dot or space
    filename = filename.strip('. ')
    return filename if filename else 'video'

def update_ytdlp():
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"])
        if 'yt_dlp' in sys.modules:
            del sys.modules['yt_dlp']
        rprint("[green]yt-dlp updated[/green]")
    except subprocess.CalledProcessError as e:
        rprint("[yellow]Warning: Failed to update yt-dlp: {e}[/yellow]")
    from yt_dlp import YoutubeDL
    return YoutubeDL

def needs_cookies(error):
    """True when the site refused the download until the user is signed in."""
    message = str(error).lower()
    return any(hint in message for hint in ("sign in to confirm", "--cookies", "login required"))

def _patch_ydl_opts(ydl_opts, proxy, cookies_path):
    """Apply proxy and cookies settings onto the yt-dlp options dict."""
    if proxy is not None:
        if not isinstance(proxy, str):
            raise ValueError('youtube.proxy must be null, an empty string, or a proxy URL')
        ydl_opts['proxy'] = proxy.strip()
    if cookies_path:
        if not os.path.isfile(cookies_path):
            raise ValueError(f"Cookies file not found: {cookies_path}")
        ydl_opts["cookiefile"] = cookies_path


def _load_youtube_settings():
    youtube = load_key("youtube")
    proxy = youtube.get("proxy")
    cookies_path = str(youtube.get("cookies_path") or "").strip().strip('"')
    return proxy, cookies_path


def download_video_ytdlp(url, save_path='output', resolution='1080'):
    os.makedirs(save_path, exist_ok=True)

    proxy, cookies_path = _load_youtube_settings()
    YoutubeDL = update_ytdlp()

    # Pre-fetch metadata using a SAFE outtmpl (never use %(title)s here —
    # some sites return multi-hundred-char titles with emojis / Windows-invalid
    # chars that crash yt-dlp *before* our post-download sanitize rename runs).
    info_opts = {'quiet': True, 'skip_download': True, 'noplaylist': True}
    _patch_ydl_opts(info_opts, proxy, cookies_path)
    with YoutubeDL(info_opts) as infoydl:
        info = infoydl.extract_info(url, download=False) or {}
    video_id = info.get('id') or 'video'
    title = sanitize_filename(info.get('title') or video_id)

    ydl_opts = {
        'format': (
            'bestvideo+bestaudio/best'
            if resolution == 'best'
            else f'bestvideo[height<={resolution}]+bestaudio/best[height<={resolution}]/best'
        ),
        'outtmpl': f'{save_path}/{video_id}.%(ext)s',
        'noplaylist': True,
        'writethumbnail': True,
        'merge_output_format': 'mp4',
        'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}],
    }
    _patch_ydl_opts(ydl_opts, proxy, cookies_path)

    with YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    # Rename the downloaded files from the safe `video_id` back to a sanitized title.
    # Only rename files that still start with `video_id.` or `video_id-` — this lets
    # previously-downloaded files with their own names pass through untouched.
    for file in os.listdir(save_path):
        path = os.path.join(save_path, file)
        if not os.path.isfile(path):
            continue
        stem, ext = os.path.splitext(file)
        if stem != video_id:
            # Not one of our generated files (maybe from a prior run), leave alone.
            new_stem = sanitize_filename(stem)
            if new_stem != stem:
                os.rename(path, os.path.join(save_path, new_stem + ext))
            continue
        new_name = title + ext
        if new_name != file:
            os.rename(path, os.path.join(save_path, new_name))
    media_file = find_video_files(save_path)
    write_input_manifest(media_file, "video", save_path)

def write_input_manifest(media_file: str, media_type: str, save_path='output', subtitle_file=None):
    os.makedirs(save_path, exist_ok=True)
    manifest_path = os.path.join(save_path, INPUT_MANIFEST)
    media_path = media_file.replace("\\", "/") if sys.platform.startswith('win') else media_file
    manifest = {"path": media_path, "type": media_type}
    if subtitle_file:
        manifest["subtitle"] = subtitle_file.replace("\\", "/")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

def _read_input_manifest(save_path='output'):
    manifest_path = os.path.join(save_path, INPUT_MANIFEST)
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    media_file = data.get("path")
    media_type = data.get("type")
    if media_type not in {"video", "audio", "subtitle"} or not media_file or not os.path.exists(media_file):
        return None
    return media_file.replace("\\", "/") if sys.platform.startswith('win') else media_file, media_type

def find_subtitle_file(save_path='output'):
    """The subtitles of the user that take the place of the recognition, or None."""
    manifest_path = os.path.join(save_path, INPUT_MANIFEST)
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path, "r", encoding="utf-8") as f:
        subtitle_file = json.load(f).get("subtitle")
    return subtitle_file if subtitle_file and os.path.exists(subtitle_file) else None

def find_video_files(save_path='output'):
    video_files = [file for file in glob.glob(save_path + "/*") if os.path.isfile(file) and os.path.splitext(file)[1][1:].lower() in load_key("allowed_video_formats")]
    # change \\ to /, this happen on windows
    if sys.platform.startswith('win'):
        video_files = [file.replace("\\", "/") for file in video_files]
    video_files = [
        file
        for file in video_files
        if os.path.basename(file).lower() not in GENERATED_VIDEO_NAMES
    ]
    if len(video_files) != 1:
        raise ValueError(f"Number of videos found {len(video_files)} is not unique. Please check.")
    return video_files[0]

def find_audio_files(save_path='output'):
    audio_files = [file for file in glob.glob(save_path + "/*") if os.path.splitext(file)[1][1:].lower() in load_key("allowed_audio_formats")]
    if sys.platform.startswith('win'):
        audio_files = [file.replace("\\", "/") for file in audio_files]
    audio_files = [file for file in audio_files if os.path.basename(file) not in GENERATED_AUDIO_NAMES]
    if len(audio_files) != 1:
        raise ValueError(f"Number of audio files found {len(audio_files)} is not unique. Please check.")
    return audio_files[0]

def _safe_find_video_file(save_path='output'):
    try:
        return find_video_files(save_path)
    except ValueError as e:
        if "found 0" in str(e):
            return None
        raise

def _safe_find_audio_file(save_path='output'):
    try:
        return find_audio_files(save_path)
    except ValueError as e:
        if "found 0" in str(e):
            return None
        raise

def find_media_file(save_path='output'):
    manifest = _read_input_manifest(save_path)
    if manifest:
        return manifest
    video_file = _safe_find_video_file(save_path)
    if video_file:
        return video_file, "video"
    audio_file = _safe_find_audio_file(save_path)
    if audio_file:
        return audio_file, "audio"
    raise ValueError("No media file found. Please download or upload a media file first.")

def is_audio_only_input(save_path='output'):
    # True when the input has no video: a standalone audio file, or subtitles only.
    # In this case VideoLingo only produces subtitle files; no video output.
    try:
        _, media_type = find_media_file(save_path)
        return media_type in ("audio", "subtitle")
    except Exception:
        return False

if __name__ == '__main__':
    # Example usage
    url = input('Please enter the URL of the video you want to download: ')
    resolution = input('Please enter the desired resolution (360/480/720/1080, default 1080): ')
    resolution = int(resolution) if resolution.isdigit() else 1080
    download_video_ytdlp(url, resolution=resolution)
    print(f"🎥 Video has been downloaded to {find_video_files()}")
