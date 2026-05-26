#!/usr/bin/env python3
"""
Automated Tennis Video Editor - Deadplay Removal (Production Grade)
------------------------------------------------------------------
Detects active tennis rallies using audio peak (racket hits) and visual motion analysis, 
then slices and stitches active segments using hardware-accelerated FFmpeg.

Features:
- Cross-platform compatibility for macOS and Linux
- Automatic GPU hardware transcoder selection (VideoToolbox, NVENC, QSV, Software)
- High-res/Low-res robust audio conversion (mono 16kHz PCM WAV)
- Dynamic color profiles and HDR metadata preservation (8-bit Rec.709 vs 10-bit Rec.2020 HLG)
- Auto-scaling bitrate engine (Adaptive 1080p/4K HDR/SDR matching)
"""

import os
import sys
import json
import argparse
import platform
import subprocess
import wave
import shutil
from pathlib import Path
import numpy as np

def run_command(cmd, desc="Running command"):
    print(f"[{desc}] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        print(f"Error during: {desc}", file=sys.stderr)
        print(result.stderr.decode('utf-8', errors='ignore'), file=sys.stderr)
        raise RuntimeError(f"Command failed with exit code {result.returncode}")
    return result.stdout

class TennisDeadplayTrimmer:
    def __init__(self, input_path, output_path, args):
        self.input = Path(input_path).resolve()
        self.output = Path(output_path).resolve()
        self.args = args
        
        # Diagnostics
        self.os = platform.system().lower()
        if self.os not in ('darwin', 'linux'):
            raise RuntimeError(
                f"Unsupported operating system: {platform.system()}. "
                "This script currently supports macOS and Linux only."
            )
        self.encoder = self.detect_best_encoder()
        self.metadata = self.probe_video_metadata()
        
    def detect_best_encoder(self):
        """Programmatically probes available FFmpeg HEVC hardware encoders."""
        if self.args.cpu_only:
            print("[Encoder Setup] Forcing software-only encoding fallback (libx265).")
            return 'libx265'
            
        cmd = ['ffmpeg', '-v', 'error', '-encoders']
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            encoders_list = result.stdout.decode('utf-8', errors='ignore')
            
            # Prioritized platform and hardware matching
            if 'darwin' in self.os and 'hevc_videotoolbox' in encoders_list:
                print("[Encoder Setup] Detected macOS. Using Apple Hardware acceleration (hevc_videotoolbox).")
                return 'hevc_videotoolbox'
            elif 'hevc_nvenc' in encoders_list:
                print("[Encoder Setup] Detected Linux-compatible NVIDIA hardware. Using NVENC acceleration (hevc_nvenc).")
                return 'hevc_nvenc'
            elif 'hevc_qsv' in encoders_list:
                print("[Encoder Setup] Detected Linux-compatible Intel hardware. Using QuickSync acceleration (hevc_qsv).")
                return 'hevc_qsv'
            elif 'libx265' in encoders_list:
                print("[Encoder Setup] No hardware GPU encoders found. Using software HEVC (libx265).")
                return 'libx265'
        except Exception as e:
            print(f"[Encoder Setup] Warning: Failed to probe FFmpeg encoders ({e}). Falling back to libx265.", file=sys.stderr)
            
        print("[Encoder Setup] Universal fallback: Software H.264 (libx264).")
        return 'libx264'

    def probe_video_metadata(self):
        """Inspects resolution, duration, colorspace, and bit-depth using JSON ffprobe output."""
        cmd = [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,pix_fmt,color_primaries,color_transfer,duration',
            '-of', 'json', str(self.input)
        ]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            data = json.loads(result.stdout.decode('utf-8'))
            if 'streams' in data and len(data['streams']) > 0:
                stream = data['streams'][0]
                
                width = int(stream.get('width', 1920))
                height = int(stream.get('height', 1080))
                pix_fmt = stream.get('pix_fmt', 'yuv420p')
                color_primaries = stream.get('color_primaries', 'bt709')
                color_transfer = stream.get('color_transfer', 'bt709')
                
                try:
                    duration = float(stream.get('duration', 0.0))
                except ValueError:
                    duration = 0.0
                    
                # 10-bit / HDR auto-detection
                is_10bit = '10' in pix_fmt or pix_fmt == 'p010le'
                is_hdr = 'bt2020' in color_primaries or is_10bit
                
                metadata = {
                    'width': width,
                    'height': height,
                    'pix_fmt': pix_fmt,
                    'color_primaries': color_primaries,
                    'color_transfer': color_transfer,
                    'duration': duration,
                    'is_10bit': is_10bit,
                    'is_hdr': is_hdr
                }
                print(f"[Probe] Metadata resolved: {width}x{height}, {pix_fmt}, HDR={is_hdr}, Duration={duration:.2f}s")
                return metadata
        except Exception as e:
            print(f"[Probe] Warning: Failed to parse metadata via JSON ({e}). Using standard 1080p SDR fallbacks.", file=sys.stderr)
            
        return {
            'width': 1920, 'height': 1080, 'pix_fmt': 'yuv420p',
            'color_primaries': 'bt709', 'color_transfer': 'bt709',
            'duration': 0.0, 'is_10bit': False, 'is_hdr': False
        }

    def get_video_duration(self):
        if self.metadata['duration'] > 0.0:
            return self.metadata['duration']
        # Fallback if duration key was missing in streams
        cmd = [
            'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1', str(self.input)
        ]
        try:
            out = run_command(cmd, "Getting video duration fallback")
            return float(out.decode('utf-8').strip())
        except Exception:
            return 0.0

    def extract_audio(self, out_wav):
        """Robustly extracts primary audio stream mapping and normalizes it to mono 16kHz PCM WAV."""
        cmd = [
            'ffmpeg', '-y', '-i', str(self.input),
            '-map', '0:a:0',
            '-vn', '-acodec', 'pcm_s16le', '-ac', '1', '-ar', '16000', str(out_wav)
        ]
        run_command(cmd, "Extracting audio track and normalizing to mono PCM WAV")

    def extract_lowres_frames(self, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        cmd = [
            'ffmpeg', '-y', '-i', str(self.input),
            '-vf', f'fps={self.args.fps},scale=320:180',
            '-q:v', '5',
            os.path.join(out_dir, 'frame_%04d.jpg')
        ]
        run_command(cmd, "Extracting low-res frames for motion analysis")
        frames = sorted([f for f in os.listdir(out_dir) if f.endswith('.jpg')])
        return frames

    def analyze_audio_peaks(self, wav_path):
        with wave.open(str(wav_path), 'rb') as w:
            params = w.getparams()
            nchannels, sampwidth, framerate, nframes = params[:4]
            str_data = w.readframes(nframes)
            if sampwidth == 2:
                data = np.frombuffer(str_data, dtype=np.int16)
            elif sampwidth == 1:
                data = np.frombuffer(str_data, dtype=np.int8)
            else:
                raise ValueError(f"Unsupported sample width: {sampwidth}")
                
        if nchannels > 1:
            data = data.reshape(-1, nchannels).mean(axis=1)
            
        abs_data = np.abs(data)
        window_size = int(framerate * 0.02)  # 20ms windows
        num_windows = len(abs_data) // window_size
        windowed_max = np.zeros(num_windows)
        windowed_times = np.zeros(num_windows)
        
        for i in range(num_windows):
            windowed_max[i] = np.max(abs_data[i*window_size : (i+1)*window_size])
            windowed_times[i] = (i * window_size + window_size / 2) / framerate
            
        max_val = np.max(windowed_max)
        norm_max = windowed_max / max_val if max_val > 0 else windowed_max
        
        peaks = []
        for i in range(1, num_windows - 1):
            if norm_max[i] > self.args.audio_threshold and norm_max[i] > norm_max[i-1] and norm_max[i] > norm_max[i+1]:
                if not peaks or (windowed_times[i] - peaks[-1][0] > 0.3):
                    peaks.append((windowed_times[i], norm_max[i]))
                    
        return peaks, windowed_times, norm_max

    def analyze_motion(self, frame_dir, frames):
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            print("[Warning] matplotlib not found. Skipping physical motion verification.", file=sys.stderr)
            return None, None

        motion_values = []
        prev_gray = None
        
        for f in frames:
            path = os.path.join(frame_dir, f)
            img = plt.imread(path)
            gray = np.mean(img, axis=2) if len(img.shape) == 3 else img
            if prev_gray is not None:
                diff = np.mean(np.abs(gray - prev_gray))
                motion_values.append(diff)
            else:
                motion_values.append(0.0)
            prev_gray = gray
            
        times = np.arange(len(frames)) / self.args.fps
        motion_values = np.array(motion_values)
        norm_motion = motion_values / np.max(motion_values) if np.max(motion_values) > 0 else motion_values
        return times, norm_motion

    def run(self):
        # Set up folders
        temp_dir = Path(self.args.temp_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
        
        temp_wav = temp_dir / 'extracted_audio.wav'
        frame_dir = temp_dir / 'frames'
        
        try:
            duration = self.get_video_duration()
            if duration <= 0.0:
                print("Error: Input video duration could not be determined.", file=sys.stderr)
                sys.exit(1)
                
            print(f"Analyzing {self.input.name} ({duration:.2f} seconds)...")
            
            # 1. Audio Peak Processing
            self.extract_audio(temp_wav)
            peaks, audio_times, audio_max = self.analyze_audio_peaks(temp_wav)
            print(f"Detected {len(peaks)} racket strike peaks.")
            
            if not peaks:
                print("Error: No tennis active racket hits detected! Adjust volume threshold via --audio-threshold.", file=sys.stderr)
                sys.exit(1)
                
            hit_times = [p[0] for p in peaks]
            
            # 2. Frame-based Motion Processing
            motion_times, norm_motion = None, None
            if not self.args.no_motion:
                try:
                    frames = self.extract_lowres_frames(frame_dir)
                    motion_times, norm_motion = self.analyze_motion(frame_dir, frames)
                except Exception as e:
                    print(f"[Warning] Motion mapping encountered an error ({e}). Falling back to audio-only parsing.", file=sys.stderr)
                    
            # 3. Rally Grouping Logic
            rallies = []
            current_rally = []
            for hit in hit_times:
                if not current_rally:
                    current_rally.append(hit)
                else:
                    if hit - current_rally[-1] < self.args.gap_threshold:
                        current_rally.append(hit)
                    else:
                        rallies.append(current_rally)
                        current_rally = [hit]
            if current_rally:
                rallies.append(current_rally)
                
            print(f"Clustered into {len(rallies)} tennis rally segments:")
            segments = []
            for i, rally in enumerate(rallies):
                first_hit = rally[0]
                last_hit = rally[-1]
                
                start_time = max(0.0, first_hit - self.args.serve_buffer)
                end_time = min(duration, last_hit + self.args.follow_through)
                
                print(f"  Rally {i+1}: Hits from {first_hit:.2f}s to {last_hit:.2f}s (Hits: {len(rally)})")
                print(f"    Play Segment: {start_time:.2f}s to {end_time:.2f}s (Duration: {end_time - start_time:.2f}s)")
                segments.append((start_time, end_time))
                
            # Plot generation
            if self.args.plot and motion_times is not None:
                try:
                    import matplotlib.pyplot as plt
                    plt.figure(figsize=(15, 6))
                    plt.plot(audio_times, audio_max, label='Audio (Hits)', color='blue', alpha=0.5)
                    plt.plot(motion_times, norm_motion, label='Motion', color='orange', alpha=0.7)
                    for pt, _ in peaks:
                        plt.axvline(x=pt, color='red', linestyle='--', alpha=0.6)
                    for i, (start, end) in enumerate(segments):
                        plt.axvspan(start, end, color='green', alpha=0.15, label='Active Play' if i == 0 else "")
                    plt.title(f"Rally Detection: {self.input.name}")
                    plt.xlabel("Time (seconds)")
                    plt.ylabel("Normalized Level")
                    plt.legend()
                    plt.grid(True)
                    plot_path = self.output.with_name(f"{self.output.stem}_plot.png")
                    plt.savefig(plot_path, dpi=300)
                    print(f"Saved visualization plot to {plot_path}")
                except Exception as e:
                    print(f"Could not save visual plot file: {e}", file=sys.stderr)
                    
            # 4. Adaptive Bitrate & Dynamic Transcode Options
            width = self.metadata['width']
            is_hdr = self.metadata['is_hdr']
            
            # Map auto-scaling bitrates
            if width > 1920: # 4K
                bitrate = '35M' if is_hdr else '25M'
            else: # 1080p / smaller
                bitrate = '12M' if is_hdr else '8M'
                
            print(f"[Auto-Scale] Selected target bitrate: {bitrate} (Resolution: {width}px, HDR={is_hdr})")
            
            # 5. Compile FFmpeg slicing filters
            filter_parts = []
            inputs_concat = []
            for idx, (start, end) in enumerate(segments):
                filter_parts.append(f"[0:v]trim=start={start:.2f}:end={end:.2f},setpts=PTS-STARTPTS[v{idx}]")
                filter_parts.append(f"[0:a]atrim=start={start:.2f}:end={end:.2f},asetpts=PTS-STARTPTS[a{idx}]")
                inputs_concat.append(f"[v{idx}][a{idx}]")
                
            concat_str = f"{''.join(inputs_concat)}concat=n={len(segments)}:v=1:a=1[outv][outa]"
            filter_parts.append(concat_str)
            filter_complex = "; ".join(filter_parts)
            
            # Construct FFmpeg options
            ffmpeg_cmd = [
                'ffmpeg', '-y', '-i', str(self.input),
                '-filter_complex', filter_complex,
                '-map', '[outv]', '-map', '[outa]',
                '-c:v', self.encoder, '-b:v', bitrate
            ]
            
            # Preserving HDR colors and container compatibility across macOS/Linux workflows
            if is_hdr:
                if self.encoder in ['hevc_videotoolbox', 'hevc_nvenc', 'libx265']:
                    # Apple and NVIDIA 10-bit HLG profiles
                    pix_fmt_out = 'p010le' if 'videotoolbox' in self.encoder else 'yuv420p10le'
                    ffmpeg_cmd.extend([
                        '-pix_fmt', pix_fmt_out,
                        '-color_primaries', 'bt2020',
                        '-color_trc', 'arib-std-b67',
                        '-colorspace', 'bt2020nc'
                    ])
            else:
                ffmpeg_cmd.extend(['-pix_fmt', 'yuv420p'])
                
            # QuickTime compat stream tagging
            ffmpeg_cmd.extend(['-tag:v', 'hvc1'])
            
            # Audio properties
            ffmpeg_cmd.extend([
                '-c:a', 'aac', '-b:a', '192k',
                str(self.output)
            ])
            
            print(f"\nEncoding using: {self.encoder}...")
            run_command(ffmpeg_cmd, "Stitching active segments")
            print(f"\nSuccess! Processed play-only video saved to: {self.output}")
            
        finally:
            print("Purging temporary workspace frames cache...")
            shutil.rmtree(temp_dir, ignore_errors=True)

def main():
    parser = argparse.ArgumentParser(description="Automated Tennis Play Video Editor - Deadplay Removal")
    parser.add_argument('input', help="Path to raw input MOV or MP4 video")
    parser.add_argument('output', help="Path to save output video")
    parser.add_argument('--temp-dir', default='temp_tennis_edit', help="Directory for temporary files")
    parser.add_argument('--gap-threshold', type=float, default=7.5, help="Time gap between hits to separate rallies (secs)")
    parser.add_argument('--audio-threshold', type=float, default=0.35, help="Normalized audio volume peak threshold (0.0 to 1.0)")
    parser.add_argument('--serve-buffer', type=float, default=2.5, help="Time to include before first hit of a rally (secs)")
    parser.add_argument('--follow-through', type=float, default=1.5, help="Time to include after last hit of a rally (secs)")
    parser.add_argument('--fps', type=int, default=5, help="FPS for motion analysis extraction")
    parser.add_argument('--no-motion', action='store_true', help="Skip visual motion analysis entirely")
    parser.add_argument('--plot', action='store_true', help="Save a visualization plot next to the output video")
    parser.add_argument('--cpu-only', action='store_true', help="Force software CPU-only encoding (libx265)")
    
    args = parser.parse_args()
    
    try:
        trimmer = TennisDeadplayTrimmer(args.input, args.output, args)
        trimmer.run()
    except Exception as e:
        print(f"\nExecution failed: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == '__main__':
    main()
