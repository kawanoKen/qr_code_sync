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
import gnureadline
import argparse
import subprocess

import pandas as pd


def sync_videos(args, df):

    blacklist = []
    if osp.exists(args.blacklist_path):
        with open(args.blacklist_path, "r") as f:
            for line in f:
                blacklist.append(line.strip("\n"))

    sync_time = max(df["start_timestamp"].tolist())

    start_frames = [row.frame_pos + round((sync_time - row.start_timestamp) * row.fps) for index, row in df.iterrows()]
    old_start_frames = [row.frame_pos + int((sync_time - row.start_timestamp) * row.fps) for index, row in df.iterrows()]

    nb_crop_frames = min([nb_frames - start_frame for nb_frames, start_frame in zip(df["nb_frames"].tolist(), start_frames)])
    start_seconds = [start_frame / fps for start_frame, fps in zip(start_frames, df["fps"].tolist())]
    old_start_seconds = [start_frame / fps for start_frame, fps in zip(old_start_frames, df["fps"].tolist())]

    os.makedirs(args.out_dir, exist_ok=True)

    month, day = list(map(int, args.date.split("/")))
    out_paths = []
    for (index, row), start_sec, old_sec in zip(df.iterrows(), start_seconds, old_start_seconds):
        # date, participant id, task, take, camera
        # 0915_1_1_1_1.mp4
        video_path = osp.join(args.root_dir, row.video_path)
        out_id = f"{month:02d}_{day:02d}_{row.participant_id:02d}_{row.task:02d}_{row['take']:02d}_{row.camera_id}"
        out_path = osp.join(args.out_dir, f"{out_id}.mp4")
        out_paths.append(out_path)

        invalid = False
        for blacklist_id in blacklist:
            if out_id.startswith(blacklist_id):
                print(f"Invalid recording, skip: {out_id}")
                invalid = True
        if invalid:
            continue

        # Keep original codec, resolution, and frame rate; only trim in time.
        duration = nb_crop_frames / row.fps
        # -ss before -i: cleaner GOP boundary with -c copy (reduces black frames at start).
        command_str = (
            f"ffmpeg -ss {start_sec} -i {video_path} -n -loglevel error -t {duration} "
            f"-c copy -avoid_negative_ts make_zero {out_path}"
        )
        if int(row.camera_id) in args.process_id_list:
            print(command_str)
            if not args.debug:
                subprocess.run(command_str.split(" "))

    # Visualization
    # XXX Assume 2 row x 3 col view for now
    if args.vis:
        out_id = f"{month:02d}_{day:02d}_{row.participant_id:02d}_{row.task:02d}_{row['take']:02d}"
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
        vis_video_paths = [
            synced_path
            for synced_path, (_, cam_row) in zip(out_paths, df.iterrows())
            if int(cam_row.camera_id) in args.camera_id_list
        ]
        if len(vis_video_paths) != 6:
            print(f"[WARN] tile vis expects 6 cameras, got {len(vis_video_paths)}; skip vis for {out_id}")
            return

        tw = int(args.vis_tile_width)
        input_str = " ".join([f"-i {video_path}" for video_path in vis_video_paths])
        filter_complex = (
            f"[0:v]scale={tw}:-2[v1];[1:v]scale={tw}:-2[v2];[2:v]scale={tw}:-2[v3];"
            f"[3:v]scale={tw}:-2[v4];[4:v]scale={tw}:-2[v5];[5:v]scale={tw}:-2[v6];"
            f"[v1][v2][v3][v4][v5][v6]xstack=inputs=6:layout=0_0|w0+w4_0|0_h0|w0+w4_h0|w0_h0|w0_0:shortest=1[v]"
        )
        encode_opts = (
            f"-c:v libx264 -preset {args.vis_preset} -crf {int(args.vis_crf)} -pix_fmt yuv420p "
            f"-c:a aac -b:a {int(args.vis_audio_kbps)}k"
        )
        if args.vis_fps > 0:
            encode_opts += f" -r {float(args.vis_fps)}"
        command_str = (
            f'ffmpeg {input_str} -n -loglevel error -filter_complex "{filter_complex}" '
            f'-map "[v]" -map 4:a {encode_opts} {vis_out_path}'
        )
        print(command_str)
        if not args.debug:
            subprocess.run(command_str, shell=True)


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
        help='AAC bitrate for tile vis audio (default 96).',
    )
    parser.add_argument(
        '--vis-fps',
        type=float,
        default=0.0,
        help='Output fps for tile vis; 0 keeps source fps (default 0).',
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
