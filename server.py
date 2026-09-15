import http.server
import socketserver
import urllib.parse
import urllib.request
import asyncio
import os
import sys
import subprocess
import re
import shutil
import json
import threading
import uuid

# Global tasks status dictionary
active_tasks = {}

def update_task_progress(task_id, msg):
    task = active_tasks.get(task_id)
    if not task:
        return
    task['logs'].append(msg)
    task['message'] = msg
    
    # Parse part progress: e.g. "📂 [ផ្នែកទី 2/4] កំពុងដំណើរការ..."
    m_part = re.search(r"\[\u1795\u17d2\u179c\u17c2\u1780\u1791\u17b8\s*(\d+)/(\d+)\]", msg, re.IGNORECASE)
    if m_part:
        current_part = int(m_part.group(1))
        total_parts = int(m_part.group(2))
        task['current_part'] = current_part
        task['total_parts'] = total_parts
        task['progress_pct'] = int(((current_part - 1) / total_parts) * 85)
        return
        
    # Parse TTS progress: "Generating TTS for subtitle 5/10..."
    m_sub = re.search(r"subtitle\s+(\d+)/(\d+)", msg, re.IGNORECASE)
    if m_sub:
        current_sub = int(m_sub.group(1))
        total_subs = int(m_sub.group(2))
        
        if 'current_part' in task and 'total_parts' in task:
            part = task['current_part']
            tot_parts = task['total_parts']
            part_start = ((part - 1) / tot_parts) * 85
            part_end = (part / tot_parts) * 85
            part_range = part_end - part_start
            sub_progress = (current_sub / total_subs) * part_range
            task['progress_pct'] = int(part_start + sub_progress)
        else:
            task['progress_pct'] = int(5 + (current_sub / total_subs) * 80)
        return
        
    if "Merging audio" in msg or "លាយសំឡេង" in msg:
        task['progress_pct'] = 88
    elif "រួមបញ្ចូលវីដេអូ" in msg or "concat" in msg.lower():
        task['progress_pct'] = 93
    elif "Cleaning" in msg or "សម្អាត" in msg:
        task['progress_pct'] = 97
    elif "Saved permanent copy" in msg or "ជោគជ័យ" in msg or "ចម្លង" in msg:
        task['progress_pct'] = 100

# Force UTF-8 encoding for standard streams to prevent Windows CP1252 crash on Khmer text
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass


# Cloud deployment: PORT from environment (Railway/Render set this automatically)
PORT = int(os.environ.get('PORT', 8000))
HOST = '0.0.0.0'  # Listen on all interfaces (required for cloud)

# Track which TTS engine is currently active
active_tts_engine = "edge-tts"  # or "gtts"

# Function to auto-install required libraries
def install_requirements():
    libs_needed = []
    try:
        import edge_tts
    except ImportError:
        libs_needed.append('edge-tts')
    try:
        import imageio_ffmpeg
    except ImportError:
        libs_needed.append('imageio-ffmpeg')
    try:
        import gtts
    except ImportError:
        libs_needed.append('gtts')

    if libs_needed:
        print(f" Installing required libraries: {', '.join(libs_needed)}...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install"] + libs_needed)
            print(" Libraries installed successfully.")
        except Exception as e:
            print(f" Error installing libraries: {e}")
            print(f"Please run: pip install {' '.join(libs_needed)}")
    else:
        print(" All required libraries (edge-tts, imageio-ffmpeg, gtts) are already installed.")

install_requirements()

import imageio_ffmpeg
import edge_tts

# Try importing gTTS (fallback engine)
try:
    from gtts import gTTS as GoogleTTS
    GTTS_AVAILABLE = True
    print(" gTTS (Google TTS fallback) is available.")
except ImportError:
    GTTS_AVAILABLE = False
    print(" gTTS not available — only edge-tts will be used.")

