#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright © 2025 Takuma Yagi <takuma.yagi@aist.go.jp>
#
# Distributed under terms of the MIT license.

import os
import os.path as osp
import sys
try:
    import gnureadline  # Optional; keeps original interactive behavior where available.
except ImportError:
    gnureadline = None
import argparse
import subprocess
import shlex

import pandas as pd


def run_ffmpeg(cmd, debug=False):
    """Run ffmpeg safely: no shell, preserve spaces in paths, fail fast on errors."""
    print(" ".join(shlex.quote(str(x)) for x in cmd))
    if not debug:
        subprocess.run(cmd, check=True)


def sync_videos(args, df):

    blacklist = []
    if osp.exists(args.blacklist_path):
        with open(args.blacklist_path, "r") as f:
            for line in f:
                blacklist.append(line.strip("\n"))

    sync_time = max(df["start_timestamp"].tolist())

    start_frames = [
        row.frame_pos + round((sync_time - row.start_timestamp) * row.fps)
        for index, row in df.iterrows()
    ]

    nb_crop_frames = min(
        [nb_frames - start_frame for nb_frames, start_frame in zip(df["nb_frames"].tolist(), start_frames)]
    )
    if nb_crop_frames <= 0:
        print(f"[WARN] No valid crop duration; skip. nb_crop_frames={nb_crop_frames}")
        return

    start_seconds = [start_frame / fps for start_frame, fps in zip(start_frames, df["fps"].tolist())]

    os.makedirs(args.out_dir, exist_ok=True)

    month, day = list(map(int, args.date.split("/")))
    out_paths = []
    path_by_cam = {}

    for (index, row), start_sec in zip(df.iterrows(), start_seconds):
        # date, participant id, task, take, camera
        # 0915_1_1_1_1.mp4
        video_path = osp.join(args.root_dir, row.video_path)
        out_id = f"{month:02d}_{day:02d}_{row.participant_id:02d}_{row.task:02d}_{row['take']:02d}_{row.camera_id}"
        out_path = osp.join(args.out_dir, f"{out_id}.mp4")
        out_paths.append(out_path)
        path_by_cam[int(row.camera_id)] = out_path

        invalid = False
        for blacklist_id in blacklist:
            if out_id.startswith(blacklist_id):
                print(f"Invalid recording, skip: {out_id}")
                invalid = True
        if invalid:
            continue

        duration = nb_crop_frames / row.fps

        # Keep the original trim method by default for minimal behavioral change.
        # Use --sync-reencode if you need frame-accurate cuts instead of keyframe-aligned stream copy.
        if args.sync_reencode:
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel", "error",
                "-ss", f"{float(start_sec):.6f}",
                "-i", video_path,
                "-t", f"{float(duration):.6f}",
                "-map", "0:v:0",
                "-map", "0:a?",
                "-c:v", "libx264",
                "-preset", args.sync_preset,
                "-crf", str(int(args.sync_crf)),
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", f"{int(args.sync_audio_kbps)}k",
                "-avoid_negative_ts", "make_zero",
                out_path,
            ]
        else:
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel", "error",
                "-ss", f"{float(start_sec):.6f}",
                "-i", video_path,
                "-t", f"{float(duration):.6f}",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                out_path,
            ]

        if int(row.camera_id) in args.process_id_list:
            run_ffmpeg(cmd, debug=args.debug)

    # Visualization
    # XXX Assume 2 row x 3 col view for now
    if args.vis:
        row0 = df.iloc[0]
        out_id = f"{month:02d}_{day:02d}_{row0.participant_id:02d}_{row0.task:02d}_{row0['take']:02d}"
        invalid = False
        for blacklist_id in blacklist:
            if out_id.startswith(blacklist_id):
                print(f"Invalid recording, skip: {out_id}")
                invalid = True
        if invalid:
            return

        if args.vis_dir:
            vis_dir = args.vis_dir
        else:
            vis_dir = osp.join(args.out_dir, "vis")
        os.makedirs(vis_dir, exist_ok=True)
        vis_out_path = osp.join(vis_dir, f"{out_id}_vis.mp4")

        if len(args.camera_id_list) != 6:
            print(f"[WARN] tile vis expects exactly 6 cameras, got {len(args.camera_id_list)}; skip vis for {out_id}")
            return

        missing_cam_ids = [cid for cid in args.camera_id_list if cid not in path_by_cam]
        if missing_cam_ids:
            print(f"[WARN] Missing camera IDs in summary: {missing_cam_ids}; skip vis for {out_id}")
            return

        vis_video_paths = [path_by_cam[cid] for cid in args.camera_id_list]

        # Safety: do not mix freshly generated files with stale files from an older run.
        # In normal runs, every camera shown in --vis must also be processed in this run.
        if not args.debug:
            missing_from_process = sorted(set(args.camera_id_list) - set(args.process_id_list))
            if missing_from_process:
                raise RuntimeError(
                    "--vis requires all cameras in --camera_id_list to be processed in this run. "
                    f"Missing from --process_id_list: {missing_from_process}"
                )
            for cid, video_path in zip(args.camera_id_list, vis_video_paths):
                if not osp.exists(video_path):
                    raise FileNotFoundError(
                        f"Missing synced video for visualization: camera_id={cid}, path={video_path}"
                    )

        tw = int(args.vis_tile_width)
        filter_parts = [
            f"[{i}:v]setpts=PTS-STARTPTS,scale={tw}:-2[v{i}]"
            for i in range(6)
        ]
        filter_complex = (
            ";".join(filter_parts)
            + ";"
            + "".join(f"[v{i}]" for i in range(6))
            + f"xstack=inputs=6:layout={args.vis_layout}:shortest=1[v]"
        )

        cmd = ["ffmpeg", "-y", "-loglevel", "error"]
        for video_path in vis_video_paths:
            cmd += ["-i", video_path]

        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-an",  # safer for visual sync checks; avoids misleading audio offset from a single camera
            "-c:v", "libx264",
            "-preset", args.vis_preset,
            "-crf", str(int(args.vis_crf)),
            "-pix_fmt", "yuv420p",
        ]
        if args.vis_fps > 0:
            cmd += ["-r", str(float(args.vis_fps))]
        cmd += [vis_out_path]

        run_ffmpeg(cmd, debug=args.debug)


