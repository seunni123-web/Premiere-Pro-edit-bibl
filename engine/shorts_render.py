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
# ── 템플릿 지오메트리(비블 확정 레퍼런스 실측, 2026-07) ──
VID_H = 1030              # 영상(얼굴) 세로 크기
VID_Y = 532               # 영상 상단(위 여백 = 제목 존)
TITLE_Y = 250             # 제목 블록 상단(an8)
CAP_FRAC = 0.62           # 자막 세로 위치(영상 영역 비율 — 얼굴 아래)
WM_GAP = 55               # 워터마크: 영상 바로 아래 간격
WATERMARK = "비블 bibl"
YELLOW = r"&H0022CCFF&"    # 골드 옐로 (BGR: R255 G204 B34)
WHITE = r"&H00FFFFFF&"
GRAY = r"&H00CFCFCF&"
# 폰트: PostScript/가중치별 패밀리명으로 지정(안 그러면 macOS CoreText가 Regular로 폴백 → 얇아짐)
F_TITLE = "Paperlogy 9 Black"
F_CAP = "Noto Sans CJK KR Black"
F_WM = "MaruBuriot-SemiBold"
# 비블 슬라이드+PIP 소스 기본 웹캠(얼굴) 영역.
# INSET: PIP의 흰 라운드 테두리가 화면에 걸리지 않게 안쪽으로 파고 들어가 크롭.
DEFAULT_CROP = dict(w=430, h=486, x=1486, y=474)
CROP_INSET = 14


def kchars(s):
    return len(s.replace(" ", ""))


# 자막 끝에 매달리면 안 되는 말(관형사/접속사/불완전 조각) — 다음 자막으로 이월
DANGLING = {"그", "이", "저", "한", "내", "제", "뭐", "좀", "더", "안", "못", "왜", "또",
            "그러면", "그리고", "근데", "그래서", "이제", "일단", "이런", "저런", "어떤", "무슨"}


def chunk_captions(words, min_chars=5, max_chars=9):
    """5~9자 '맥락' 단위 자막.
    규칙: ① 문장 끝(.?!)에서 무조건 끊는다(두 문장이 한 자막에 안 섞임)
         ② 숫자 조각은 병합("0"+".4초"→"0.4초")
         ③ 관형사/접속사로 자막이 끝나면 다음 자막으로 이월("…한", "…그러면 그" 방지)"""
    merged = []
    for w in words:
        t = w[2]
        if merged:
            p = merged[-1][2]
            near = w[0] - merged[-1][1] < 0.35
            if near and (t.startswith(".") or (p and p[-1].isdigit() and t[0].isdigit())):
                merged[-1] = [merged[-1][0], w[1], p + t]; continue
        merged.append([w[0], w[1], t])
    caps, cur = [], []

    def flush():
        nonlocal cur
        if cur:
            caps.append([cur[0][0], cur[-1][1], " ".join(x[2] for x in cur)]); cur = []

    for i, w in enumerate(merged):
        cur.append(w)
        n = kchars(" ".join(x[2] for x in cur))
        nxt = merged[i + 1] if i + 1 < len(merged) else None
        if w[2].rstrip().endswith((".", "?", "!")):       # 문장 경계 — 무조건 분절
            flush(); continue
        if nxt is None:
            flush(); continue
        if n + kchars(nxt[2]) > max_chars:                # 다음 단어를 넣으면 9자 초과
            if len(cur) > 1 and w[2].strip() in DANGLING:
                d = cur.pop(); flush(); cur = [d]         # 매달린 말은 다음 자막 머리로
            else:
                flush()
    return caps


def ass_time(t):
    t = max(0, t)
    return f"{int(t//3600):d}:{int((t%3600)//60):02d}:{t%60:05.2f}"


def is_sent_end(words, i, gap_min=0.30):
    """words[i]가 문장 끝인가 — 구두점(.?!) 또는 종결어미(다/요/죠/까)+뒤 쉼."""
    t = words[i][2].rstrip()
    if t.endswith((".", "?", "!")):
        return True
    gap = (words[i + 1][0] - words[i][1]) if i + 1 < len(words) else 9.9
    return t.endswith(("다", "요", "죠", "까")) and gap >= gap_min


