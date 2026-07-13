#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shorts_render.py — 비블 '완성형' 세로 쇼츠(번인 MP4) 렌더러.
프리미어 '비블-쇼츠' 템플릿을 이식(2026-07, 프리미어 화면 실측 + 비블 지정 스펙):

  · 얼굴: 웹캠 크롭 → 풀와이드, 세로 중앙 + 위아래 여백 동일(각 MARGIN px)
  · 제목: Paperlogy 9Black 106~118(글자수 적응), 2줄, 1줄 흰색 + 강조줄 노랑, 상단 밴드
  · 자막: Noto Sans KR Black 90, 얼굴 아래, 흰색+외곽선, 7자 이내 '맥락' 단위(직립)
  · 워터마크: '비블 bibl' MaruBuriOTF SemiBold 기울임 70, 하단 여백 중앙

폰트는 engine/assets/fonts 에 번들(Paperlogy OFL / MaruBuri Naver / Noto OFL).

사용:
  python3 shorts_render.py <source.mp4> <words.json> <clips.json> [--crop w:h:x:y] [--out DIR]
  clips.json: [{"name","start","end","hook","yellow"(1|2, 선택)}, ...]  (start/end = source 초)
  --crop: 얼굴 웹캠 영역(소스 픽셀). 기본은 비블 슬라이드+PIP 셋업 값.
          풀프레임 토킹헤드 소스면 얼굴에 맞게 조정(예: 전체 프레임이면 w=1080:h=1080:x=420:y=0 식).
