#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
edit_diff.py — 엔진 러프컷 vs 사용자 최종본 비교(편집 학습 루프).

원본 전사(_words.json, 원본 타임라인)와 최종본 전사(final_words.json, 최종 타임라인)를
단어 시퀀스로 정렬해서 다음을 뽑는다:
  1) 사용자가 '내용으로' 지운 구간 — 엔진이 남겼는데 최종본에 없는 발화 (타임코드+텍스트)
  2) 사용자가 되살린 구간 — 엔진이 지웠는데 최종본에 있는 발화 (과제거 신호)
  3) 쉼(pause) 캘리브레이션 — 같은 두 단어 사이 간격이 엔진 컷과 최종본에서 얼마나 다른가
     (문장 경계/비경계 구분 → PAD_LEAD/PAD_TAIL/MIN_SILENCE 보정 근거)

사용:
  python3 edit_diff.py "<base>_words.json" "<base>_cut.xml" final_words.json [출력폴더]
"""
import sys, os, json, re, difflib, statistics

def norm(t):
    return re.sub(r"[^\w가-힣]", "", t).lower()

def load_words(p):
    return [tuple(w) for w in json.load(open(p, encoding="utf-8"))]

def parse_keeps(cut_xml):
    txt = open(cut_xml, encoding="utf-8").read()
    fps = int(re.search(r"<timebase>(\d+)</timebase>", txt).group(1))
    ntsc = "TRUE" in (re.search(r"<ntsc>(\w+)</ntsc>", txt).group(1))
    f = fps * 1000 / 1001 if ntsc else float(fps)
    ks = []
    for ci in re.finditer(r'<clipitem id="cv\d+">.*?</clipitem>', txt, re.S):
        b = ci.group(0)
        i = re.search(r"<in>(\d+)</in>", b); o = re.search(r"<out>(\d+)</out>", b)
        if i and o:
            ks.append((int(i.group(1)) / f, int(o.group(1)) / f))
    return sorted(ks)

def in_keeps(t, keeps):
    for a, b in keeps:
        if a <= t <= b:
            return True
        if a > t:
            break
    return False

def ts(t):
    t = int(t); return f"{t//3600:02d}:{(t%3600)//60:02d}:{t%60:02d}"

SENT_END = ("다", "요", "죠", "까", "니다", "세요", ".", "?", "!")

def main():
    ow_p, xml_p, fw_p = sys.argv[1], sys.argv[2], sys.argv[3]
    outdir = sys.argv[4] if len(sys.argv) > 4 else "."
    ow = load_words(ow_p)          # 원본 타임라인
    fw = load_words(fw_p)          # 최종 타임라인
    keeps = parse_keeps(xml_p)     # 엔진 keep(원본 타임라인)

    a = [norm(t) for _, _, t in ow]
    b = [norm(t) for _, _, t in fw]
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    blocks = sm.get_matching_blocks()

    # 매칭 페어 (orig_idx, final_idx)
    pairs = []
    for bl in blocks:
        for k in range(bl.size):
            pairs.append((bl.a + k, bl.b + k))

    matched_a = set(p[0] for p in pairs)

    # 1) 사용자가 지운 구간(원본에 있는데 최종에 없음) → 엔진이 남겼었는지 분류
    deleted = []   # (dur, s, e, text, kept_by_engine_fraction)
    i = 0
    n = len(ow)
    while i < n:
        if i in matched_a or not a[i]:
            i += 1; continue
        j = i
        while j + 1 < n and (j + 1 not in matched_a):
            j += 1
        s, e = ow[i][0], ow[j][1]
        text = " ".join(w[2] for w in ow[i:j+1])
        mid_pts = [ (ow[k][0]+ow[k][1])/2 for k in range(i, j+1) ]
        kept_frac = sum(1 for m in mid_pts if in_keeps(m, keeps)) / len(mid_pts)
        deleted.append((e - s, s, e, text, kept_frac))
        i = j + 1

    user_only = [d for d in deleted if d[4] >= 0.5]     # 엔진은 남겼는데 사용자가 지움
    both = [d for d in deleted if d[4] < 0.5]           # 엔진도 지웠음(무음/더듬 등)

    # 2) 사용자가 되살린 구간(매칭됐는데 원본 시각이 엔진 keep 밖) = 엔진 과제거
    restored = []
    run = []
    for oi, fi in pairs:
        mid = (ow[oi][0] + ow[oi][1]) / 2
        if not in_keeps(mid, keeps) and a[oi]:
            run.append(oi)
        else:
            if run:
                s, e = ow[run[0]][0], ow[run[-1]][1]
                restored.append((e - s, s, e, " ".join(ow[k][2] for k in run)))
                run = []
    if run:
        s, e = ow[run[0]][0], ow[run[-1]][1]
        restored.append((e - s, s, e, " ".join(ow[k][2] for k in run)))

    # 3) 쉼 캘리브레이션 — 인접 매칭 페어의 간격 비교
    pause_final_sent, pause_final_mid = [], []
    tighten = []   # (delta, orig_t, prev_word) 사용자가 엔진보다 더 줄인 곳
    pairs.sort()
    for (o1, f1), (o2, f2) in zip(pairs, pairs[1:]):
        if o2 - o1 != 1 or f2 - f1 != 1:
            continue
        g_orig = ow[o2][0] - ow[o1][1]
        g_fin = fw[f2][0] - fw[f1][1]
        if g_fin < -0.2 or g_orig < 0:
            continue
        w_prev = ow[o1][2].rstrip()
        is_sent = w_prev.endswith(SENT_END)
        (pause_final_sent if is_sent else pause_final_mid).append(g_fin)
        # 엔진 컷에서의 간격: 두 단어가 모두 keep 안이고 같은 keep이 아니면 컷 지점
        delta = g_orig - g_fin
        if delta > 0.25 and in_keeps((ow[o1][0]+ow[o1][1])/2, keeps) and in_keeps((ow[o2][0]+ow[o2][1])/2, keeps):
            # 엔진은 이 사이를 (거의) 그대로 뒀는데 사용자가 줄였는가?
            # 엔진 컷 후 간격 근사: keep 경계가 사이에 없으면 g_orig 그대로
            between_cut = any(a2 > ow[o1][1] and b2 < ow[o2][0] for a2, b2 in
                              ((k[1], k2[0]) for k, k2 in zip(keeps, keeps[1:])))
            g_mine = g_orig if not between_cut else None
            if g_mine is not None and g_mine - g_fin > 0.25:
                tighten.append((round(g_mine - g_fin, 2), ow[o1][1], w_prev, round(g_mine,2), round(g_fin,2)))

    # ── 리포트 ──
    os.makedirs(outdir, exist_ok=True)
    def med(x): return statistics.median(x) if x else 0
    def p90(x): return sorted(x)[int(len(x)*0.9)] if x else 0

    print(f"원본 단어 {len(ow)} · 최종 단어 {len(fw)} · 매칭 {len(pairs)} ({len(pairs)/max(1,len(fw))*100:.0f}% of final)")
    print(f"\n[1] 사용자가 '내용으로' 지운 구간(엔진은 남김): {len(user_only)}곳 · 총 {sum(d[0] for d in user_only)/60:.1f}분")
    print(f"    (참고: 엔진도 지웠던 발화 {len(both)}곳 · {sum(d[0] for d in both)/60:.1f}분)")
    print(f"[2] 사용자가 되살린 구간(엔진 과제거): {len([r for r in restored if r[0]>0.15])}곳 · 총 {sum(r[0] for r in restored)/60:.2f}분")
    print(f"[3] 최종본 쉼: 문장경계 중앙값 {med(pause_final_sent):.2f}s (p90 {p90(pause_final_sent):.2f}) · "
          f"문장중간 중앙값 {med(pause_final_mid):.2f}s (p90 {p90(pause_final_mid):.2f})")
    print(f"    엔진이 남긴 간격을 사용자가 더 줄인 곳: {len(tighten)}곳 (중앙값 {med([t[0] for t in tighten]):.2f}s 추가 단축)")

    with open(os.path.join(outdir, "diff_user_deleted.txt"), "w", encoding="utf-8") as f:
        f.write("=== 사용자가 지운 구간 (엔진은 남김) — 긴 순 ===\n")
        for dur, s, e, text, kf in sorted(user_only, reverse=True):
            f.write(f"\n[{ts(s)}~{ts(e)}] ({dur:.1f}s, 엔진keep {kf*100:.0f}%)\n  {text}\n")
    with open(os.path.join(outdir, "diff_restored.txt"), "w", encoding="utf-8") as f:
        f.write("=== 사용자가 되살린 구간 (엔진이 지움) — 긴 순 ===\n")
        for dur, s, e, text in sorted(restored, reverse=True):
            if dur > 0.1:
                f.write(f"\n[{ts(s)}~{ts(e)}] ({dur:.1f}s)\n  {text}\n")
    with open(os.path.join(outdir, "diff_tighten.txt"), "w", encoding="utf-8") as f:
        f.write("=== 엔진이 남긴 쉼을 사용자가 더 줄인 지점 — 큰 순 ===\n")
        for d, t, w, gm, gf in sorted(tighten, reverse=True):
            f.write(f"[{ts(t)}] '{w}' 뒤  {gm}s → {gf}s (−{d}s)\n")

    # ── 정답(ground truth) JSON — 리서처 평가용 ──
    # user_only = 비블이 '내용으로' 지운 구간(엔진은 남겼음). [초, 초, 텍스트] 원본 타임라인.
    truth = [[round(s, 2), round(e, 2), text] for _dur, s, e, text, _kf
             in sorted(user_only, key=lambda d: d[1])]
    json.dump(truth, open(os.path.join(outdir, "truth_content_cuts.json"), "w",
                          encoding="utf-8"), ensure_ascii=False)
    # truth_all = 비블이 없앤 모든 발화(엔진이 이미 일부 지운 구간 포함) — 정밀도 채점의 공정한 분모
    truth_all = [[round(s, 2), round(e, 2), text] for _dur, s, e, text, _kf
                 in sorted(deleted, key=lambda d: d[1])]
    json.dump(truth_all, open(os.path.join(outdir, "truth_all_deleted.json"), "w",
                              encoding="utf-8"), ensure_ascii=False)
    print(f"\n상세 → {outdir}/diff_user_deleted.txt, diff_restored.txt, diff_tighten.txt")
    print(f"정답 → {outdir}/truth_content_cuts.json ({len(truth)}구간)")

if __name__ == "__main__":
    main()