def snap_clip(words, start, end, min_dur=40.0, max_dur=60.0,
              start_exact=False, hard_end=None, end_exact=None):
    """구간을 문장 경계로 스냅. 시작=문장 첫 단어, 끝=min~max초 안의 마지막 문장 끝.
    '시간 맞추기'보다 '한 편이 말이 되는 것'이 우선 — 문장 끝이 없으면 최대 +8초까지 연장.
    start_exact: 후퇴 없이 start 직후 단어에서 그대로 시작(사용자 지정 시작).
    hard_end: 이 시각 이전의 마지막 문장 끝에서 무조건 종료(사용자 지정 끝)."""
    idx = [i for i, w in enumerate(words) if w[1] > start - 4 and w[0] < end + 12]
    if not idx:
        return start, end, "(단어 없음)"
    s_i = next((i for i in idx if words[i][0] >= start - 0.2), idx[0])
    if not start_exact:
        j = s_i
        while j - 1 >= 0 and words[s_i][0] - words[j - 1][0] <= 6.0 and not is_sent_end(words, j - 1):
            j -= 1
        s_i = j
    t0 = words[s_i][0]
    note = ""
    if end_exact is not None:                             # 사용자 지정 끝(문장 탐색 없이 그 지점 단어까지)
        e = [i for i in range(s_i, len(words)) if words[i][1] <= end_exact + 0.05]
        if e:
            return t0, words[e[-1]][1], "(정확 끝)"
    if hard_end is not None:                              # 사용자 지정 끝 — 그 안의 마지막 문장 끝
        cands = [i for i in range(s_i, len(words))
                 if words[i][1] <= hard_end + 0.05 and is_sent_end(words, i)]
        if cands:
            return t0, words[cands[-1]][1], "(지정 끝)"
    cands = [i for i in range(s_i, len(words))
             if min_dur <= words[i][1] - t0 <= max_dur and is_sent_end(words, i)]
    if cands:
        e_i = cands[-1]
    else:
        after = [i for i in range(s_i, len(words))
                 if max_dur < words[i][1] - t0 <= max_dur + 8 and is_sent_end(words, i)]
        if after:
            e_i = after[0]; note = f"(문장 완결 위해 {words[e_i][1]-t0:.0f}s로 연장)"
        else:
            e_i = max(i for i in range(s_i, len(words)) if words[i][1] - t0 <= max_dur)
            note = "(문장 끝 못 찾음 — 확인 필요)"
    return t0, words[e_i][1], note


def split2(hook):
    if " " not in hook:
        return hook, ""
    ws = hook.split(" "); best = None
    for i in range(1, len(ws)):
        a, b = " ".join(ws[:i]), " ".join(ws[i:]); d = abs(kchars(a) - kchars(b))
        if best is None or d < best[0]:
            best = (d, a, b)
    return best[1], best[2]


def detect_pip(src, t):
    """t초 프레임에서 웹캠 PIP의 흰 라운드 테두리 박스를 감지(밝기>225의 긴 직선 런).
    성공 시 테두리 외곽 박스 dict(w,h,x,y), 실패 시 None."""
    try:
        import numpy as np
        r = subprocess.run(['ffmpeg', '-ss', str(t), '-i', src, '-frames:v', '1',
                            '-f', 'rawvideo', '-pix_fmt', 'gray', '-'],
                           capture_output=True)
        g = np.frombuffer(r.stdout, dtype=np.uint8)
        if g.size < 1920 * 1080:
            return None
        g = g[:1920 * 1080].reshape(1080, 1920)
        bright = g > 225

        def runs(mask_2d, axis_len, other_len, get_line):
            hits = []
            for i in range(axis_len):
                line = get_line(i)
                best = cur = 0
                for v in line:
                    cur = cur + 1 if v else 0
                    best = max(best, cur)
                if best >= 220:
                    hits.append(i)
            return hits

        rows = runs(bright, 1080, 1920, lambda y: bright[y])
        cols = runs(bright, 1920, 1080, lambda x: bright[:, x])
        if not rows or not cols:
            return None
        x0, x1 = min(cols), max(cols)
        y0, y1 = min(rows), max(rows)
        if x1 - x0 < 200 or y1 - y0 < 200:                # 너무 작으면 오탐
            return None
        return dict(w=x1 - x0, h=y1 - y0, x=x0, y=y0)
    except Exception:
        return None