"""
import sys, os, json, subprocess, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
FONTS = os.path.join(HERE, "assets", "fonts")

W, H = 1080, 1920
VID_H = 1500              # 영상(얼굴) 세로 크기 — 크게(더 확대). 세로 중앙 + 위아래 동일 여백.
CAP_FRAC = 0.70           # 얼굴 아래(가슴 위) 자막 세로 위치(영상 영역 비율)
WATERMARK = "비블 bibl"
YELLOW = r"&H0022CCFF&"    # 골드 옐로 (BGR: R255 G204 B34)
WHITE = r"&H00FFFFFF&"
GRAY = r"&H00CFCFCF&"
# 폰트: PostScript/가중치별 패밀리명으로 지정(안 그러면 macOS CoreText가 Regular로 폴백 → 얇아짐)
F_TITLE = "Paperlogy 9 Black"
F_CAP = "Noto Sans CJK KR Black"
F_WM = "MaruBuriot-SemiBold"
# 비블 슬라이드+PIP 소스 기본 웹캠(얼굴) 영역. 이 안에서 9:16-세로로 더 확대 크롭.
DEFAULT_CROP = dict(w=430, h=486, x=1486, y=474)


def kchars(s):
    return len(s.replace(" ", ""))


def chunk_captions(words, max_chars=7):
    """7자 이내 '맥락' 단위. 단어 경계에서만(공백 유지) 끊되, 종결/연결어미 뒤를 우선 분절."""
    caps, cur = [], []
    ENDERS = ("다", "요", "죠", "고", "서", "은", "는", "을", "를", "의", "도", "만", "까", "네")
    for i, w in enumerate(words):
        cur.append(w)
        cur_txt = " ".join(x[2] for x in cur)
        nxt = words[i + 1] if i + 1 < len(words) else None
        over = nxt and kchars(cur_txt) + kchars(nxt[2]) > max_chars   # 다음 단어 넣으면 초과 → 여기서 끊음
        end_ok = w[2].rstrip().rstrip(".").endswith(ENDERS) and kchars(cur_txt) >= 4
        if nxt is None or over or end_ok:
            caps.append([cur[0][0], cur[-1][1], cur_txt]); cur = []
    if cur:
        caps.append([cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)])
    return caps


def ass_time(t):
    t = max(0, t)
    return f"{int(t//3600):d}:{int((t%3600)//60):02d}:{t%60:05.2f}"


def split2(hook):
    if " " not in hook:
        return hook, ""
    ws = hook.split(" "); best = None
    for i in range(1, len(ws)):
        a, b = " ".join(ws[:i]), " ".join(ws[i:]); d = abs(kchars(a) - kchars(b))
        if best is None or d < best[0]:
            best = (d, a, b)
    return best[1], best[2]


def zoom_crop(crop):
    """얼굴 영역(crop) 안에서 풀와이드(W)×VID_H 비율로 더 확대해 잘라낸다.
    좌우는 꽉 채우고(비지 않게), 위아래를 필요한 만큼 잘라 얼굴을 크게."""
    aspect = W / VID_H                                    # 목표 영상 가로세로비 (0.72)
    zw = min(crop["w"], round(crop["h"] * aspect))
    zh = min(crop["h"], round(zw / aspect))
    zx = crop["x"] + (crop["w"] - zw) // 2
    zy = crop["y"] + (crop["h"] - zh) // 2                # 얼굴 중앙 기준 위아래 균등 크롭
    return dict(w=int(zw), h=int(zh), x=int(zx), y=int(zy))


def geometry():
    """풀와이드 + 세로 중앙 + 위아래 동일 여백."""
    margin = (H - VID_H) // 2
    return W, VID_H, margin


def build_ass(hook, caps, dur, yellow_line=2):
    vid_w, vid_h, margin = geometry()
    cap_y = margin + int(vid_h * CAP_FRAC)
    wm_y = H - margin // 2
    l1, l2 = split2(hook)
    maxlen = max(kchars(l1), kchars(l2))
    tsize = 120 if maxlen <= 8 else (114 if maxlen <= 10 else 108)   # 지정 110~120
    c1 = YELLOW if yellow_line == 1 else WHITE
    c2 = YELLOW if yellow_line == 2 else WHITE
    title = f"{{\\c{c1}}}{l1}" + (f"\\N{{\\c{c2}}}{l2}" if l2 else "")
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{F_TITLE},{tsize},&H00FFFFFF,&H000000FF,&H00141414,&H64000000,0,0,0,0,100,100,0,0,1,3,3,8,40,40,0,1
Style: Cap,{F_CAP},90,&H00FFFFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0.5,0,1,6,2,5,40,40,0,1
Style: WM,{F_WM},70,{GRAY},&H000000FF,&H00000000,&H00000000,0,-1,0,0,100,100,1,0,1,0,1,5,40,40,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = [f"Dialogue: 0,{ass_time(0)},{ass_time(dur)},Title,,0,0,0,,{{\\pos({W//2},40)}}{title}",
          f"Dialogue: 0,{ass_time(0)},{ass_time(dur)},WM,,0,0,0,,{{\\pos({W//2},{wm_y})\\fax0.12}}{WATERMARK}"]
    for s, e, txt in caps:
        ev.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{{\\pos({W//2},{cap_y})}}{txt.strip()}")
    return head + "\n".join(ev) + "\n"


def render(src, words, clip, crop, outdir):
    name = clip["name"]; start = float(clip["start"]); end = float(clip["end"])
    ws = [w for w in words if w[0] >= start - 0.4 and w[1] <= end + 0.4 and w[0] < end]
    if not ws:
        print(f"[{name}] 구간 내 단어 없음 — 건너뜀"); return None
    c_start, c_end = ws[0][0], ws[-1][1] + 0.25
    dur = c_end - c_start
    rel = [[w[0] - c_start, w[1] - c_start, w[2]] for w in ws]
    caps = chunk_captions(rel)
    vid_w, vid_h, margin = geometry()
    zc = zoom_crop(crop)                                  # 얼굴 더 확대(풀와이드, 위아래 크롭)
    ass_path = os.path.join(outdir, name + ".ass")
    open(ass_path, "w", encoding="utf-8").write(
        build_ass(clip["hook"], caps, dur, clip.get("yellow", 2)))
    out_path = os.path.join(outdir, name + ".mp4")
    vf = (f"crop={zc['w']}:{zc['h']}:{zc['x']}:{zc['y']},"
          f"scale={vid_w}:{vid_h}:flags=lanczos,unsharp=5:5:0.9:5:5:0.0,"
          f"pad={W}:{H}:0:{margin}:color=black,"
          f"ass={ass_path}:fontsdir={FONTS}")
    cmd = ["ffmpeg", "-y", "-ss", f"{c_start:.3f}", "-to", f"{c_end:.3f}", "-i", src,
           "-vf", vf, "-af", "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000",
           "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-r", "30", out_path]
    print(f"[{name}] {ass_time(c_start)}~{ass_time(c_end)} ({dur:.0f}s) 자막 {len(caps)}개 → 렌더...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("  에러:\n" + r.stderr[-1200:]); return None
    print(f"  완료 → {out_path}")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source"); ap.add_argument("words"); ap.add_argument("clips")
    ap.add_argument("--crop", default=None, help="w:h:x:y (얼굴 웹캠 영역, 소스 픽셀)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    crop = DEFAULT_CROP
    if a.crop:
        w, h, x, y = (int(v) for v in a.crop.split(":")); crop = dict(w=w, h=h, x=x, y=y)
    outdir = a.out or os.path.join(os.path.dirname(a.source) or ".", "shorts")
    os.makedirs(outdir, exist_ok=True)
    words = [tuple(x) for x in json.load(open(a.words, encoding="utf-8"))]
    clips = json.load(open(a.clips, encoding="utf-8"))
    for c in clips:
        render(a.source, words, c, crop, outdir)


if __name__ == "__main__":
    main()