def main():
    """
    Crop videos via ffmpeg
    Can specify a blacklist to skip processing broken files
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('root_dir', type=str)  # e.g. ./videos_251001
    parser.add_argument('--date', type=str, default="01/01")
    parser.add_argument('--out_dir', type=str, default="./export")
    parser.add_argument(
        '--vis_dir',
        type=str,
        default="",
        help='Tile vis output directory (default: <out_dir>/vis).',
    )
    parser.add_argument('--camera_id_list', type=int, nargs="+", default=[1, 2, 3, 4, 5, 6])
    parser.add_argument('--process_id_list', type=int, nargs="*", default=[1, 2, 3, 4, 5, 6])
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--vis', action='store_true')
    parser.add_argument(
        '--sync-reencode',
        action='store_true',
        help='Re-encode synced videos for more accurate cuts. Default keeps original -c copy behavior.',
    )
    parser.add_argument(
        '--sync-crf',
        type=int,
        default=18,
        help='x264 CRF for --sync-reencode output; lower = higher quality (default 18).',
    )
    parser.add_argument(
        '--sync-preset',
        type=str,
        default='veryfast',
        help='x264 preset for --sync-reencode output (default veryfast).',
    )
    parser.add_argument(
        '--sync-audio-kbps',
        type=int,
        default=128,
        help='AAC bitrate for --sync-reencode output (default 128).',
    )
    parser.add_argument(
        '--vis-tile-width',
        type=int,
        default=480,
        help='Per-camera tile width in px for --vis (default 480, was hardcoded 720).',
    )
    parser.add_argument(
        '--vis-crf',
        type=int,
        default=28,
        help='x264 CRF for tile vis output; higher = lower quality/smaller file (default 28).',
    )
    parser.add_argument(
        '--vis-preset',
        type=str,
        default='veryfast',
        help='x264 preset for tile vis (default veryfast).',
    )
    parser.add_argument(
        '--vis-audio-kbps',
        type=int,
        default=96,
        help='Deprecated in this safe version: tile vis output is video-only by default.',
    )
    parser.add_argument(
        '--vis-fps',
        type=float,
        default=0.0,
        help='Output fps for tile vis; 0 keeps source fps (default 0).',
    )
    parser.add_argument(
        '--vis-layout',
        type=str,
        default='0_0|w0+w4_0|0_h0|w0+w4_h0|w0_h0|w0_0',
        help='xstack layout for 6-camera tile vis. Default preserves the original layout.',
    )
    parser.add_argument('--blacklist_path', type=str, default="./blacklist.txt")
    args = parser.parse_args()

    # Remove /
    if args.root_dir.endswith("/"):
        args.root_dir = args.root_dir[:-1]

    print("Target date: {}".format(args.date))
    target_month, target_day = list(map(int, args.date.split("/")))

    summary_path = osp.join(args.root_dir, "summary_{:02d}_{:02d}.csv".format(target_month, target_day))
    if not osp.exists(summary_path):
        print(f"{summary_path} does not exist. Run get_metadata.py first.")
        sys.exit(1)

    df = pd.read_csv(summary_path)
    print(df)

    # XXX Brute-force
    for pid in range(1, 100+1, 1):
        for task in range(1, 7+1, 1):
            for take in range(1, 10+1, 1):
                sub_df = df.query(f'participant_id == {pid} & task == {task} & take == {take}')
                if len(sub_df) == 0:
                    continue
                print(f"Participant id: {pid}, Task: {task}, Take: {take}")
                sync_videos(args, sub_df)


if __name__ == "__main__":
    main()