# -----------------------------------------------------------------------
def detect_voice_gender(wav_path):
    import wave
    import struct
    try:
        with wave.open(wav_path, 'rb') as w:
            num_frames = w.getnframes()
            sample_rate = w.getframerate()
            if num_frames == 0 or sample_rate == 0:
                return "Unknown"
            
            sec_to_read = 0.5
            frames_to_read = int(sec_to_read * sample_rate)
            if num_frames > frames_to_read:
                start_frame = (num_frames - frames_to_read) // 2
                w.setpos(start_frame)
            else:
                frames_to_read = num_frames
                
            data = w.readframes(frames_to_read)
            
        fmt = f"{len(data) // 2}h"
        samples = list(struct.unpack(fmt, data))
        
        if not samples:
            return "Unknown"
        mean_sq = sum(s*s for s in samples) / len(samples)
        rms = mean_sq ** 0.5
        if rms < 300:
            return "Unknown"
            
        window_size = min(2000, len(samples))
        window = samples[len(samples)//2 - window_size//2 : len(samples)//2 + window_size//2]
        if not window:
            window = samples[:window_size]
            
        lag_min = int(sample_rate / 300)
        lag_max = int(sample_rate / 75)
        
        best_lag = -1
        max_correlation = -float('inf')
        
        for lag in range(lag_min, lag_max + 1):
            corr = 0
            limit = len(window) - lag
            if limit <= 0:
                continue
            corr = sum(window[i] * window[i + lag] for i in range(0, limit, 2))
            if corr > max_correlation:
                max_correlation = corr
                best_lag = lag
                
        if best_lag != -1:
            freq = sample_rate / best_lag
            if 165 <= freq <= 300:
                return "Female"
            elif 75 <= freq < 165:
                return "Male"
    except Exception as e:
        print("Gender detection exception:", e)
    return "Unknown"


# Multi-engine TTS generator
# Tries edge-tts (Piseth/Sreymom) first; falls back to gTTS if it fails.
# -----------------------------------------------------------------------
async def generate_tts_mp3(text: str, edge_voice: str, rate_param: str, output_path: str, pitch_param: str = None) -> str:
    """
    Generate TTS audio. Returns 'edge-tts' or 'gtts' to indicate which engine succeeded.
    Raises Exception if both fail.
    """
    global active_tts_engine

    # --- Attempt 1: edge-tts (Piseth / Sreymom Neural voices) ---
    try:
        if pitch_param:
            communicate = edge_tts.Communicate(text, edge_voice, rate=rate_param, pitch=pitch_param)
        else:
            communicate = edge_tts.Communicate(text, edge_voice, rate=rate_param)
        await asyncio.wait_for(communicate.save(output_path), timeout=25.0)
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            active_tts_engine = "edge-tts"
            return "edge-tts"
        else:
            raise RuntimeError("edge-tts produced empty file")
    except Exception as e:
        print(f"  [TTS] edge-tts failed ({type(e).__name__}: {e}), trying gTTS fallback...")
        # Fallback to no-pitch if edge-tts failed because of an invalid pitch format
        if pitch_param:
            try:
                print("  [TTS] Retrying edge-tts without pitch parameter...")
                communicate = edge_tts.Communicate(text, edge_voice, rate=rate_param)
                await asyncio.wait_for(communicate.save(output_path), timeout=25.0)
                if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    active_tts_engine = "edge-tts"
                    return "edge-tts"
            except Exception as e_retry:
                print(f"  [TTS] Retry edge-tts without pitch also failed: {e_retry}")

    # --- Attempt 2: gTTS (Google Translate TTS, Khmer locale) ---
    if GTTS_AVAILABLE:
        try:
            def _gtts_sync():
                tts = GoogleTTS(text=text, lang='km', slow=False)
                tts.save(output_path)
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(
                loop.run_in_executor(None, _gtts_sync),
                timeout=20.0
            )
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                active_tts_engine = "gtts"
                return "gtts"
            else:
                raise RuntimeError("gTTS produced empty file")
        except Exception as e2:
            print(f"  [TTS] gTTS fallback also failed: {e2}")

    raise RuntimeError("All TTS engines failed. Check network connection and try again.")


# SRT parsing helpers
def time_to_ms(h, m, s, ms):
    return ((h * 3600) + (m * 60) + s) * 1000 + ms

def parse_srt(srt_path):
    with open(srt_path, 'r', encoding='utf-8') as f:
        content = f.read()
    content = content.replace('\r\n', '\n')
    
    # Strip markdown block wrappers or conversational intro text from Gemini
    lines = content.split('\n')
    srt_start_idx = -1
    for idx, line in enumerate(lines):
        if '-->' in line:
            if idx > 0 and lines[idx - 1].strip().isdigit():
                srt_start_idx = idx - 1
            else:
                srt_start_idx = idx
            break
            
    if srt_start_idx != -1:
        lines = lines[srt_start_idx:]
        
    clean_lines = []
    for line in lines:
        if line.strip().startswith('```'):
            continue
        clean_lines.append(line)
        
    reconstructed = "\n".join(clean_lines).strip()
    blocks = re.split(r'\n\s*\n', reconstructed)
    subtitles = []
    for block in blocks:
        blk_lines = [l.strip() for l in block.split('\n') if l.strip()]
        if len(blk_lines) >= 2:
            time_line = ""
            text_lines = []
            
            time_idx = -1
            for l_idx, l in enumerate(blk_lines):
                if '-->' in l:
                    time_idx = l_idx
                    time_line = l
                    break
            
            if time_idx != -1:
                text_lines = blk_lines[time_idx + 1:]
                text = " ".join(text_lines).strip()
                match = re.match(
                    r'(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,\.](\d{3})',
                    time_line
                )
                if match:
                    start_ms = time_to_ms(int(match.group(1)), int(match.group(2)),
                                          int(match.group(3)), int(match.group(4)))
                    end_ms = time_to_ms(int(match.group(5)), int(match.group(6)),
                                        int(match.group(7)), int(match.group(8)))
                    
                    idx_val = str(len(subtitles) + 1)
                    if time_idx > 0:
                        idx_val = blk_lines[time_idx - 1]
                    subtitles.append({
                        'id': idx_val,
                        'start_ms': start_ms,
                        'end_ms': end_ms,
                        'text': text
                    })
    return subtitles

# Custom multipart form parser (no external library required)
def parse_multipart(body_bytes, boundary):
    boundary_bytes = b'--' + boundary.encode('utf-8')
    parts = body_bytes.split(boundary_bytes)
    form_data = {}
    files = {}
    
    for part in parts:
        if not part or part == b'--\r\n' or part == b'--':
            continue
        if part.startswith(b'\r\n'):
            part = part[2:]
        header_end = part.find(b'\r\n\r\n')
        if header_end == -1:
            continue
        headers = part[:header_end].decode('utf-8', errors='ignore')
        body = part[header_end+4:]
        if body.endswith(b'\r\n'):
            body = body[:-2]
            
        name_match = re.search(r'name="([^"]+)"', headers)
        if name_match:
            name = name_match.group(1)
            filename_match = re.search(r'filename="([^"]+)"', headers)
            if filename_match:
                files[name] = {
                    'filename': filename_match.group(1),
                    'content': body
                }
            else:
                form_data[name] = body.decode('utf-8', errors='ignore')
    return form_data, files

# Helper: get WAV duration accurately using WAV header (fast) or ffprobe (fallback)
def get_audio_duration_ms(ffmpeg_exe, wav_path):
    """Get accurate audio duration in ms using fast WAV header calculation first, falling back to ffprobe."""
    try:
        file_size = os.path.getsize(wav_path)
        with open(wav_path, 'rb') as f:
            f.seek(24)  # Sample rate offset in WAV header
            sample_rate = int.from_bytes(f.read(4), 'little')
            f.seek(34)  # Bits per sample offset
            bits_per_sample = int.from_bytes(f.read(2), 'little')
            f.seek(22)  # Num channels
            num_channels = int.from_bytes(f.read(2), 'little')
        bytes_per_sec = sample_rate * num_channels * (bits_per_sample // 8)
        data_bytes = max(0, file_size - 44)
        if bytes_per_sec > 0:
            return int((data_bytes / bytes_per_sec) * 1000)
    except Exception:
        pass

    # Fallback to ffprobe
    if ffmpeg_exe == "ffmpeg":
        ffprobe_exe = "ffprobe"
    else:
        ffprobe_exe = ffmpeg_exe.replace('ffmpeg', 'ffprobe')
    
    import shutil
    if shutil.which(ffprobe_exe) or os.path.exists(ffprobe_exe):
        try:
            cmd_exe = ffprobe_exe if os.path.exists(ffprobe_exe) else "ffprobe"
            result = subprocess.run(
                [cmd_exe, "-v", "quiet", "-print_format", "json",
                 "-show_streams", wav_path],
                capture_output=True, text=True, timeout=15
            )
            import json
            info = json.loads(result.stdout)
            for stream in info.get('streams', []):
                duration = stream.get('duration')
                if duration:
                    return int(float(duration) * 1000)
        except Exception:
            pass
    return 0


# Helper: get total video duration in seconds via ffprobe/ffmpeg
def get_video_duration(video_path, ffmpeg_exe):
    if ffmpeg_exe == "ffmpeg":
        ffprobe_exe = "ffprobe"
    else:
        ffprobe_exe = ffmpeg_exe.replace('ffmpeg', 'ffprobe')
    try:
        cmd = [
            ffprobe_exe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass

    # Fallback: get duration by parsing ffmpeg -i output (highly compatible)
    try:
        cmd = [ffmpeg_exe, "-i", video_path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        output = res.stderr
        match = re.search(r"Duration:\s*(\d{2}):(\d{2}):(\d{2})\.(\d{2})", output)
        if match:
            hours = int(match.group(1))
            minutes = int(match.group(2))
            seconds = int(match.group(3))
            hundredths = int(match.group(4))
            total_seconds = hours * 3600 + minutes * 60 + seconds + hundredths / 100.0
            print("[Server] Probed video duration using ffmpeg fallback:", total_seconds)
            return total_seconds
    except Exception as e:
        print("[Server] Failed probing video duration with ffmpeg fallback:", e)
    return None


# Asynchronous compiler engine — handles ONE video + ONE SRT file
async def compile_single_dub_backend(video_path, srt_path, voice, orig_vol, tts_vol, vocal_removed, speed_rate, output_path, log_callback, auto_voice=False, time_offset_ms=0):
    temp_dir = f"temp_backend_dub_{uuid.uuid4()}"
    try:
        log_callback("Reading SRT file...")
        subtitles = parse_srt(srt_path)
        
        if not subtitles:
            log_callback("Error: No subtitles found in SRT file.")
            return False
            
        # Shift subtitles if absolute timecode offset is detected
        if time_offset_ms != 0:
            for sub in subtitles:
                sub['start_ms'] = max(0, sub['start_ms'] - time_offset_ms)
                sub['end_ms'] = max(0, sub['end_ms'] - time_offset_ms)
        
        os.makedirs(temp_dir, exist_ok=True)
        
        # Check system ffmpeg first
        import shutil
        if shutil.which("ffmpeg"):
            ffmpeg_exe = "ffmpeg"
        else:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            
        # 1. Calculate overall duration
        video_dur_sec = get_video_duration(video_path, ffmpeg_exe)
        if not video_dur_sec:
            video_dur_sec = (subtitles[-1]['end_ms'] / 1000.0) + 2.0
        total_ms = int(video_dur_sec * 1000)
        
        # Each ms represents 48 bytes (24000Hz * 2 bytes/sample * 1 channel)
        bytes_per_ms = 48
        final_pcm = bytearray(total_ms * bytes_per_ms)
        
        temp_files_to_clean = []
        rate_param = f"{speed_rate:+d}%" if speed_rate != 0 else "+0%"
        
        for i, sub in enumerate(subtitles):
            log_callback(f"Generating TTS for subtitle {i+1}/{len(subtitles)}...")
            
            temp_mp3 = os.path.join(temp_dir, f"temp_tts_{i}.mp3")
            temp_files_to_clean.append(temp_mp3)
            
            seg_voice = voice
            seg_pitch = None
            sub_text = sub['text'].strip()
            
            if auto_voice:
                if "(female)" in sub_text.lower():
                    seg_voice = 'km-KH-SreymomNeural'
                    sub_text = re.sub(r'\s*\(female\)', '', sub_text, flags=re.IGNORECASE).strip()
                elif "(male)" in sub_text.lower():
                    seg_voice = 'km-KH-PisethNeural'
                    sub_text = re.sub(r'\s*\(male\)', '', sub_text, flags=re.IGNORECASE).strip()

                tag_match = re.search(r'<dubbing\s+[^>]*voice="([^"]+)"[^>]*>(.*?)</dubbing>', sub_text, re.DOTALL | re.IGNORECASE)
                if tag_match:
                    seg_voice_val = tag_match.group(1).strip()
                    sub_text = tag_match.group(2).strip()
                    
                    if seg_voice_val.lower() == 'sreymom' or 'sreymom' in seg_voice_val.lower():
                        seg_voice = 'km-KH-SreymomNeural'
                    elif seg_voice_val.lower() == 'piseth' or 'piseth' in seg_voice_val.lower():
                        seg_voice = 'km-KH-PisethNeural'
                    elif seg_voice_val.lower() == 'sokha' or 'sokha' in seg_voice_val.lower():
                        seg_voice = 'km-KH-SreymomNeural'
                    elif seg_voice_val.lower() == 'chitra' or 'chitra' in seg_voice_val.lower():
                        seg_voice = 'km-KH-PisethNeural'
                    else:
                        seg_voice = seg_voice_val
                        
                    pitch_match = re.search(r'pitch="([^"]+)"', tag_match.group(0), re.IGNORECASE)
                    if pitch_match:
                        seg_pitch = pitch_match.group(1).strip()
                        if seg_pitch.isdigit():
                            seg_pitch = f"+{seg_pitch}Hz"
                        elif seg_pitch.startswith(('-', '+')) and seg_pitch[1:].isdigit():
                            if not seg_pitch.endswith('Hz') and not seg_pitch.endswith('%'):
                                seg_pitch = f"{seg_pitch}Hz"
                    print(f"[Server Dubbing] Auto-detected tag: voice={seg_voice}, pitch={seg_pitch}")
            else:
                if "(female)" in sub_text.lower():
                    sub_text = re.sub(r'\s*\(female\)', '', sub_text, flags=re.IGNORECASE).strip()
                elif "(male)" in sub_text.lower():
                    sub_text = re.sub(r'\s*\(male\)', '', sub_text, flags=re.IGNORECASE).strip()
                sub_text = re.sub(r'<dubbing[^>]*>', '', sub_text, flags=re.IGNORECASE)
                sub_text = re.sub(r'</dubbing>', '', sub_text, flags=re.IGNORECASE)

            try:
                engine_used = await generate_tts_mp3(sub_text, seg_voice, rate_param, temp_mp3, seg_pitch)
                if engine_used != "edge-tts":
                    log_callback(f"  Info: Subtitle {i+1} used {engine_used} (edge-tts unavailable)")
            except Exception as e:
                log_callback(f"  Warning: All TTS engines failed for subtitle {i+1}: {e} — inserting silence.")
                continue

            if not os.path.exists(temp_mp3) or os.path.getsize(temp_mp3) == 0:
                log_callback(f"  Warning: Empty TTS output for subtitle {i+1}, skipping.")
                continue
            
            chunk_wav = os.path.join(temp_dir, f"chunk_{i}.wav")
            temp_files_to_clean.append(chunk_wav)
            cmd = [
                ffmpeg_exe, "-y",
                "-i", temp_mp3,
                # FIX: Keep atrim to skip MP3 encoder delay (first 1024 samples are
                # garbled transitional data from the MP3 codec warm-up).
                # Fade-in/out will be applied in Python directly on the PCM buffer below.
                "-filter:a", "atrim=start_sample=1024,asetpts=PTS-STARTPTS,aresample=24000",
                "-ar", "24000",
                "-ac", "1",
                "-acodec", "pcm_s16le",
                chunk_wav
            ]
            transcode_result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            
            try:
                os.remove(temp_mp3)
            except Exception:
                pass
            
            if transcode_result.returncode != 0 or not os.path.exists(chunk_wav):
                log_callback(f"  Warning: Transcode failed for subtitle {i+1}, skipping.")
                continue
            
            chunk_duration_ms = get_audio_duration_ms(ffmpeg_exe, chunk_wav)
            if chunk_duration_ms <= 0:
                log_callback(f"  Warning: Could not determine duration for subtitle {i+1}, skipping.")
                continue
            
            allowed_duration_ms = max(sub['end_ms'] - sub['start_ms'], 300)
            
            if allowed_duration_ms > 100:
                tempo = chunk_duration_ms / allowed_duration_ms
                if tempo > 1.05:
                    tempo = min(1.8, tempo)
                    speeded_wav = os.path.join(temp_dir, f"chunk_speed_{i}.wav")
                    temp_files_to_clean.append(speeded_wav)
                    print(f"[Server Dubbing] Subtitle {i+1} duration too long ({chunk_duration_ms}ms > {allowed_duration_ms}ms). Speeding up to {tempo:.2f}x...")
                    speed_cmd = [
                        ffmpeg_exe, "-y",
                        "-i", chunk_wav,
                        "-filter:a", f"atempo={tempo:.3f}",
                        "-ar", "24000",
                        "-ac", "1",
                        "-acodec", "pcm_s16le",
                        speeded_wav
                    ]
                    speed_result = subprocess.run(speed_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    if speed_result.returncode == 0 and os.path.exists(speeded_wav):
                        chunk_wav = speeded_wav
            
            with open(chunk_wav, 'rb') as wf:
                wf.seek(44)
                raw_pcm = wf.read()

            start_byte = sub['start_ms'] * bytes_per_ms
            write_len = min(len(raw_pcm), len(final_pcm) - start_byte)

            if write_len > 0 and write_len % 2 == 0:
                import struct, math
                n_samples = write_len // 2
                new_s = list(struct.unpack(f'<{n_samples}h', raw_pcm[:write_len]))

                # IMPROVED FIX: 60ms cosine fade-in + fade-out on raw PCM samples.
                # Cosine curve has zero derivative at both ends = no abrupt slope change
                # = much smoother silence→speech transition than linear fade.
                FADE_SAMPLES = 1440  # 60ms at 24000Hz

                # Cosine fade-in: smooth ramp 0 → full
                fade_in_len = min(FADE_SAMPLES, n_samples)
                for fi in range(fade_in_len):
                    factor = (1.0 - math.cos(math.pi * fi / FADE_SAMPLES)) / 2.0
                    new_s[fi] = int(new_s[fi] * factor)

                # Cosine fade-out: smooth ramp full → 0
                fade_out_len = min(FADE_SAMPLES, n_samples)
                for fo in range(fade_out_len):
                    idx = n_samples - 1 - fo
                    if idx >= fade_in_len:  # Don't overlap with fade-in region
                        factor = (1.0 - math.cos(math.pi * fo / FADE_SAMPLES)) / 2.0
                        new_s[idx] = int(new_s[idx] * factor)

                # Mix additively with clamping
                existing = struct.unpack_from(f'<{n_samples}h', final_pcm, start_byte)
                mixed = struct.pack(
                    f'<{n_samples}h',
                    *[max(-32768, min(32767, e + n)) for e, n in zip(existing, new_s)]
                )
                final_pcm[start_byte : start_byte + write_len] = mixed

        log_callback("Combining voice tracks...")
        tts_full_wav = os.path.join(temp_dir, "tts_full.wav")
        temp_files_to_clean.append(tts_full_wav)
        
        import struct
        num_samples = len(final_pcm) // 2
        num_channels = 1
        bits_per_sample = 16
        sample_rate = 24000
        byte_rate = sample_rate * num_channels * (bits_per_sample // 8)
        block_align = num_channels * (bits_per_sample // 8)
        data_size = num_samples * block_align
        file_size = 36 + data_size
        
        wav_header = struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', file_size, b'WAVE', b'fmt ', 16, 1, num_channels,
            sample_rate, byte_rate, block_align, bits_per_sample, b'data', data_size
        )
        
        with open(tts_full_wav, 'wb') as out_f:
            out_f.write(wav_header)
            out_f.write(final_pcm)

        # Pre-resample TTS WAV to 44100Hz stereo BEFORE mixing.
        # This prevents the FFmpeg resampler from creating ringing artifacts
        # at silence→speech boundaries during real-time conversion in the mix step.
        tts_wav_44100 = os.path.join(temp_dir, "tts_full_44100.wav")
        temp_files_to_clean.append(tts_wav_44100)
        preresample_cmd = [
            ffmpeg_exe, "-y",
            "-i", tts_full_wav,
            "-ar", "44100",
            "-ac", "2",
            "-acodec", "pcm_s16le",
            tts_wav_44100
        ]
        pre_res = subprocess.run(preresample_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if pre_res.returncode != 0 or not os.path.exists(tts_wav_44100):
            # Fallback to original if resample fails
            tts_wav_44100 = tts_full_wav
        
        # Check if original video has an audio stream
        has_audio = False
        try:
            cmd = [ffmpeg_exe, "-i", video_path]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if "Audio:" in res.stderr:
                has_audio = True
        except Exception as e:
            print("Failed probing audio streams:", e)
            has_audio = True # Default fallback
            
        if not has_audio:
            log_callback("Original video has no audio stream. Mapping translator track directly.")
                
        # Audio Mix and Video Render
        log_callback("Merging audio into video stream...")
        orig_vol_ratio = orig_vol / 100.0
        tts_vol_ratio = tts_vol / 100.0
        
        if vocal_removed:
            orig_vol_ratio *= 0.15
            
        if has_audio:
            cmd = [
                ffmpeg_exe, "-y",
                "-i", video_path,
                "-i", tts_wav_44100,   # Use pre-resampled 44100Hz stereo file
                "-filter_complex",
                # TTS is already 44100Hz stereo — only need volume + mix.
                # No resampler artifacts at silence boundaries.
                f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={orig_vol_ratio:.3f}[orig]; "
                f"[1:a]volume={tts_vol_ratio:.3f}[tts]; "
                f"[orig][tts]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]",
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "44100",
                "-ac", "2",
                output_path
            ]
        else:
            # Silent video - use pre-resampled TTS track directly
            cmd = [
                ffmpeg_exe, "-y",
                "-i", video_path,
                "-i", tts_wav_44100,   # Pre-resampled 44100Hz stereo
                "-filter_complex",
                f"[1:a]volume={tts_vol_ratio:.3f}[aout]",
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-b:a", "192k",
                "-ar", "44100",
                "-ac", "2",
                output_path
            ]
        
        process = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600)
        
        if process.returncode != 0:
            log_callback(f"Error: FFmpeg video mux failed: {process.stderr.decode(errors='ignore')[-500:]}")
            return False
        
        return True
        
    except asyncio.TimeoutError:
        log_callback("Fatal error: Operation timed out.")
        return False
    except Exception as e:
        log_callback(f"Compiler backend error: {e}")
        return False
    finally:
        # Always cleanup temp dir reliably using shutil.rmtree
        log_callback("Cleaning up temporary files...")
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


# ── Orchestrator: splits video when multiple SRT files provided ──────────────
async def compile_dubbed_video_backend(
    video_path, srt_paths_joined, voice, orig_vol, tts_vol,
    vocal_removed, speed_rate, output_path, log_callback, auto_voice=False
):
    import shutil as _shutil

    # Check system ffmpeg first
    if shutil.which("ffmpeg"):
        ffmpeg_exe = "ffmpeg"
    else:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    video_paths = [p.strip() for p in video_path.split(';') if p.strip()]
    srt_paths = [p.strip() for p in srt_paths_joined.split(';') if p.strip()]

    # ── MULTI-VIDEO BATCH MODE: dub each video file with matching SRT and concat ──
    if len(video_paths) > 1:
        num_vids = len(video_paths)
        log_callback(f"🎬 រកឃើញវីដេអូចំនួន {num_vids} និង SRT ចំនួន {len(srt_paths)} (Batch Mode)...")

        batch_temp_dir = f"temp_batch_srv_{uuid.uuid4()}"
        os.makedirs(batch_temp_dir, exist_ok=True)
        part_files = []
        base_out, ext_out = os.path.splitext(output_path)

        try:
            for idx, current_video in enumerate(video_paths):
                part_num = idx + 1
                current_srt = srt_paths[idx] if idx < len(srt_paths) else srt_paths[min(idx, len(srt_paths) - 1)]
                log_callback(f"\n📂 [វីដេអូទី {part_num}/{num_vids}] កំពុងដំណើរការ...")

                part_output = f"{base_out}_vid{part_num}{ext_out}"
                success = await compile_single_dub_backend(
                    current_video, current_srt, voice, orig_vol, tts_vol,
                    vocal_removed, speed_rate, part_output, log_callback, auto_voice
                )
                if not success:
                    log_callback(f"❌ បញ្ចូលសំឡេងវីដេអូទី {part_num} បរាជ័យ")
                    return False

                log_callback(f"✅ វីដេអូទី {part_num} រួចរាល់")
                part_files.append(part_output)

            # Concat all dubbed videos into final output
            log_callback("\n🔗 កំពុងរួមបញ្ចូលវីដេអូទាំងអស់...")
            concat_txt = os.path.join(batch_temp_dir, "concat_parts.txt")
            with open(concat_txt, 'w', encoding='utf-8') as f:
                for pf in part_files:
                    escaped = os.path.abspath(pf).replace('\\', '/')
                    f.write(f"file '{escaped}'\n")

            concat_cmd = [
                ffmpeg_exe, "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_txt,
                "-c", "copy",
                output_path
            ]
            res = subprocess.run(
                concat_cmd,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=600
            )
            if res.returncode == 0:
                log_callback(f"🎉 ជោគជ័យ! បង្កើតវីដេអូសរុប {num_vids} ផ្នែករួចរាល់")
                return True
            else:
                log_callback("⚠️ Re-encoding concat...")
                concat_cmd2 = [
                    ffmpeg_exe, "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", concat_txt,
                    "-c:v", "libx264", "-c:a", "aac",
                    output_path
                ]
                res2 = subprocess.run(concat_cmd2, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=900)
                if res2.returncode == 0:
                    log_callback(f"🎉 ជោគជ័យ! បង្កើតវីដេអូសរុប {num_vids} ផ្នែករួចរាល់")
                    return True
                err = res.stderr.decode('utf-8', errors='ignore')
                log_callback(f"❌ កំហុសរួមបញ្ចូល: {err[:200]}")
                return False
        finally:
            _shutil.rmtree(batch_temp_dir, ignore_errors=True)
            for pf in part_files:
                try: os.remove(pf)
                except: pass

    # ── MULTI-SRT MODE: split single video into equal parts, dub each part ──
    if len(srt_paths) > 1:
        total_duration = get_video_duration(video_path, ffmpeg_exe)
        if total_duration is None:
            log_callback("Cannot get video duration — falling back to single-SRT mode with first SRT file only.")
            return await compile_single_dub_backend(
                video_path, srt_paths[0], voice, orig_vol, tts_vol,
                vocal_removed, speed_rate, output_path, log_callback, auto_voice
            )
        else:
            num_parts = len(srt_paths)
            part_duration = total_duration / num_parts
            log_callback(
                f"🎬 រកឃើញ SRT ចំនួន {num_parts}។ "
                f"នឹងកាត់វីដេអូជា {num_parts} ផ្នែក "
                f"(មួយផ្នែកៗ {part_duration:.2f} វិនាទី)..."
            )

            split_temp_dir = f"temp_split_srv_{uuid.uuid4()}"
            os.makedirs(split_temp_dir, exist_ok=True)
            part_files = []
            temp_video_parts = []
            base_out, ext_out = os.path.splitext(output_path)

            try:
                for idx, current_srt in enumerate(srt_paths):
                    part_num = idx + 1
                    log_callback(f"\n📂 [ផ្នែកទី {part_num}/{num_parts}] កំពុងដំណើរការ...")

                    # 1. Split video segment
                    start_sec = idx * part_duration
                    end_sec = (idx + 1) * part_duration
                    temp_video_part = os.path.join(split_temp_dir, f"video_part_{idx}.mp4")
                    temp_video_parts.append(temp_video_part)

                    log_callback(f"✂️ កំពុងកាត់វីដេអូ {start_sec:.2f}s → {end_sec:.2f}s...")
                    split_cmd = [
                        ffmpeg_exe, "-y",
                        "-ss", f"{start_sec:.3f}",
                        "-to", f"{end_sec:.3f}",
                        "-i", video_path,
                        "-c:v", "copy",
                        "-c:a", "aac",
                        temp_video_part
                    ]
                    res = subprocess.run(
                        split_cmd,
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300
                    )
                    if res.returncode != 0:
                        err = res.stderr.decode('utf-8', errors='ignore')
                        log_callback(f"❌ កំហុសកាត់វីដេអូផ្នែកទី {part_num}: {err[:200]}")
                        return False

                    # 2. Dub this video segment with its SRT
                    part_output = f"{base_out}_part{part_num}{ext_out}"
                    
                    time_offset_ms = 0
                    try:
                        temp_subs = parse_srt(current_srt)
                        if temp_subs:
                            first_start = temp_subs[0]['start_ms']
                            start_ms = int(start_sec * 1000)
                            if first_start >= start_ms - 5000:
                                time_offset_ms = start_ms
                                log_callback(f"⚙️ បានរកឃើញកូដម៉ោង Absolute ក្នុង SRT — ធ្វើការលៃតម្រូវកូដម៉ោងឱ្យត្រូវនឹងផ្នែកទី {part_num} (-{start_sec:.1f}s)...")
                    except Exception as e:
                        print("Failed checking srt offset:", e)

                    success = await compile_single_dub_backend(
                        temp_video_part, current_srt, voice, orig_vol, tts_vol,
                        vocal_removed, speed_rate, part_output, log_callback, auto_voice,
                        time_offset_ms=time_offset_ms
                    )
                    if not success:
                        log_callback(f"❌ បញ្ចូលសំឡេងផ្នែកទី {part_num} បរាជ័យ")
                        return False

                    log_callback(f"✅ ផ្នែកទី {part_num} រួចរាល់")
                    part_files.append(part_output)

                # 3. Concat all dubbed parts into final output
                log_callback("\n🔗 កំពុងរួមបញ្ចូលវីដេអូទាំងអស់...")
                concat_txt = os.path.join(split_temp_dir, "concat_parts.txt")
                with open(concat_txt, 'w', encoding='utf-8') as f:
                    for pf in part_files:
                        escaped = os.path.abspath(pf).replace('\\', '/')
                        f.write(f"file '{escaped}'\n")

                concat_cmd = [
                    ffmpeg_exe, "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", concat_txt,
                    "-c", "copy",
                    output_path
                ]
                res = subprocess.run(
                    concat_cmd,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300
                )
                if res.returncode == 0:
                    log_callback(f"🎉 ជោគជ័យ! វីដេអូពេញលេញរួចរាល់")
                    return True
                else:
                    err = res.stderr.decode('utf-8', errors='ignore')
                    log_callback(f"❌ កំហុសរួមបញ្ចូល: {err[:200]}")
                    return False

            finally:
                # Cleanup split temp dir and part files
                _shutil.rmtree(split_temp_dir, ignore_errors=True)
                for pf in part_files:
                    try: os.remove(pf)
                    except: pass

    # ── SINGLE SRT MODE (default or when only 1 SRT given) ──────────────────────
    return await compile_single_dub_backend(
        video_path, srt_paths[0], voice, orig_vol, tts_vol,
        vocal_removed, speed_rate, output_path, log_callback, auto_voice
    )


def start_compilation_thread(task_id, video_path, srt_paths_joined, voice, orig_vol, tts_vol, vocal_removed, speed_rate, output_path, auto_voice, orig_filename):
    loop = asyncio.new_event_loop()
    
    def log_callback(msg):
        update_task_progress(task_id, msg)
        
    coro = compile_dubbed_video_backend(
        video_path, srt_paths_joined, voice,
        orig_vol, tts_vol, vocal_removed, speed_rate,
        output_path, log_callback, auto_voice
    )
    
    def run():
        asyncio.set_event_loop(loop)
        try:
            success = loop.run_until_complete(coro)
            if success and os.path.exists(output_path):
                # Copy to permanent location
                server_output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dubbed_outputs")
                os.makedirs(server_output_dir, exist_ok=True)
                
                import datetime
                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                orig_name = "video"
                if orig_filename:
                    dot_idx = orig_filename.rfind('.')
                    if dot_idx != -1:
                        orig_name = orig_filename[:dot_idx]
                    else:
                        orig_name = orig_filename
                
                safe_orig_name = re.sub(r'[^a-zA-Z0-9_\u1780-\u17f9\-]', '_', orig_name)
                server_output_filename = f"{safe_orig_name}_dubbed_{timestamp}.mp4"
                server_output_file = os.path.join(server_output_dir, server_output_filename)
                
                shutil.copy2(output_path, server_output_file)
                
                active_tasks[task_id]['server_output_file'] = server_output_file
                active_tasks[task_id]['output_video_path'] = output_path
                active_tasks[task_id]['progress_pct'] = 100
                active_tasks[task_id]['status'] = 'completed'
                print(f"[Server Task {task_id}] Successfully finished. Output: {server_output_file}")
            else:
                active_tasks[task_id]['status'] = 'failed'
                last_logs = "\n".join(active_tasks[task_id]['logs'][-8:]) if active_tasks[task_id]['logs'] else "No logs."
                active_tasks[task_id]['error'] = f"Compilation failed. Log tail:\n{last_logs}"
                print(f"[Server Task {task_id}] Compilation returned False.")
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(f"[Server Task {task_id}] Exception:\n{tb}")
            active_tasks[task_id]['status'] = 'failed'
            active_tasks[task_id]['error'] = f"Server error: {type(e).__name__}: {e}"
        finally:
            try:
                loop.close()
            except:
                pass

    t = threading.Thread(target=run)
    t.daemon = True
    t.start()


# Custom HTTP Handler class
class DubbingHandler(http.server.SimpleHTTPRequestHandler):
    
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')
        print(f"[Server Request] GET {path}")

        # --- /api/status: Polls progress and status of a dubbing task ---
        if path == '/api/status':
            query = urllib.parse.parse_qs(parsed_url.query)
            task_id = query.get('task_id', [''])[0]
            if not task_id:
                self.send_error(400, "Missing 'task_id' query parameter.")
                return
            if task_id not in active_tasks:
                self.send_error(404, f"Task {task_id} not found.")
                return
            
            task = active_tasks[task_id]
            status_data = {
                "status": task['status'],
                "progress_pct": task['progress_pct'],
                "message": task['message'],
                "error": task['error']
            }
            body = json.dumps(status_data, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- /api/download: Downloads completed dubbing task video ---
        if parsed_url.path == '/api/download':
            query = urllib.parse.parse_qs(parsed_url.query)
            task_id = query.get('task_id', [''])[0]
            if not task_id:
                self.send_error(400, "Missing 'task_id' query parameter.")
                return
            if task_id not in active_tasks:
                self.send_error(404, f"Task {task_id} not found.")
                return
            
            task = active_tasks[task_id]
            if task['status'] != 'completed':
                self.send_error(400, f"Task {task_id} is in status '{task['status']}' (not completed).")
                return
                
            filepath = task['output_video_path']
            server_output_file = task['server_output_file']
            if not filepath or not os.path.exists(filepath):
                self.send_error(404, "Dubbed video file not found on server.")
                return
                
            file_size = os.path.getsize(filepath)
            self.send_response(200)
            self.send_header('Content-Type', 'video/mp4')
            self.send_header('Content-Length', str(file_size))
            self.send_header('X-Output-Path', urllib.parse.quote(server_output_file))
            self.send_header('Access-Control-Expose-Headers', 'X-Output-Path')
            self.end_headers()
            
            try:
                with open(filepath, 'rb') as f:
                    self.wfile.write(f.read())
            except Exception as e:
                print("Failed sending download file:", e)
                return
                
            # Cleanup local task data and temporary directory after transfer
            active_tasks.pop(task_id, None)
            return

        # --- /api/engine-status: Returns current TTS engine and availability ---
        if parsed_url.path == '/api/engine-status':
            status = {
                "edge_tts_available": True,  # Always importable; network may fail at runtime
                "gtts_available": GTTS_AVAILABLE,
                "active_engine": active_tts_engine,
                "voices": {
                    "Piseth":  "km-KH-PisethNeural  (Edge TTS - Male)",
                    "Sreymom": "km-KH-SreymomNeural (Edge TTS - Female)",
                    "Sokha":   "km-KH-SreymomNeural (Edge TTS - Female / alias)",
                    "Chitra":  "km-KH-PisethNeural  (Edge TTS - Male  / alias)"
                }
            }
            body = json.dumps(status, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- /api/test-voice: Quickly tests if edge-tts can reach Microsoft servers ---
        if parsed_url.path == '/api/test-voice':
            test_text = "សួស្តី"
            test_file = f"test_voice_{os.getpid()}.mp3"
            result = {"edge_tts": False, "gtts": False, "recommended": "gtts"}
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                engine = loop.run_until_complete(
                    generate_tts_mp3(test_text, "km-KH-PisethNeural", "+0%", test_file)
                )
                result["edge_tts"] = (engine == "edge-tts")
                result["gtts"] = (engine == "gtts") or GTTS_AVAILABLE
                result["recommended"] = engine
                result["message"] = f"TTS test successful using: {engine}"
            except Exception as e:
                result["message"] = f"Both TTS engines failed: {e}"
            finally:
                loop.close()
                try: os.remove(test_file)
                except: pass
            body = json.dumps(result, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- /api/tts: Real-time TTS preview (edge-tts → gTTS fallback) ---
        if parsed_url.path == '/api/tts':
            query = urllib.parse.parse_qs(parsed_url.query)
            text = query.get('text', [''])[0]
            voice_param = query.get('voice', ['Piseth'])[0]
            pitch_param = query.get('pitch', [None])[0]

            if not text:
                self.send_error(400, "Missing 'text' query parameter.")
                return

            voice_map = {
                'Piseth':  'km-KH-PisethNeural',
                'Sreymom': 'km-KH-SreymomNeural',
                'Sokha':   'km-KH-SreymomNeural',
                'Chitra':  'km-KH-PisethNeural'
            }
            # Handle full voice name keys passed directly (from auto-detected tags)
            if voice_param in voice_map.values():
                edge_voice = voice_param
            else:
                edge_voice = voice_map.get(voice_param, 'km-KH-SreymomNeural')
                
            print(f"[TTS Preview] voice={edge_voice} pitch={pitch_param} len={len(text)}")

            temp_file = f"temp_tts_{os.getpid()}_{abs(hash(text))}.mp3"
            loop = asyncio.new_event_loop()
            engine_used = "unknown"
            try:
                asyncio.set_event_loop(loop)
                engine_used = loop.run_until_complete(
                    generate_tts_mp3(text, edge_voice, "+0%", temp_file, pitch_param)
                )
                print(f"[TTS Preview] engine={engine_used}")
            except Exception as e:
                print(f"[TTS Preview] All engines failed: {e}")
                self.send_error(500, f"TTS generation failed: {e}")
                return
            finally:
                loop.close()

            if os.path.exists(temp_file) and os.path.getsize(temp_file) > 0:
                file_size = os.path.getsize(temp_file)
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Length', str(file_size))
                self.send_header('X-TTS-Engine', engine_used)  # Let browser know which engine
                self.end_headers()
                with open(temp_file, 'rb') as f:
                    self.wfile.write(f.read())
                try: os.remove(temp_file)
                except: pass
            else:
                self.send_error(500, "Preview audio output file not found.")
            return

        super().do_GET()


    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.rstrip('/')
        print(f"[Server Request] POST {path}")
        
        # Route the real video dubbing API
        if path == '/api/dub':
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self.send_error(400, "Content-Type must be multipart/form-data")
                return
                
            boundary_match = re.search(r'boundary=([^;]+)', content_type)
            if not boundary_match:
                self.send_error(400, "Missing boundary in Content-Type")
                return
            boundary = boundary_match.group(1).strip().strip('"')
            
            # Read post body bytes
            content_length = int(self.headers.get('Content-Length', 0))
            body_bytes = self.rfile.read(content_length)
            
            # Parse form fields and files
            form_data, files = parse_multipart(body_bytes, boundary)
            
            # Extract variables
            voice_param = form_data.get('voice', 'Sreymom')
            orig_vol = int(form_data.get('orig_vol', '15'))
            tts_vol = int(form_data.get('tts_vol', '100'))
            vocal_removed = form_data.get('vocal_removed', 'false').lower() == 'true'
            auto_voice = form_data.get('auto_voice', 'false').lower() == 'true'
            
            # Calculate speed rate float to percentage change
            speed_val = form_data.get('speed_rate', '1.0')
            speed_rate = 0
            try:
                rate_float = float(speed_val)
                if rate_float != 1.0:
                    speed_rate = int((rate_float - 1.0) * 100)
            except (ValueError, TypeError):
                pass
            
            # Choose Edge TTS voice ID
            voice_map = {
                'Piseth': 'km-KH-PisethNeural',
                'Sreymom': 'km-KH-SreymomNeural',
                'Sokha': 'km-KH-SreymomNeural',
                'Chitra': 'km-KH-PisethNeural'
            }
            voice = voice_map.get(voice_param, 'km-KH-SreymomNeural')
            
            # Setup sandbox directories
            temp_dir = f"temp_srv_dub_{uuid.uuid4()}"
            os.makedirs(temp_dir, exist_ok=True)

            input_video_path = os.path.join(temp_dir, "input_video.mp4")
            output_video_path = os.path.join(temp_dir, "output_dubbed.mp4")

            # ── Handle video: support single or multiple uploads (video, video_0, video_1, …) ──
            video_file_paths = []

            # Primary key 'video'
            if 'video' in files:
                v_save_path = os.path.join(temp_dir, "input_video_0.mp4")
                with open(v_save_path, 'wb') as f:
                    f.write(files['video']['content'])
                video_file_paths.append(v_save_path)

            # Additional video keys: video_0, video_1, video_2, …
            v_idx = 0
            while True:
                v_key = f"video_{v_idx}"
                if v_key not in files:
                    break
                v_save_path = os.path.join(temp_dir, f"input_video_extra_{v_idx}.mp4")
                with open(v_save_path, 'wb') as f:
                    f.write(files[v_key]['content'])
                if not (v_idx == 0 and 'video' in files):
                    video_file_paths.append(v_save_path)
                v_idx += 1

            if video_file_paths:
                input_video_path = ";".join(video_file_paths)
                print(f"[Server Dubbing] Video files ({len(video_file_paths)}): {input_video_path}")
            elif form_data.get('video_url'):
                input_video_path = os.path.join(temp_dir, "input_video.mp4")
                video_url = form_data['video_url'].strip()
                print(f"[Server Dubbing] Downloading video from URL: {video_url}...")
                try:
                    req = urllib.request.Request(
                        video_url,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                    )
                    with urllib.request.urlopen(req, timeout=180) as response, open(input_video_path, 'wb') as out_file:
                        shutil.copyfileobj(response, out_file)
                    print("[Server Dubbing] Video downloaded successfully.")
                except Exception as e:
                    print("[Server Dubbing] Failed downloading video from URL:", e)
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    self.send_error(500, f"Failed to download video from URL: {e}")
                    return
            else:
                input_video_path = os.path.join(temp_dir, "input_video.mp4")
                demo_cache_path = "demo_video_cache.mp4"
                if not os.path.exists(demo_cache_path):
                    print("[Server Dubbing] Downloading sample video for Demo Mode cache...")
                    try:
                        req = urllib.request.Request(
                            "https://www.w3schools.com/html/mov_bbb.mp4",
                            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                        )
                        with urllib.request.urlopen(req, timeout=180) as response, open(demo_cache_path, 'wb') as out_file:
                            shutil.copyfileobj(response, out_file)
                    except Exception as e:
                        print("[Server Dubbing] Failed downloading demo video cache:", e)
                        shutil.rmtree(temp_dir, ignore_errors=True)
                        self.send_error(500, f"Demo file download failed: {e}")
                        return
                shutil.copy(demo_cache_path, input_video_path)

            # ── Handle SRT: support multiple uploads (srt, srt_0, srt_1, …) ──
            # Collect all SRT file keys in upload order
            srt_file_paths = []

            # Primary key 'srt' (single SRT or first SRT)
            if 'srt' in files:
                srt_save_path = os.path.join(temp_dir, "input_srt_0.srt")
                with open(srt_save_path, 'wb') as f:
                    f.write(files['srt']['content'])
                srt_file_paths.append(srt_save_path)

            # Additional SRT keys: srt_0, srt_1, srt_2, …
            idx = 0
            while True:
                key = f"srt_{idx}"
                if key not in files:
                    break
                srt_save_path = os.path.join(temp_dir, f"input_srt_extra_{idx}.srt")
                with open(srt_save_path, 'wb') as f:
                    f.write(files[key]['content'])
                # Only append if not already added via 'srt' key
                if not (idx == 0 and 'srt' in files):
                    srt_file_paths.append(srt_save_path)
                idx += 1

            if not srt_file_paths:
                shutil.rmtree(temp_dir, ignore_errors=True)
                self.send_error(400, "Missing SRT subtitles payload")
                return

            # Join all SRT paths with semicolon for the backend orchestrator
            input_srt_joined = ";".join(srt_file_paths)
            print(f"[Server Dubbing] SRT files ({len(srt_file_paths)}): {input_srt_joined}")
                
            # Generate task_id and register it in active_tasks
            task_id = str(uuid.uuid4())
            active_tasks[task_id] = {
                'status': 'running',
                'progress_pct': 0,
                'message': 'Starting compilation...',
                'logs': [],
                'server_output_file': None,
                'output_video_path': None,
                'temp_dir': temp_dir,
                'error': None
            }

            orig_filename = None
            if 'video' in files:
                orig_filename = files['video']['filename']

            start_compilation_thread(
                task_id, input_video_path, input_srt_joined, voice,
                orig_vol, tts_vol, vocal_removed, speed_rate,
                output_video_path, auto_voice, orig_filename
            )

            # Return task_id immediately
            response_data = {"task_id": task_id}
            body = json.dumps(response_data).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Route the translate API
        elif path == '/api/translate':
            content_length = int(self.headers.get('Content-Length', 0))
            body_bytes = self.rfile.read(content_length)
            try:
                data = json.loads(body_bytes.decode('utf-8'))
                srt_content = data.get('srt', '')
                gemini_key = data.get('gemini_key', '').strip()
                if not srt_content:
                    self.send_error(400, "Missing srt content")
                    return
                
                blocks = srt_content.strip().split('\n\n')
                parsed_blocks = []
                for idx_b, block in enumerate(blocks):
                    lines = block.split('\n')
                    if len(lines) >= 3:
                        idx = lines[0]
                        time_line = lines[1]
                        text = "\n".join(lines[2:])
                        tag = ""
                        clean_text = text.strip()
                        if clean_text.lower().endswith("(female)"):
                            tag = " (Female)"
                            clean_text = clean_text[:-8].strip()
                        elif clean_text.lower().endswith("(male)"):
                            tag = " (Male)"
                            clean_text = clean_text[:-6].strip()
                        parsed_blocks.append({"idx": idx, "time_line": time_line, "clean_text": clean_text, "tag": tag, "valid": True})
                    else:
                        parsed_blocks.append({"block": block, "valid": False})
                
                original_lines = [b["clean_text"] for b in parsed_blocks if b["valid"] and b["clean_text"]]
                translated_map = {}
                
                # ── Gemini Translation (high quality) ──────────────────────────
                if gemini_key and original_lines:
                    try:
                        srt_payload = ""
                        for i, b in enumerate([b for b in parsed_blocks if b["valid"] and b["clean_text"]]):
                            m = i // 60
                            s = i % 60
                            srt_payload += f"{i+1}\n00:{m:02d}:{s:02d},000 --> 00:{m:02d}:{s:02d},999\n{b['clean_text']}\n\n"
                        
                        prompt = (
                            "You are a professional movie script writer and translator specializing in Khmer movie dubbing (បញ្ចូលសំឡេងភាពយន្ត).\n"
                            "Translate the following subtitles into natural, conversational, and contextually fluent Khmer (ភាសាខ្មែរ).\n\n"
                            "Requirements:\n"
                            "1. Cinematic Tone: Make the Khmer translation sound like real spoken dialogues in a movie. Avoid formal or robotic translations.\n"
                            "2. Appropriate Pronouns: Choose matching Khmer pronouns (e.g., ខ្ញុំ, បង, អូន, ឯង, លោក, ម៉ាក់, ប៉ា) based on the context so the characters sound natural speaking to each other.\n"
                            "3. Idiom Translation: Translate idioms, slang, and casual phrases into their natural Khmer equivalents rather than word-for-word.\n"
                            "4. Strict Format:\n"
                            "   - Translate ONLY the subtitle text lines.\n"
                            "   - Keep all SRT index numbers and timestamps EXACTLY as they are.\n"
                            "   - Return ONLY the clean, raw translated SRT content. Do NOT include any intro, notes, or code blocks.\n\n"
                            f"SRT subtitles to translate:\n{srt_payload}"
                        )
                        
                        gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
                        gemini_payload = json.dumps({
                            "contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192}
                        }).encode('utf-8')
                        gemini_req = urllib.request.Request(gemini_url, data=gemini_payload,
                            headers={"Content-Type": "application/json"}, method="POST")
                        with urllib.request.urlopen(gemini_req, timeout=60) as gemini_resp:
                            gemini_result = json.loads(gemini_resp.read().decode('utf-8'))
                            translated_srt_text = gemini_result['candidates'][0]['content']['parts'][0]['text'].strip()
                        
                        gemini_blocks = translated_srt_text.strip().split('\n\n')
                        translated_lines = []
                        for blk in gemini_blocks:
                            blk_lines = blk.strip().split('\n')
                            if len(blk_lines) >= 3:
                                translated_lines.append('\n'.join(blk_lines[2:]).strip())
                            elif len(blk_lines) == 2 and '-->' in blk_lines[1]:
                                translated_lines.append('')
                        
                        if len(translated_lines) == len(original_lines):
                            line_idx = 0
                            for b in parsed_blocks:
                                if b["valid"] and b["clean_text"]:
                                    translated_map[b["clean_text"]] = translated_lines[line_idx]
                                    line_idx += 1
                            print("[Server Translate] Gemini translation successful!")
                        else:
                            raise ValueError(f"Gemini line count mismatch: {len(translated_lines)} vs {len(original_lines)}")
                    except Exception as gemini_err:
                        print(f"[Server Translate] Gemini failed: {gemini_err}. Falling back to Google Translate...")
                        translated_map = {}

                # ── Google Translate fallback ───────────────────────────────────
                if not translated_map and original_lines:
                    payload = "\n".join(original_lines)
                    try:
                        # Try high-quality Neural mobile Google Translate endpoint first
                        full_translation = None
                        try:
                            url = "https://translate.google.com/m?sl=auto&tl=km&q=" + urllib.parse.quote(payload)
                            req = urllib.request.Request(url, headers={
                                'User-Agent': 'Mozilla/5.0 (Linux; Android 10; SM-A205U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36'
                            })
                            with urllib.request.urlopen(req, timeout=15) as response:
                                html_content = response.read().decode('utf-8')
                                match = re.search(r'class="result-container">([^<]+)', html_content)
                                if not match:
                                    match = re.search(r'class="t0">([^<]+)', html_content)
                                if match:
                                    import html as html_parser
                                    full_translation = html_parser.unescape(match.group(1).strip())
                        except Exception as premium_err:
                            print("[Server Translate] Premium batch translation failed:", premium_err)

                        if not full_translation:
                            # Fallback legacy gtx
                            url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=km&dt=t&q=" + urllib.parse.quote(payload)
                            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                            with urllib.request.urlopen(req, timeout=15) as response:
                                res_data = response.read().decode('utf-8')
                                parsed_json = json.loads(res_data)
                                full_translation = "".join(item[0] for item in parsed_json[0] if item[0])
                        
                        translated_lines = [line.strip() for line in full_translation.split('\n')]
                        
                        while len(translated_lines) > len(original_lines):
                            if not translated_lines[-1]:
                                translated_lines.pop()
                            else:
                                break
                                
                        if len(translated_lines) == len(original_lines):
                            line_idx = 0
                            for b in parsed_blocks:
                                if b["valid"] and b["clean_text"]:
                                    translated_map[b["clean_text"]] = translated_lines[line_idx]
                                    line_idx += 1
                        else:
                            raise ValueError("Line count mismatch in batch translation")
                    except Exception as e:
                        print("[Server Translate] Batch failed, falling back to line-by-line:", e)
                        for b in parsed_blocks:
                            if b["valid"] and b["clean_text"]:
                                clean_text = b["clean_text"]
                                translated_text = None
                                
                                # Try premium first
                                try:
                                    url = "https://translate.google.com/m?sl=auto&tl=km&q=" + urllib.parse.quote(clean_text)
                                    req = urllib.request.Request(url, headers={
                                        'User-Agent': 'Mozilla/5.0 (Linux; Android 10; SM-A205U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36'
                                    })
                                    with urllib.request.urlopen(req, timeout=8) as response:
                                        html_content = response.read().decode('utf-8')
                                        match = re.search(r'class="result-container">([^<]+)', html_content)
                                        if not match:
                                            match = re.search(r'class="t0">([^<]+)', html_content)
                                        if match:
                                            import html as html_parser
                                            translated_text = html_parser.unescape(match.group(1).strip())
                                except Exception:
                                    pass
                                    
                                if not translated_text:
                                    # Fallback legacy
                                    try:
                                        url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=km&dt=t&q=" + urllib.parse.quote(clean_text)
                                        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                                        with urllib.request.urlopen(req, timeout=8) as response:
                                            res_data = response.read().decode('utf-8')
                                            parsed_json = json.loads(res_data)
                                            translated_text = "".join(item[0] for item in parsed_json[0] if item[0])
                                            translated_text = translated_text.strip()
                                    except Exception as err:
                                        print("Server single translate err:", err)
                                        translated_text = clean_text
                                        
                                translated_map[clean_text] = translated_text
                
                translated_blocks = []
                for b in parsed_blocks:
                    if b["valid"]:
                        translated_text = ""
                        if b["clean_text"]:
                            translated_text = translated_map.get(b["clean_text"], b["clean_text"])
                        translated_blocks.append(f"{b['idx']}\n{b['time_line']}\n{translated_text}{b['tag']}")
                    else:
                        translated_blocks.append(b["block"])
                        
                translated_srt = "\n\n".join(translated_blocks)
                
                response_data = {"success": True, "srt": translated_srt}
                body = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, f"Translation failed: {e}")
            return

        # Route the transcribe API
        elif path == '/api/transcribe':
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self.send_error(400, "Content-Type must be multipart/form-data")
                return
                
            boundary_match = re.search(r'boundary=([^;]+)', content_type)
            if not boundary_match:
                self.send_error(400, "Missing boundary in Content-Type")
                return
            boundary = boundary_match.group(1).strip().strip('"')
            
            content_length = int(self.headers.get('Content-Length', 0))
            body_bytes = self.rfile.read(content_length)
            
            form_data, files = parse_multipart(body_bytes, boundary)
            if 'video' not in files and 'video_url' not in form_data:
                self.send_error(400, "Missing video file or video_url in payload")
                return
                
            lang_code = form_data.get('language', 'km-KH')
            gemini_key = form_data.get('gemini_key', '').strip()
                
            temp_dir = f"temp_srv_trans_{uuid.uuid4()}"
            os.makedirs(temp_dir, exist_ok=True)
            input_video_path = os.path.join(temp_dir, "input_video.mp4")
            
            try:
                if 'video' in files:
                    # Save uploaded video
                    with open(input_video_path, 'wb') as f:
                        f.write(files['video']['content'])
                elif 'video_url' in form_data:
                    video_url = form_data['video_url'].strip()
                    print(f"[Server Transcribe] Downloading video from URL: {video_url}")
                    req = urllib.request.Request(video_url, headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=120) as resp, open(input_video_path, 'wb') as f:
                        f.write(resp.read())
                    
                # Setup ffmpeg executable
                if shutil.which("ffmpeg"):
                    ffmpeg_exe = "ffmpeg"
                else:
                    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                    
                lang_name_map = {
                    "km-KH": "Khmer (ភាសាខ្មែរ)",
                    "en-US": "English",
                    "zh-CN": "Chinese",
                    "th-TH": "Thai",
                    "vi-VN": "Vietnamese",
                    "ja-JP": "Japanese",
                    "ko-KR": "Korean"
                }
                lang_name = lang_name_map.get(lang_code, "Khmer")
                if lang_code == "auto":
                    lang_name = "Khmer"

                # ── Gemini Transcribe Mode ──────────────────────────────────────
                if gemini_key:
                    print("[Server Transcribe] Using Gemini direct audio transcription...")
                    mp3_path = os.path.join(temp_dir, "audio.mp3")
                    cmd_mp3 = [
                        ffmpeg_exe, "-y",
                        "-i", input_video_path,
                        "-ar", "16000",
                        "-ac", "1",
                        "-b:a", "32k",
                        mp3_path
                    ]
                    res_mp3 = subprocess.run(cmd_mp3, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    if res_mp3.returncode == 0 and os.path.exists(mp3_path):
                        try:
                            import base64
                            with open(mp3_path, "rb") as f:
                                audio_b64 = base64.b64encode(f.read()).decode("utf-8")
                                
                            prompt = (
                                "You are an expert movie transcriber and subtitle generator. Listen to the attached audio file and transcribe/translate it "
                                f"directly into highly accurate subtitles in standard SRT format in {lang_name} (ភាសាខ្មែរ).\n\n"
                                "Rules:\n"
                                "1. Split the dialogues into logical segments (between 1.5 to 7 seconds long).\n"
                                "2. Provide standard SRT timestamps for each block.\n"
                                "3. Keep the translation natural, cinematic, and fluent.\n"
                                "4. Detect the speaker gender: append ' (Female)' to the text line if a female voice is speaking, "
                                "or ' (Male)' if a male voice is speaking.\n"
                                "5. Output ONLY the raw SRT subtitle content. Do not include any introduction, notes, or backticks (like ```srt)."
                            )
                            
                            payload = {
                                "contents": [{
                                    "parts": [
                                        {
                                            "inlineData": {
                                                "mimeType": "audio/mp3",
                                                "data": audio_b64
                                            }
                                        },
                                        {
                                            "text": prompt
                                        }
                                    ]
                                }],
                                "generationConfig": {
                                    "temperature": 0.2
                                }
                            }
                            
                            models_to_try = [
                                "gemini-1.5-flash",
                                "gemini-2.0-flash",
                                "gemini-1.5-flash-latest"
                            ]
                            
                            gemini_res = None
                            last_err_msg = ""
                            
                            for model_name in models_to_try:
                                try:
                                    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={gemini_key}"
                                    gemini_payload = json.dumps(payload).encode("utf-8")
                                    gemini_req = urllib.request.Request(
                                        gemini_url,
                                        data=gemini_payload,
                                        headers={"Content-Type": "application/json"},
                                        method="POST"
                                    )
                                    with urllib.request.urlopen(gemini_req, timeout=120) as gemini_resp:
                                        gemini_res = json.loads(gemini_resp.read().decode("utf-8"))
                                        if gemini_res:
                                            break
                                except urllib.error.HTTPError as http_ex:
                                    err_body = http_ex.read().decode('utf-8', errors='ignore')
                                    print(f"[Server Transcribe] Gemini {model_name} HTTP {http_ex.code}: {err_body}")
                                    try:
                                        err_json = json.loads(err_body)
                                        last_err_msg = err_json.get('error', {}).get('message', err_body)
                                    except Exception:
                                        last_err_msg = f"HTTP {http_ex.code}: {err_body[:200]}"
                                except Exception as ex:
                                    print(f"[Server Transcribe] Gemini {model_name} error: {ex}")
                                    last_err_msg = str(ex)

                            if gemini_res and 'candidates' in gemini_res and gemini_res['candidates']:
                                candidate = gemini_res['candidates'][0]
                                parts = candidate.get('content', {}).get('parts', [])
                                if parts:
                                    srt_content = parts[0].get('text', '').strip()
                                    if srt_content.startswith("```"):
                                        lines = srt_content.split("\n")
                                        if lines[0].startswith("```"):
                                            lines = lines[1:]
                                        if lines[-1].strip() == "```":
                                            lines = lines[:-1]
                                        srt_content = "\n".join(lines).strip()
                                    
                                    if srt_content:
                                        shutil.rmtree(temp_dir, ignore_errors=True)
                                        response_data = {"success": True, "srt": srt_content}
                                        body = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                                        self.send_response(200)
                                        self.send_header('Content-Type', 'application/json; charset=utf-8')
                                        self.send_header('Content-Length', str(len(body)))
                                        self.end_headers()
                                        self.wfile.write(body)
                                        return

                            err_text = f"Gemini API error: {last_err_msg}" if last_err_msg else "Gemini API error: មិនអាចដំណើរការ Gemini Key បានទេ (សូមពិនិត្យ Key)"
                            print(f"[Server Transcribe] Returning Gemini error to client: {err_text}")
                            response_data = {"success": False, "error": err_text}
                            body = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json; charset=utf-8')
                            self.send_header('Content-Length', str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            shutil.rmtree(temp_dir, ignore_errors=True)
                            return

                        except Exception as gem_ex:
                            print("[Server Transcribe] Gemini exception:", gem_ex)
                            response_data = {"success": False, "error": f"Gemini Error: {gem_ex}"}
                            body = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json; charset=utf-8')
                            self.send_header('Content-Length', str(len(body)))
                            self.end_headers()
                            self.wfile.write(body)
                            shutil.rmtree(temp_dir, ignore_errors=True)
                            return
                
                # ── Legacy Google STT Mode (Fallback) ───────────────────────────
                wav_path = os.path.join(temp_dir, "full_audio.wav")
                print("[Server Transcribe] Extracting audio for legacy STT...")
                cmd_wav = [
                    ffmpeg_exe, "-y",
                    "-i", input_video_path,
                    "-ar", "16000",
                    "-ac", "1",
                    "-c:a", "pcm_s16le",
                    wav_path
                ]
                res_wav = subprocess.run(cmd_wav, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if res_wav.returncode != 0 or not os.path.exists(wav_path):
                    raise RuntimeError("Failed to extract audio track")
                    
                # Probing duration
                duration_sec = get_video_duration(input_video_path, ffmpeg_exe)
                if not duration_sec:
                    duration_sec = 0
                    try:
                        import wave
                        with wave.open(wav_path, 'rb') as w:
                            duration_sec = w.getnframes() / float(w.getframerate())
                    except:
                        pass
                
                if duration_sec <= 0:
                    raise RuntimeError("Could not determine video/audio duration")
                    
                print(f"[Server Transcribe] Video duration: {duration_sec:.1f}s. Running silence detection...")
                # Silence detect
                cmd_silence = [
                    ffmpeg_exe, "-i", wav_path,
                    "-filter_complex", "silencedetect=noise=-35dB:d=0.3",
                    "-f", "null", "-"
                ]
                res_silence = subprocess.run(cmd_silence, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                output = res_silence.stderr
                
                starts = [float(x) for x in re.findall(r"silence_start:\s*([\d\.]+)", output)]
                ends = [float(x) for x in re.findall(r"silence_end:\s*([\d\.]+)", output)]
                
                slice_points = [0.0]
                for s, e in zip(starts, ends):
                    mid = (s + e) / 2.0
                    if mid - slice_points[-1] >= 1.0:
                        slice_points.append(mid)
                if duration_sec - slice_points[-1] >= 2.0:
                    slice_points.append(duration_sec)
                else:
                    slice_points[-1] = duration_sec
                    
                segments = []
                for i in range(len(slice_points) - 1):
                    segments.append((slice_points[i], slice_points[i+1]))
                    
                refined = []
                for start, end in segments:
                    dur = end - start
                    if dur > 15.0:
                        num_splits = int(dur // 8.0) + 1
                        split_dur = dur / num_splits
                        for i in range(num_splits):
                            refined.append((start + i * split_dur, start + (i + 1) * split_dur))
                    else:
                        refined.append((start, end))
                        
                import speech_recognition as sr
                import time
                
                recognizer = sr.Recognizer()
                srt_blocks = []
                
                def format_time(sec):
                    hrs = int(sec // 3600)
                    mins = int((sec % 3600) // 60)
                    secs = int(sec % 60)
                    ms = int((sec - int(sec)) * 1000)
                    return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"
                    
                actual_lang = lang_code
                if actual_lang == "auto":
                    print("[Server Transcribe] Warning: Google STT does not support Auto Detect. Defaulting to en-US.")
                    actual_lang = "en-US"

                print(f"[Server Transcribe] Transcribing {len(refined)} chunks...")
                for idx, (start, end) in enumerate(refined):
                    chunk_wav = os.path.join(temp_dir, f"chunk_{idx}.wav")
                    duration = end - start
                    cmd_cut = [
                        ffmpeg_exe, "-y",
                        "-i", wav_path,
                        "-ss", f"{start:.3f}",
                        "-t", f"{duration:.3f}",
                        "-c:a", "pcm_s16le",
                        "-af", "dynaudnorm",
                        chunk_wav
                    ]
                    subprocess.run(cmd_cut, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    
                    text = ""
                    gender = "Unknown"
                    if os.path.exists(chunk_wav):
                        try:
                            gender = detect_voice_gender(chunk_wav)
                            with sr.AudioFile(chunk_wav) as source:
                                audio_data = recognizer.record(source)
                                text = recognizer.recognize_google(audio_data, language=actual_lang)
                                text = text.strip()
                        except sr.UnknownValueError:
                            pass
                        except Exception as e:
                            print(f"[Server Transcribe] Chunk {idx} err:", e)
                        try: os.remove(chunk_wav)
                        except: pass
                        
                    if text:
                        if gender == "Female":
                            text = f"{text} (Female)"
                        elif gender == "Male":
                            text = f"{text} (Male)"
                        srt_blocks.append(
                            f"{len(srt_blocks) + 1}\n"
                            f"{format_time(start)} --> {format_time(end)}\n"
                            f"{text}\n"
                        )
                    time.sleep(0.3)
                    
                srt_content = "\n".join(srt_blocks).strip()
                if not srt_content:
                    print("[Server Transcribe] Warning: No speech detected in video audio.")
                    response_data = {
                        "success": False,
                        "error": "មិនអាចស្គាល់សំឡេងនិយាយក្នុងវីដេអូបានទេ! (សូមបញ្ចូល Gemini API Key ឥតគិតថ្លៃ ក្នុងប្រអប់ Gemini API Key ដើម្បីបំប្លែងសំឡេងបានច្បាស់ 100%)"
                    }
                else:
                    print(f"[Server Transcribe] Transcription completed with {len(srt_blocks)} blocks.")
                    response_data = {"success": True, "srt": srt_content}

                body = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                
            except Exception as e:
                print("[Server Transcribe] Fatal error:", e)
                response_data = {"success": False, "error": str(e)}
                body = json.dumps(response_data, ensure_ascii=False).encode('utf-8')
                self.send_response(500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            return

        else:
            self.send_error(404, "Not Found")

def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = '127.0.0.1'
    return ip

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True

if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    install_requirements()
    
    local_ip = get_local_ip()
    print(f"\n==================================================")
    print(f" Starting AI Dubbing Pro Local Server")
    print(f" Web Interface (PC): http://localhost:{PORT}/index.html")
    if local_ip != '127.0.0.1':
        print(f" Mobile / APK URL:   http://{local_ip}:{PORT}/index.html")
        print(f" Backend URL for APK: http://{local_ip}:{PORT}")
    print(f"==================================================")
    print(f" * Multi-threaded request processing enabled")
    print(f" * Automatic TTS engine fallback (Edge/Google) ready")
    print(f"==================================================\n")
    
    try:
        with ThreadingTCPServer(("", PORT), DubbingHandler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    except Exception as e:
        print(f"Server error: {e}")
