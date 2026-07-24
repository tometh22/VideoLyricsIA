#!/usr/bin/env python3
"""EXPERIMENT: proprietary CTC forced-alignment timing engine (MMS_FA, local).

Hypothesis — one engine kills BOTH residual blockers of the Replicate
chunked aligner at once:
  (1) LATENCY: ~10 min/song over the network → local torchaudio MMS_FA
      inference on CPU/MPS, no network calls (stem comes from R2 cache).
  (2) REPEATED CHORUSES: chunked alignment binds a line to the wrong
      occurrence. Here the Viterbi runs over the WHOLE song's emission
      matrix with the WHOLE lyric token sequence — the alignment is
      monotonic by construction, so the 2nd chorus can only land after
      the 1st. The failure mode disappears structurally, not heuristically.

Emissions are still computed in chunks (wav2vec2 attention is O(T^2)),
but chunking only the ACOUSTIC ENCODER is safe: each frame's emission
depends on local context (we give ±CTX seconds), while the global
Viterbi pass — the part where monotonicity matters — sees the full song.

Text comes from ground_truth.json (the text side of the pipeline —
whisper-1 studio / gemini-pro live — was validated separately). This
isolates the TIMING question: given correct text, can we time it
Rotor-grade, fast, on repeated choruses and noisy lives?

Usage (from lyricgen/backend, with the main worktree's venv + .env loaded):
  python scripts/exp_ctc_align.py rotor_megustas rotor_puesto
  python scripts/exp_ctc_align.py --star rotor_puesto   # '*' between lines
                                                        # absorbs crowd/adlibs
Writes benchmark/dataset/<slug>/ctc_output.json and prints per-line AOO.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import time
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent
sys.path.insert(0, str(BACKEND))
DATASET = BACKEND / "benchmark" / "dataset"

SR = 16000
CHUNK_S = 30.0   # encoder window
CTX_S = 4.0      # acoustic context fed on each side, trimmed from emissions
FRAME = 320      # wav2vec2 downsample: 1 emission frame per 320 samples (20 ms)


XLSR_ID = "jonatasgrosman/wav2vec2-large-xlsr-53-spanish"  # Apache-2.0


def norm_word(w: str, keep_accents: bool = False) -> str:
    """MMS_FA dictionary is romanized lowercase a-z + apostrophe; the
    Spanish XLSR vocab keeps accented vowels + ñ/ü."""
    w = w.lower()
    if keep_accents:
        return re.sub(r"[^a-záéíóúñü']", "", unicodedata.normalize("NFC", w))
    w = unicodedata.normalize("NFD", w)
    w = "".join(c for c in w if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z']", "", w)


def get_stem(slug: str, audio: Path) -> Path:
    """Vocal stem via vocal_sep (R2 cache hit for known songs); cached
    next to the dataset so reruns are free."""
    cached = DATASET / slug / "stem.wav"
    if cached.exists():
        return cached
    import vocal_sep
    stem = vocal_sep.separate_vocals(str(audio))
    if not stem:
        raise RuntimeError(f"separate_vocals failed for {slug}")
    shutil.copy(stem, cached)
    return cached


def load_wave(path: Path):
    import torch
    import torchaudio
    wav, sr = torchaudio.load(str(path))
    wav = wav.mean(0, keepdim=True)
    if sr != SR:
        wav = torchaudio.functional.resample(wav, sr, SR)
    return wav  # (1, N)


def chunked_emissions(forward, wav, device):
    """Full-song emission matrix from windowed encoder passes.
    Frame t covers samples [t*FRAME, (t+1)*FRAME) of the full waveform —
    we trim the context frames so global frame indices stay exact.
    `forward(chunk) -> (T_local, C) log-probs` abstracts the model."""
    import torch
    n = wav.shape[1]
    chunk, ctx = int(CHUNK_S * SR), int(CTX_S * SR)
    pieces = []
    with torch.inference_mode():
        for start in range(0, n, chunk):
            end = min(start + chunk, n)
            a, b = max(0, start - ctx), min(n, end + ctx)
            em = forward(wav[:, a:b].to(device))  # (T_local, C)
            lo = (start - a) // FRAME          # frames of left context to drop
            hi = lo + (end - start) // FRAME   # keep exactly the chunk's frames
            pieces.append(em[lo:hi])
    return torch.cat(pieces)  # (T_total, C)


def load_engine(model_name: str, use_star: bool, star_delta: float, device: str):
    """Returns (forward, dictionary, blank_id, star_id, keep_accents).

    mms : torchaudio MMS_FA (CC-BY-NC — benchmark reference only).
    xlsr: HF wav2vec2-large-xlsr-53-spanish (Apache-2.0, prod-safe) +
          our SYNTHETIC star class: an appended emission column equal to
          max(non-blank log-prob) - star_delta per frame. It absorbs sung
          audio that has no transcript line (solos with backing vocals,
          crowd noise) while losing ties against any token that actually
          fits — same role the MMS star plays, but model-agnostic."""
    import torch
    if model_name == "mms":
        import torchaudio
        bundle = torchaudio.pipelines.MMS_FA
        model = bundle.get_model(with_star=use_star).to(device).eval()
        dictionary = bundle.get_dict(star="*" if use_star else None)

        def forward(chunk):
            em, _ = model(chunk)
            return em[0].cpu()

        return forward, dictionary, 0, dictionary.get("*"), False

    # NOT AutoProcessor: the repo ships an optional LM decoder that drags in
    # pyctcdecode; we only need raw emissions + the char vocab.
    from transformers import AutoModelForCTC, Wav2Vec2CTCTokenizer
    tok = Wav2Vec2CTCTokenizer.from_pretrained(XLSR_ID)
    model = AutoModelForCTC.from_pretrained(XLSR_ID).to(device).eval()
    vocab = tok.get_vocab()
    dictionary = {k.lower(): v for k, v in vocab.items() if len(k) == 1}
    blank = tok.pad_token_id
    n_classes = model.config.vocab_size
    star_id = n_classes if use_star else None

    def forward(chunk):
        # wav2vec2 fine-tunes expect zero-mean/unit-var input (do_normalize)
        chunk = (chunk - chunk.mean()) / (chunk.std() + 1e-7)
        logits = model(chunk).logits[0]
        em = torch.log_softmax(logits, dim=-1)
        if use_star:
            nb = em.clone()
            nb[:, blank] = float("-inf")
            star = nb.max(dim=-1, keepdim=True).values - star_delta
            em = torch.cat([em, star], dim=-1)
        return em.cpu()

    return forward, dictionary, blank, star_id, True


def align(slug: str, use_star: bool = False, source: str = "stem",
          model_name: str = "mms", star_delta: float = 0.5):
    import torch
    import torchaudio.functional as AF

    d = DATASET / slug
    audio = next(p for p in d.iterdir()
                 if p.suffix in (".wav", ".mp3", ".m4a", ".flac") and p.stem != "stem")
    gt = json.loads((d / "ground_truth.json").read_text())
    lines = [g["text"] for g in gt]

    t0 = time.time()
    stem = get_stem(slug, audio)
    t_stem = time.time() - t0

    device = "cpu"
    forward, dictionary, blank_id, star_id, keep_acc = load_engine(
        model_name, use_star, star_delta, device)

    # tokens: flat char sequence; remember each word's (line_idx, token_count)
    words = []  # (line_idx, [token_ids])
    for li, line in enumerate(lines):
        for raw in line.split():
            w = norm_word(raw, keep_accents=keep_acc)
            ids = [dictionary[c] for c in w if c in dictionary]
            if ids:
                words.append((li, ids))
        if use_star and li < len(lines) - 1:
            words.append((-1, [star_id]))  # absorbs inter-line noise
    targets = torch.tensor([t for _, ids in words for t in ids]).unsqueeze(0)

    t1 = time.time()
    if source == "mix":
        wav = load_wave(audio)
        emission = chunked_emissions(forward, wav, device)
    elif source == "fuse":
        # Per-frame max over {stem, mix} emissions: the stem wins where
        # demucs isolated the singer cleanly; the mix wins where demucs
        # ERASED the voice (crowd-sung choruses in live shows — the known
        # residual). Viterbi only needs relative scores, so the slight
        # de-normalization is harmless.
        import torch as _t
        wav = load_wave(stem)
        e1 = chunked_emissions(forward, wav, device)
        e2 = chunked_emissions(forward, load_wave(audio), device)
        n = min(e1.shape[0], e2.shape[0])
        emission = _t.maximum(e1[:n], e2[:n])
    else:
        wav = load_wave(stem)
        emission = chunked_emissions(forward, wav, device)
    t_emit = time.time() - t1

    t2 = time.time()
    aligned, scores = AF.forced_align(
        emission.unsqueeze(0), targets, blank=blank_id
    )
    spans = AF.merge_tokens(aligned[0], scores[0].exp())
    t_vit = time.time() - t2

    # group token spans back into words → lines
    sec = lambda fr: fr * FRAME / SR
    out, i = [], 0
    line_start, line_end, cur_line = {}, {}, None
    for li, ids in words:
        wspans = spans[i : i + len(ids)]
        i += len(ids)
        if li < 0:
            continue  # star filler
        s, e = sec(wspans[0].start), sec(wspans[-1].end)
        if li not in line_start:
            line_start[li] = s
        line_end[li] = e
    for li, g in enumerate(gt):
        if li in line_start:
            out.append({"start": round(line_start[li], 2),
                        "end": round(line_end[li], 2),
                        "text": g["text"]})

    (d / f"ctc_output_{model_name}_{source}.json").write_text(json.dumps(
        {"segments": out, "source": f"exp_ctc_align/{model_name}/{source}",
         "meta": {"star": use_star, "star_delta": star_delta, "t_stem_s": round(t_stem, 1),
                  "t_emission_s": round(t_emit, 1), "t_viterbi_s": round(t_vit, 1)}},
        ensure_ascii=False, indent=1))

    # per-line onset offset vs GT (1:1 — same text by construction)
    offs = [abs(o["start"] - g["start"]) for o, g in zip(out, gt)]
    offs_sorted = sorted(offs)
    med = offs_sorted[len(offs) // 2]
    p95 = offs_sorted[int(len(offs) * 0.95) - 1] if len(offs) >= 2 else offs_sorted[0]
    dur = wav.shape[1] / SR
    print(f"\n=== {slug}  ({dur:.0f}s audio, model={model_name}, star={use_star} d={star_delta}, source={source}) ===")
    print(f"timing: stem {t_stem:.1f}s | emissions {t_emit:.1f}s | viterbi {t_vit:.1f}s")
    print(f"AOO vs Rotor: median {med:.2f}s  mean {sum(offs)/len(offs):.2f}s  "
          f"p95 {p95:.2f}s  max {max(offs):.2f}s  (n={len(offs)})")
    worst = sorted(zip(offs, out, gt), key=lambda x: -x[0])[:5]
    for o, seg, g in worst:
        print(f"  {o:6.2f}s  ours {seg['start']:7.2f} vs rotor {g['start']:7.2f}  | {g['text'][:48]}")
    return med


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    star = "--star" in sys.argv
    source = "mix" if "--mix" in sys.argv else ("fuse" if "--fuse" in sys.argv else "stem")
    model_name = "xlsr" if "--xlsr" in sys.argv else "mms"
    delta = 0.5
    for a in sys.argv[1:]:
        if a.startswith("--delta="):
            delta = float(a.split("=")[1])
    for slug in args or ["rotor_megustas"]:
        align(slug, use_star=star, source=source, model_name=model_name, star_delta=delta)