def zoom_crop(crop, inset=CROP_INSET):
    """얼굴 영역(crop)에서 테두리 인셋만큼 안으로 들어간 뒤, 풀와이드(W)×VID_H 비율로 잘라낸다.
    좌우는 꽉 채우고(비지 않게), 위아래를 잘라 얼굴 크게 + PIP 흰 라운드 테두리 제거."""
    cw, ch = crop["w"] - 2 * inset, crop["h"] - 2 * inset
    cx, cy = crop["x"] + inset, crop["y"] + inset
    aspect = W / VID_H                                    # 목표 가로세로비 (1080/1030)
    zw = min(cw, round(ch * aspect))
    zh = min(ch, round(zw / aspect))
    zx = cx + (cw - zw) // 2
    zy = cy + (ch - zh) // 2                              # 얼굴 중앙 기준 위아래 균등 크롭
    return dict(w=int(zw), h=int(zh), x=int(zx), y=int(zy))


def geometry():
    """템플릿 배치: 위 여백(제목 존) VID_Y, 영상 VID_H, 아래 여백 나머지."""
    return W, VID_H, VID_Y


def build_ass(hook, caps, dur, yellow_line=2):
    vid_w, vid_h, vid_y = geometry()
    cap_y = vid_y + int(vid_h * CAP_FRAC)
    wm_y = vid_y + vid_h + WM_GAP
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
    ev = [f"Dialogue: 0,{ass_time(0)},{ass_time(dur)},Title,,0,0,0,,{{\\pos({W//2},{TITLE_Y})}}{title}",
          f"Dialogue: 0,{ass_time(0)},{ass_time(dur)},WM,,0,0,0,,{{\\pos({W//2},{wm_y})\\fax0.12}}{WATERMARK}"]
    for s, e, txt in caps:
        ev.append(f"Dialogue: 0,{ass_time(s)},{ass_time(e)},Cap,,0,0,0,,{{\\pos({W//2},{cap_y})}}{txt.strip()}")
    return head + "\n".join(ev) + "\n"


def _apply_fix(t, fixes):
    for k, v in fixes.items():
        t = t.replace(k, v)
    return t


