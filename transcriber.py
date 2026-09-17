import os
import sys
import subprocess
import re
import shutil
import time
import argparse

# Subprocess flags to hide console window on Windows if run as gui
SUBPROCESS_FLAGS = 0
if sys.platform == "win32":
    SUBPROCESS_FLAGS = 0x08000000

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
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15, creationflags=SUBPROCESS_FLAGS)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception:
        pass

    # Fallback to ffmpeg -i
    try:
        cmd = [ffmpeg_exe, "-i", video_path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15, creationflags=SUBPROCESS_FLAGS)
        output = res.stderr
        match = re.search(r"Duration:\s*(\d{2}):(\d{2}):(\d{2})\.(\d{2})", output)
        if match:
            hours = int(match.group(1))
            minutes = int(match.group(2))
            seconds = int(match.group(3))
            hundredths = int(match.group(4))
            total_seconds = hours * 3600 + minutes * 60 + seconds + hundredths / 100.0
            return total_seconds
    except Exception:
        pass
    return None

def transcribe_video(video_path, output_srt_path, lang_code):
    try:
        import speech_recognition as sr
    except ImportError:
        print("Installing speech_recognition...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "SpeechRecognition"], creationflags=SUBPROCESS_FLAGS)
        import speech_recognition as sr

    try:
        import imageio_ffmpeg
    except ImportError:
        print("Installing imageio-ffmpeg...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "imageio-ffmpeg"], creationflags=SUBPROCESS_FLAGS)
        import imageio_ffmpeg

    if shutil.which("ffmpeg"):
        ffmpeg_exe = "ffmpeg"
    else:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    print(f"\n🎬 Video File: {video_path}")
    print(f"🌐 Target Language: {lang_code}")
    print(f"💾 Saving to: {output_srt_path}")

    # Extract audio
    print("\n🔊 Extracting audio track...")
    temp_dir = f"temp_cli_trans_{os.getpid()}"
    os.makedirs(temp_dir, exist_ok=True)
    wav_path = os.path.join(temp_dir, "audio.wav")

    cmd_wav = [
        ffmpeg_exe, "-y",
        "-i", video_path,
        "-ar", "16000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        wav_path
    ]
    res = subprocess.run(cmd_wav, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=SUBPROCESS_FLAGS)
    if res.returncode != 0 or not os.path.exists(wav_path):
        print("❌ Error: Failed to extract audio track.")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False

    # Get duration
    duration_sec = get_video_duration(video_path, ffmpeg_exe)
    if not duration_sec:
        duration_sec = 0
        try:
            import wave
            with wave.open(wav_path, 'rb') as w:
                duration_sec = w.getnframes() / float(w.getframerate())
        except:
            pass

    if duration_sec <= 0:
        print("❌ Error: Could not determine video duration.")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return False

    print(f"🎬 Video Duration: {duration_sec:.2f} seconds")
    print("🔍 Analyzing silences for segment division...")

    # Detect silences
    cmd_silence = [
        ffmpeg_exe, "-i", wav_path,
        "-filter_complex", "silencedetect=noise=-35dB:d=0.3",
        "-f", "null", "-"
    ]
    res_silence = subprocess.run(cmd_silence, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=SUBPROCESS_FLAGS)
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

    total_chunks = len(refined)
    print(f"✂️ Segmented video audio into {total_chunks} blocks.")

    recognizer = sr.Recognizer()
    srt_blocks = []

    def format_time(sec):
        hrs = int(sec // 3600)
        mins = int((sec % 3600) // 60)
        secs = int(sec % 60)
        ms = int((sec - int(sec)) * 1000)
        return f"{hrs:02d}:{mins:02d}:{secs:02d},{ms:03d}"

    print("\n🎙️ Transcribing blocks (Speech-to-Text)...")
    for idx, (start, end) in enumerate(refined):
        print(f"  ➜ Processing segment {idx+1}/{total_chunks} ({start:.2f}s -> {end:.2f}s)...", end="\r")
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
        subprocess.run(cmd_cut, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=SUBPROCESS_FLAGS)

        text = ""
        gender = "Unknown"
        if os.path.exists(chunk_wav):
            try:
                gender = detect_voice_gender(chunk_wav)
                with sr.AudioFile(chunk_wav) as source:
                    audio_data = recognizer.record(source)
                    text = recognizer.recognize_google(audio_data, language=lang_code)
                    text = text.strip()
            except sr.UnknownValueError:
                pass
            except Exception as e:
                pass
            try: os.remove(chunk_wav)
            except: pass

        if text:
            if gender == "Female":
                text = f"{text} (Female)"
            elif gender == "Male":
                text = f"{text} (Male)"
            print(f"  📝 [{format_time(start)}]: \"{text}\"")
            srt_blocks.append(
                f"{len(srt_blocks) + 1}\n"
                f"{format_time(start)} --> {format_time(end)}\n"
                f"{text}\n"
            )
        time.sleep(0.3)

    # Write SRT
    with open(output_srt_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(srt_blocks))

    # Clean up
    shutil.rmtree(temp_dir, ignore_errors=True)
    print(f"\n🎉 Success! SRT file saved to: {output_srt_path}")
    return True

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Video Transcriber (Video to SRT)")
    parser.add_argument("--video", type=str, help="Path to original video file")
    parser.add_argument("--output", type=str, help="Path to save output SRT file")
    parser.add_argument("--lang", type=str, default="en-US", help="Language code (e.g. en-US, km-KH, zh-CN, th-TH)")

    args = parser.parse_args()

    # If args not provided, ask interactively
    video = args.video
    if not video:
        video = input("🎬 Please enter path to video file: ").strip().strip('"')
    if not os.path.exists(video):
        print(f"❌ Error: Video file not found: {video}")
        sys.exit(1)

    output = args.output
    if not output:
        output = os.path.splitext(video)[0] + ".srt"

    lang = args.lang
    if not args.video: # if run interactively, prompt for language choice
        print("\n🌐 Choose Spoken Language:")
        print("1. English (en-US)")
        print("2. Khmer (km-KH)")
        print("3. Chinese (zh-CN)")
        print("4. Thai (th-TH)")
        print("5. Vietnamese (vi-VN)")
        print("6. Japanese (ja-JP)")
        print("7. Korean (ko-KR)")
        choice = input("Enter choice (1-7, default 1): ").strip()
        lang_map = {
            "1": "en-US", "2": "km-KH", "3": "zh-CN", "4": "th-TH",
            "5": "vi-VN", "6": "ja-JP", "7": "ko-KR"
        }
        lang = lang_map.get(choice, "en-US")

    transcribe_video(video, output, lang)
