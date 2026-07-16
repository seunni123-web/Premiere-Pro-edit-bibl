#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shorts_premiere.py — 비블 쇼츠를 '프리미어에서 수정 가능한 XML'로 생성 (번인 MP4 아님).

구성(시퀀스 1080x1920, 30fps — 클립 8개가 XML 한 파일에 시퀀스 8개로 열림):
  V1  얼굴 클립 MP4 (텍스트 없음 — 크롭·컷·음량 처리 완료, 풀프레임 1080x1920)
  V2  자막 PNG (발화 싱크, 자막 하나 = 클립 하나)
  V3  키워드 그래픽 오버레이 PNG (말하는 타이밍)
  V4  제목 PNG (2줄, 흰/노랑)
  V5  워터마크 PNG (비블 bibl)
  A1  얼굴 클립 오디오 (V1과 링크)

모든 PNG는 1080x1920 풀캔버스(요소가 제자리에 배치됨) → Motion 파라미터 불필요,
프리미어에서 위치/타이밍/삭제 자유. 자막 텍스트 자체를 다시 치고 싶으면 자막SRT/ 임포트.

사용:
  python3 shorts_premiere.py <source.mp4> <words.json> <clips.json> [--out DIR] [--only 이름조각] [--force]
출력: <out>/비블쇼츠_프리미어.xml + 클립/*.mp4 + 그래픽/**.png + 자막SRT/*.srt
"""
import sys, os, json, subprocess, argparse
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from shorts_render import (W, H, VID_H, VID_Y, TITLE_Y, CAP_FRAC, WM_GAP, WATERMARK,
                           FONTS, DEFAULT_CROP, CROP_INSET, kchars, split2, prepare_clip,
                           detect_pip_robust, zoom_crop, src_duration, ass_time,
                           verify_timestamps, verify_content, verify_audio_head)

FPS = 30
F_TITLE_P = os.path.join(FONTS, "Paperlogy-9Black.ttf")
F_CAP_P = os.path.join(FONTS, "NotoSansCJKkr-Black.otf")
F_WM_P = os.path.join(FONTS, "MaruBuri-SemiBold.otf")
YELLOW = (255, 204, 34)
WHITE = (255, 255, 255)
GRAY = (207, 207, 207)
CAP_Y = VID_Y + int(VID_H * CAP_FRAC)          # 1170
WM_Y = VID_Y + VID_H + WM_GAP                  # 1617
OV_Y = 1775                                     # 오버레이: 하단 검정 존(워터마크 아래)

# 키워드 오버레이는 기본 생성하지 않음(2026-07-16 비블 "필요 없어" 확정).
# 명시 요청 시에만 clips.json "overlays": [["찾을 문구","표시 문구"],...]로 켠다.


# ───────────────────────── PNG 텍스트 렌더 (PIL) ─────────────────────────

def _font(path, size):
    """ASS(libass) 사이즈 정규화: libass는 fontsize를 행 높이(ascent+descent)로 맞춘다.
    같은 숫자로 PIL에 그대로 주면 ~15% 크게 렌더되므로 동일 스케일로 보정."""
    from PIL import ImageFont
    f0 = ImageFont.truetype(path, size)
    a, d = f0.getmetrics()
    return ImageFont.truetype(path, max(1, round(size * size / (a + d))))


def _canvas():
    from PIL import Image
    return Image.new("RGBA", (W, H), (0, 0, 0, 0))


def _draw_text(img, xy, txt, font, fill, stroke=0, stroke_fill=(16, 16, 16, 255),
               shadow=0, shadow_alpha=110, anchor="mm"):
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    if shadow:
        d.text((xy[0] + shadow, xy[1] + shadow), txt, font=font, anchor=anchor,
               fill=(0, 0, 0, shadow_alpha), stroke_width=stroke,
               stroke_fill=(0, 0, 0, shadow_alpha))
    d.text(xy, txt, font=font, anchor=anchor, fill=fill,
           stroke_width=stroke, stroke_fill=stroke_fill)


def png_title(hook, yellow_line, path):
    """제목 2줄 — Paperlogy 9Black, 1줄 흰/강조줄 노랑, 상단 밴드(an8=위 중앙)."""
    l1, l2 = split2(hook)
    maxlen = max(kchars(l1), kchars(l2))
    tsize = 120 if maxlen <= 8 else (114 if maxlen <= 10 else 108)
    f = _font(F_TITLE_P, tsize)
    img = _canvas()
    c1 = YELLOW if yellow_line == 1 else WHITE
    c2 = YELLOW if yellow_line == 2 else WHITE
    _draw_text(img, (W // 2, TITLE_Y), l1, f, c1, stroke=3,
               stroke_fill=(20, 20, 20, 255), shadow=3, anchor="ma")
    if l2:                                     # 행 높이 = ASS fontsize
        _draw_text(img, (W // 2, TITLE_Y + tsize), l2, f, c2, stroke=3,
                   stroke_fill=(20, 20, 20, 255), shadow=3, anchor="ma")
    img.save(path)


def png_caption(txt, path):
    """자막 — Noto Sans KR Black 90, 흰색+외곽선, 얼굴 아래 중앙."""
    img = _canvas()
    _draw_text(img, (W // 2, CAP_Y), txt.strip(), _font(F_CAP_P, 90), WHITE,
               stroke=6, stroke_fill=(16, 16, 16, 255), shadow=2, shadow_alpha=128)
    img.save(path)


def png_watermark(path):
    """워터마크 — MaruBuri SemiBold 70 기울임, 회색, 영상 바로 아래 중앙."""
    from PIL import Image
    f = _font(F_WM_P, 70)
    pad = 60
    bx = f.getbbox(WATERMARK)
    tw, th = bx[2] - bx[0], bx[3] - bx[1]
    tmp = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    _draw_text(tmp, (tmp.width // 2, tmp.height // 2), WATERMARK, f,
               GRAY + (255,), shadow=1, shadow_alpha=90)
    k = 0.25                                   # 이탤릭 시어(위가 오른쪽으로)
    sheared = tmp.transform((tmp.width + int(k * tmp.height), tmp.height),
                            Image.AFFINE, (1, k, -k * tmp.height / 2, 0, 1, 0),
                            resample=Image.BICUBIC)
    img = _canvas()
    img.alpha_composite(sheared, (W // 2 - sheared.width // 2, WM_Y - sheared.height // 2))
    img.save(path)


def png_overlay(txt, path):
    """키워드 그래픽 오버레이 — Paperlogy 9Black 96 노랑, 하단 검정 존."""
    img = _canvas()
    _draw_text(img, (W // 2, OV_Y), txt, _font(F_TITLE_P, 96), YELLOW,
               stroke=6, stroke_fill=(10, 10, 10, 255), shadow=3)
    img.save(path)


# ───────────────────────── 얼굴 클립(텍스트 없음) 렌더 ─────────────────────────

def render_clean(src, p, zc, out_path):
    """번인 렌더와 동일 체인(크롭·컷·음량·방탄 인코딩)에서 ass 자막만 뺀 얼굴 클립."""
    c_start, c_end, dur, segs = p["c_start"], p["c_end"], p["dur"], p["segs"]
    # setsar=1 필수: crop→scale은 DAR 보존을 위해 비정규 SAR(예 1751:1752)을 심는데,
    # 프리미어가 임의 SAR을 오해석해 화면이 찌그러진다(2026-07-16 비블 리포트).
    vchain = (f"crop={zc['w']}:{zc['h']}:{zc['x']}:{zc['y']},"
              f"scale={W}:{VID_H}:flags=lanczos,unsharp=5:5:0.9:5:5:0.0,"
              f"fps={FPS},pad={W}:{H}:0:{VID_Y}:color=black,setsar=1,setpts=PTS-STARTPTS")
    achain = (f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
              f"afade=t=out:st={max(0.0, dur - 0.22):.2f}:d=0.22,asetpts=PTS-STARTPTS")
    parts, cc = [], ""
    for i, (a, b) in enumerate(segs):
        parts.append(f"[0:v]trim=start={c_start + a:.3f}:end={c_start + b:.3f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim=start={c_start + a:.3f}:end={c_start + b:.3f},asetpts=PTS-STARTPTS[a{i}]")
        cc += f"[v{i}][a{i}]"
    fc = (";".join(parts) + f";{cc}concat=n={len(segs)}:v=1:a=1[vc][ac];"
          f"[vc]{vchain}[vout];[ac]{achain}[aout]")
    seek = max(0.0, c_start - 6.0)
    cmd = ["ffmpeg", "-y", "-ss", f"{seek:.3f}", "-to", f"{c_end + 0.5:.3f}", "-i", src,
           "-copyts", "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-pix_fmt", "yuv420p",
           "-bf", "0", "-c:a", "aac", "-b:a", "192k",
           "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("  에러:\n" + r.stderr[-1200:]); return False
    ok, m1 = verify_timestamps(out_path)
    ok2, m2 = verify_content(out_path, src, c_start, zc, W, VID_H)
    ok3, m3 = verify_audio_head(out_path)
    print(f"  {'검증OK' if ok and ok2 and ok3 else '[검증실패!]'} {m1} {m2} {m3}")
    return ok and ok2 and ok3


# ───────────────────────── SRT ─────────────────────────

def srt_time(t):
    ms = int(round(max(0, t) * 1000))
    return f"{ms//3600000:02d}:{ms%3600000//60000:02d}:{ms%60000//1000:02d},{ms%1000:03d}"


def write_srt(caps, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, (s, e, txt) in enumerate(caps, 1):
            f.write(f"{i}\n{srt_time(s)} --> {srt_time(e)}\n{txt.strip()}\n\n")


# ───────────────────────── XML(xmeml) ─────────────────────────

def xesc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def purl(p):
    return "file://" + quote(os.path.abspath(p))


RATE = f"<rate><timebase>{FPS}</timebase><ntsc>FALSE</ntsc></rate>"
_filedefs = {}                                  # path → file id (첫 사용 시 전체 정의)


def file_ref(path, fid, kind, dur_f=None):
    """file 요소: 첫 사용은 전체 정의, 이후는 id 참조."""
    if path in _filedefs:
        return f'<file id="{_filedefs[path]}"/>'
    _filedefs[path] = fid
    name = xesc(os.path.basename(path))
    sc = (f'{RATE}<width>{W}</width><height>{H}</height>'
          f'<anamorphic>FALSE</anamorphic><pixelaspectratio>square</pixelaspectratio>'
          f'<fielddominance>none</fielddominance>')
    if kind == "av":
        media = (f'<media><video><samplecharacteristics>{sc}</samplecharacteristics></video>'
                 f'<audio><samplecharacteristics><depth>16</depth>'
                 f'<samplerate>48000</samplerate></samplecharacteristics>'
                 f'<channelcount>2</channelcount></audio></media>')
    else:                                       # png still
        media = f'<media><video><samplecharacteristics>{sc}</samplecharacteristics></video></media>'
    d = f"<duration>{dur_f}</duration>" if dur_f else ""
    return (f'<file id="{fid}"><name>{name}</name><pathurl>{xesc(purl(path))}</pathurl>'
            f'{RATE}{d}{media}</file>')


def clipitem(cid, name, fileel, start, end, cin, cout, links="", extra=""):
    return (f'<clipitem id="{cid}"><name>{xesc(name)}</name>{RATE}'
            f'<start>{start}</start><end>{end}</end><in>{cin}</in><out>{cout}</out>'
            f'{fileel}{extra}{links}</clipitem>')


def link(ref, mtype, tidx):
    return (f'<link><linkclipref>{ref}</linkclipref><mediatype>{mtype}</mediatype>'
            f'<trackindex>{tidx}</trackindex><clipindex>1</clipindex></link>')


def build_sequence(si, name, mp4, F, cap_items, ov_items, title_png, wm_png):
    vid, aid = f"s{si}v1", f"s{si}a1"
    links = link(vid, "video", 1) + link(aid, "audio", 1)
    v1 = clipitem(vid, name, file_ref(mp4, f"s{si}f", "av", F), 0, F, 0, F, links)
    a1 = clipitem(aid, name, file_ref(mp4, f"s{si}f", "av", F), 0, F, 0, F, links,
                  "<sourcetrack><mediatype>audio</mediatype><trackindex>1</trackindex></sourcetrack>")
    t_caps = "".join(
        clipitem(f"s{si}c{i}", txt.strip(), file_ref(p, f"s{si}cf{i}", "png", e - s),
                 s, e, 0, e - s)
        for i, (s, e, txt, p) in enumerate(cap_items))
    t_ovs = "".join(
        clipitem(f"s{si}o{i}", txt, file_ref(p, f"s{si}of{i}", "png", e - s),
                 s, e, 0, e - s)
        for i, (s, e, txt, p) in enumerate(ov_items))
    t_title = clipitem(f"s{si}t", "제목: " + name,
                       file_ref(title_png, f"s{si}tf", "png", F), 0, F, 0, F)
    t_wm = clipitem(f"s{si}w", WATERMARK, file_ref(wm_png, "wmf", "png", F), 0, F, 0, F)
    return f"""<sequence id="seq{si}">
  <name>{xesc(name)}</name>
  <duration>{F}</duration>
  {RATE}
  <media>
    <video>
      <format><samplecharacteristics>{RATE}<width>{W}</width><height>{H}</height>
        <pixelaspectratio>square</pixelaspectratio></samplecharacteristics></format>
      <track>{v1}</track>
      <track>{t_caps}</track>
      <track>{t_ovs}</track>
      <track>{t_title}</track>
      <track>{t_wm}</track>
    </video>
    <audio>
      <format><samplecharacteristics><depth>16</depth><samplerate>48000</samplerate></samplecharacteristics></format>
      <track>{a1}</track>
    </audio>
  </media>
</sequence>"""


def face_crop(timeline, c_start, c_end, removes):
    """풀프레임 토킹헤드 소스: 얼굴 타임라인(0.5s 샘플, 640x360 스케일)에서
    클립 구간 얼굴 중앙값 기준 고정 크롭. 하단은 소스 번인자막 존(y>950) 회피."""
    import statistics as st
    pts = [b for t, b in timeline
           if c_start <= t <= c_end and b and not any(a <= t <= r for a, r in removes)]
    if len(pts) < 4:
        return None
    cx = st.median([(b[0] + b[2] / 2) * 3 for b in pts])
    cy = st.median([(b[1] + b[3] / 2) * 3 for b in pts])
    fh = st.median([b[3] * 3 for b in pts])
    ch = max(520, min(700, int(round(fh * 2.4 / 2) * 2)))     # 얼굴이 세로 ~42%
    cw = int(round(ch * W / VID_H / 2) * 2)
    x0 = int(max(0, min(1920 - cw, cx - cw / 2)))
    y0 = int(max(0, min(1080 - ch, 950 - ch, cy - ch * 0.40)))
    return dict(w=cw, h=ch, x=x0, y=y0)


def pick_overlays(clip, caps, dur):
    """자막에서 키워드 문구를 찾아 (초 시작, 초 끝, 표시문구) — 겹치면 앞 것을 줄임.
    clips.json "overlays": [["찾을 문구","표시 문구"],...]가 있을 때만 생성(기본 없음)."""
    found = []
    pairs = clip.get("overlays") or []
    for search, disp in pairs:
        for s, e, txt in caps:
            if search.replace(" ", "") in txt.replace(" ", ""):
                found.append([s, min(dur, max(e, s + 2.2) + 0.6), disp])
                break
    found.sort()
    for i in range(len(found) - 1):
        if found[i][1] > found[i + 1][0]:
            found[i][1] = max(found[i][0] + 0.8, found[i + 1][0] - 0.08)
    return [f for f in found if f[1] - f[0] > 0.5]


# ───────────────────────── main ─────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source"); ap.add_argument("words"); ap.add_argument("clips")
    ap.add_argument("--out", default=None)
    ap.add_argument("--only", default=None, help="이름에 이 조각이 든 클립만")
    ap.add_argument("--force", action="store_true", help="얼굴 클립 MP4 재렌더")
    ap.add_argument("--crop", default=None, help="w:h:x:y 수동 크롭")
    ap.add_argument("--faces", default=None,
                    help="얼굴 타임라인 JSON([[t,[x,y,w,h,conf]|null],...], 640x360 스케일) — 풀프레임 소스용")
    a = ap.parse_args()
    faces_tl = json.load(open(a.faces, encoding="utf-8")) if a.faces else None
    base = a.out or os.path.join(os.path.dirname(a.source) or ".", "프리미어")
    d_clip = os.path.join(base, "클립"); d_gfx = os.path.join(base, "그래픽")
    d_srt = os.path.join(base, "자막SRT")
    for d in (base, d_clip, d_gfx, d_srt):
        os.makedirs(d, exist_ok=True)
    words = [tuple(x) for x in json.load(open(a.words, encoding="utf-8"))]
    clips = json.load(open(a.clips, encoding="utf-8"))
    if a.only:
        clips = [c for c in clips if a.only in c["name"]]
    cli_crop = None
    if a.crop:
        w_, h_, x_, y_ = (int(v) for v in a.crop.split(":"))
        cli_crop = dict(w=w_, h=h_, x=x_, y=y_)

    wm_png = os.path.join(d_gfx, "비블_워터마크.png")
    if not os.path.exists(wm_png):
        png_watermark(wm_png)

    sequences = []
    for si, c in enumerate(clips, 1):
        name = c["name"]
        p = prepare_clip(words, c)
        if p is None:
            print(f"[{name}] 구간 내 단어 없음 — 건너뜀"); continue
        crop = cli_crop
        inset = CROP_INSET if cli_crop else 0               # --crop은 PIP 박스 관례(테두리 인셋)
        if crop is None and faces_tl:                       # 풀프레임 소스: 얼굴 크롭 우선
            crop = face_crop(faces_tl, p["c_start"], p["c_end"], p["removes"])
            if crop:
                print(f"  얼굴 크롭: x={crop['x']} y={crop['y']} w={crop['w']} h={crop['h']}")
        if crop is None:                                    # 슬라이드+PIP 소스: 흰 테두리 감지
            s0, e0 = float(c["start"]), float(c["end"])
            crop = detect_pip_robust(a.source, [s0 + 1, (s0 + e0) / 2, e0 - 2],
                                     duration=src_duration(a.source)) or DEFAULT_CROP
            inset = CROP_INSET
        zc = zoom_crop(crop, inset)
        F = int(round(p["dur"] * FPS))
        print(f"[{name}] {ass_time(p['c_start'])}~{ass_time(p['c_end'])} "
              f"({p['dur']:.0f}s, {F}f) 자막 {len(p['caps'])}개")

        mp4 = os.path.join(d_clip, name + ".mp4")
        if a.force or not os.path.exists(mp4):
            print("  얼굴 클립 렌더 중...")
            if not render_clean(a.source, p, zc, mp4):
                print(f"  [{name}] 렌더 실패 — 건너뜀"); continue

        gdir = os.path.join(d_gfx, name)
        os.makedirs(gdir, exist_ok=True)
        title_png = os.path.join(gdir, "제목.png")
        png_title(c["hook"], c.get("yellow", 2), title_png)
        cap_items = []
        for i, (s, e, txt) in enumerate(p["caps"]):
            cp = os.path.join(gdir, f"자막_{i:03d}.png")
            png_caption(txt, cp)
            sf, ef = int(round(s * FPS)), min(F, int(round(e * FPS)))
            if ef > sf:
                cap_items.append((sf, ef, txt, cp))
        ov_items = []
        for i, (s, e, disp) in enumerate(pick_overlays(c, p["caps"], p["dur"])):
            op = os.path.join(gdir, f"오버레이_{i}_{disp.replace(' ', '')[:10]}.png")
            png_overlay(disp, op)
            ov_items.append((int(round(s * FPS)), min(F, int(round(e * FPS))), disp, op))
        write_srt(p["caps"], os.path.join(d_srt, name + ".srt"))
        sequences.append(build_sequence(si, name, mp4, F, cap_items, ov_items,
                                        title_png, wm_png))
        print(f"  그래픽 {1 + len(cap_items) + len(ov_items)}개(제목1·자막{len(cap_items)}·오버레이{len(ov_items)}) + SRT")

    if not sequences:
        print("생성된 시퀀스 없음"); sys.exit(1)
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE xmeml>\n'
           '<xmeml version="5"><project><name>비블쇼츠</name><children>'
           + "".join(sequences) + "</children></project></xmeml>\n")
    out_xml = os.path.join(base, "비블쇼츠_프리미어.xml" if not a.only else f"테스트_{a.only}.xml")
    open(out_xml, "w", encoding="utf-8").write(xml)
    print(f"\nXML {len(sequences)}개 시퀀스 → {out_xml}")


if __name__ == "__main__":
    main()