def render(src, words, clip, crop, outdir):
    name = clip["name"]; start = float(clip["start"]); end = float(clip["end"])
    # 문장 경계 스냅: 시작=문장 첫 단어, 끝=40~60초 안 마지막 문장 끝(말이 되는 게 우선)
    t0, t1, note = snap_clip(words, start, end,
                             max_dur=float(clip.get("max_dur", 60)),
                             start_exact=bool(clip.get("start_exact")),
                             hard_end=clip.get("hard_end"),
                             end_exact=clip.get("end_exact"))
    ws = [w for w in words if t0 - 0.05 <= w[0] and w[1] <= t1 + 0.05]
    if not ws:
        print(f"[{name}] 구간 내 단어 없음 — 건너뜀"); return None
    tail_txt = " ".join(w[2] for w in ws[-6:])
    print(f"[{name}] 문장 스냅 {note}  끝맺음: \"…{tail_txt}\"")
    # 스마트 테일: 다음 발화 직전까지만(뒤 문장 첫음 '그래/특' 새어들기 방지) + 끝 페이드
    last_end = ws[-1][1]
    nxt = next((w for w in words if w[0] > last_end + 0.001), None)
    tail = 0.45 if nxt is None else max(0.05, min(0.45, nxt[0] - last_end - 0.08))
    c_start, c_end = ws[0][0], last_end + tail
    # force_start: 발화 온셋 직전으로 강제(첫 단어 앞의 '아~'/숨을 잘라냄 — STT 단어 시각이 앞으로 패딩된 경우)
    fs = clip.get("force_start")
    if fs is not None and fs > c_start:
        c_start = fs
        ws = [w for w in ws if w[1] > fs + 0.05]
    # 구간 제거(숨소리 등): clips.json "remove": [[소스초, 소스초], ...]
    removes = sorted([max(c_start, a), min(c_end, b)] for a, b in (clip.get("remove") or []))
    removes = [r for r in removes if r[1] - r[0] > 0.02]
    dur = (c_end - c_start) - sum(b - a for a, b in removes)
    rel = [[max(0.0, w[0] - c_start), max(0.06, w[1] - c_start), w[2]] for w in ws]
    for a, b in sorted(((a - c_start, b - c_start) for a, b in removes), reverse=True):
        cut = b - a
        rel = [[(s - cut if s >= b else s), (e - cut if e >= b else min(e, a)), t]
               for s, e, t in rel if not (a <= s and e <= b)]
    fixes = clip.get("fix") or {}
    if fixes:
        rel = [[s, e, _apply_fix(t, fixes)] for s, e, t in rel]
        rel = [w for w in rel if w[2].strip()]
    caps = chunk_captions(rel)
    if fixes:
        caps = [[s, e, _apply_fix(t, fixes)] for s, e, t in caps]
    vid_w, vid_h, vid_y = geometry()
    zc = zoom_crop(crop)
    ass_path = os.path.join(outdir, name + ".ass")
    open(ass_path, "w", encoding="utf-8").write(
        build_ass(clip["hook"], caps, dur, clip.get("yellow", 2)))
    out_path = os.path.join(outdir, name + ".mp4")
    # 업로드 방탄: fps=30(CFR 강제·PTS 재생성)로 유튜브/인스타 '앞부분 빨리감기' 원천 차단
    vchain = (f"crop={zc['w']}:{zc['h']}:{zc['x']}:{zc['y']},"
              f"scale={vid_w}:{vid_h}:flags=lanczos,unsharp=5:5:0.9:5:5:0.0,"
              f"fps=30,pad={W}:{H}:0:{vid_y}:color=black,"
              f"ass={ass_path}:fontsdir={FONTS},setpts=PTS-STARTPTS")
    achain = (f"loudnorm=I=-14:TP=-1.5:LRA=11,"
              f"aresample=48000:async=1:first_pts=0,"
              f"afade=t=out:st={max(0.0, dur - 0.22):.2f}:d=0.22,"
              f"asetpts=PTS-STARTPTS")
    # 제거 구간 반영: keep 세그먼트를 trim/concat (제거 없으면 단일 세그먼트)
    segs, pos = [], 0.0
    for a, b in ((a - c_start, b - c_start) for a, b in removes):
        if a > pos:
            segs.append((pos, a))
        pos = b
    if c_end - c_start > pos:
        segs.append((pos, c_end - c_start))
    # ── 핵심: -copyts + 절대시간 trim ──
    # 입력 시킹(-ss)은 키프레임으로 점프하며 시크 지점 이전 '프리롤' 프레임을 흘려보낼 수 있고,
    # 그 프레임들이 첫 2~3초 빨리감기(내용이 앞당겨 압축 재생)로 나타난다.
    # -copyts로 원본 절대 타임스탬프를 유지하면 trim=start=<절대초>가 프리롤을 결정적으로 잘라낸다.
    parts, cc = [], ""
    for i, (a, b) in enumerate(segs):
        parts.append(f"[0:v]trim=start={c_start + a:.3f}:end={c_start + b:.3f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim=start={c_start + a:.3f}:end={c_start + b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        cc += f"[v{i}][a{i}]"
    fc = (";".join(parts) + f";{cc}concat=n={len(segs)}:v=1:a=1[vc][ac];"
          f"[vc]{vchain}[vout];[ac]{achain}[aout]")
    seek = max(0.0, c_start - 6.0)                       # 키프레임 여유(프리롤은 trim이 자름)
    cmd = ["ffmpeg", "-y", "-ss", f"{seek:.3f}", "-to", f"{c_end + 0.5:.3f}", "-i", src,
           "-copyts",
           "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p",
           "-bf", "0",  # B프레임 off: dts=pts → 시작 0.000 보장(업로드 플랫폼 최무해)
           "-c:a", "aac", "-b:a", "192k",
           "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out_path]
    rm = f" 제거{len(removes)}곳" if removes else ""
    print(f"[{name}] {ass_time(c_start)}~{ass_time(c_end)} ({dur:.0f}s) 자막 {len(caps)}개{rm} → 렌더...")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("  에러:\n" + r.stderr[-1200:]); return None
    ok, msg = verify_timestamps(out_path)
    ok2, msg2 = verify_content(out_path, src, c_start, zc, vid_w, vid_h)
    print(f"  {'완료' if (ok and ok2) else '[검증실패!]'} {msg} {msg2} → {out_path}")
    return out_path if (ok and ok2) else None


def _grab_gray(path, t, vf, w, h):
    import numpy as np
    r = subprocess.run(["ffmpeg", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                        "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                       capture_output=True)
    g = np.frombuffer(r.stdout, dtype=np.uint8)
    return g[:w * h].reshape(h, w).astype(int) if g.size >= w * h else None


def verify_content(out_path, src, c_start, zc, vid_w, vid_h):
    """내용 검증: 출력 첫 부분 프레임이 소스의 '같은 시점' 프레임과 일치하는지(빨리감기/프리롤 오염 감지).
    출력 영상영역 상단(얼굴)과 소스 zoom_crop 동일 영역을 픽셀 대조(MAD)."""
    try:
        import numpy as np
        half_h = 480                                       # 영상영역 상단(자막·제목 안 겹침)
        checks = []
        for t in (0.5, 1.5, 2.5):
            o = _grab_gray(out_path, t,
                           f"crop={W}:{half_h}:0:{VID_Y}, scale=96:64", 96, 64)
            s = _grab_gray(src, c_start + t,
                           f"crop={zc['w']}:{int(zc['h']*half_h/vid_h)}:{zc['x']}:{zc['y']},scale=96:64",
                           96, 64)
            if o is None or s is None:
                return False, "(내용검증 프레임 실패)"
            checks.append(float(np.abs(o - s).mean()))
        bad = [c for c in checks if c > 26]
        ok = len(bad) == 0
        return ok, f"(내용 MAD={','.join(f'{c:.0f}' for c in checks)}{' 불일치!' if not ok else ''})"
    except Exception as e:
        return False, f"(내용검증 오류: {e})"


def verify_timestamps(path):
    """업로드 안전성 검증: 영상/오디오 start=0, 음수 PTS 0개, CFR 균일."""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                            "-show_entries", "packet=pts_time", "-of", "csv=p=0", path],
                           capture_output=True, text=True)
        ts = sorted(float(x.strip().rstrip(",")) for x in r.stdout.splitlines()
                    if x.strip().rstrip(","))
        neg = sum(1 for t in ts if t < -0.001)
        gaps = [round(b - a, 4) for a, b in zip(ts, ts[1:])]
        irregular = sum(1 for g in gaps if abs(g - 1 / 30) > 0.003)
        ra = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                             "-show_entries", "stream=start_time", "-of", "csv=p=0", path],
                            capture_output=True, text=True)
        a0 = float(ra.stdout.strip().rstrip(","))
        # 시작 오프셋 1프레임(33ms) 이내는 표준(AAC priming 상쇄) — 빨리감기 원인은 음수/불균일 PTS
        ok = abs(ts[0]) < 0.034 and neg == 0 and irregular == 0 and -0.001 <= a0 < 0.05
        return ok, (f"(검증: v0={ts[0]:.3f} a0={a0:.3f} 음수{neg} 불균일{irregular})")
    except Exception as e:
        return False, f"(검증 오류: {e})"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source"); ap.add_argument("words"); ap.add_argument("clips")
    ap.add_argument("--crop", default=None, help="w:h:x:y (얼굴 웹캠 영역, 소스 픽셀)")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    cli_crop = None
    if a.crop:
        w, h, x, y = (int(v) for v in a.crop.split(":")); cli_crop = dict(w=w, h=h, x=x, y=y)
    outdir = a.out or os.path.join(os.path.dirname(a.source) or ".", "shorts")
    os.makedirs(outdir, exist_ok=True)
    words = [tuple(x) for x in json.load(open(a.words, encoding="utf-8"))]
    clips = json.load(open(a.clips, encoding="utf-8"))
    for c in clips:
        # 크롭 우선순위: --crop 명시 > 클립 중간 시점 PIP 자동 감지 > 기본값
        crop = cli_crop
        if crop is None:
            mid = (float(c["start"]) + float(c["end"])) / 2
            crop = detect_pip(a.source, mid)
            if crop:
                print(f"[{c['name']}] PIP 자동 감지: x={crop['x']} y={crop['y']} "
                      f"w={crop['w']} h={crop['h']}")
            else:
                crop = DEFAULT_CROP
                print(f"[{c['name']}] PIP 감지 실패 → 기본 크롭 사용(확인 필요)")
        render(a.source, words, c, crop, outdir)


if __name__ == "__main__":
    main()
