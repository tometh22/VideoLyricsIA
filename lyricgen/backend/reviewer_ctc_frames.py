"""CTC frame/path diagnostics. Blank and synthetic star are not silence."""
import time
import numpy as np
from reviewer_shadow_audio import pcm, file_sha


def last_token_profile(emission, targets, blank_id):
    """Max-path score for each possible last-text-token exit, fixed emissions.

    Classic interleaved CTC trellis; final synthetic star is suffix. Scores are
    path likelihoods, not confidence or perceptual-end posteriors.
    """
    labels = [blank_id]
    for token in targets:
        labels += [token, blank_id]
    t, states = len(emission), len(labels)
    forward = np.full((t, states), -np.inf)
    backward = np.full((t, states), -np.inf)
    forward[0,0], forward[0,1] = emission[0,labels[0]], emission[0,labels[1]]
    for i in range(1,t):
        for s in range(states):
            prev = [forward[i-1,s]]
            if s: prev.append(forward[i-1,s-1])
            if s>1 and labels[s] != blank_id and labels[s] != labels[s-2]: prev.append(forward[i-1,s-2])
            forward[i,s] = max(prev)+emission[i,labels[s]]
    backward[-1,-2:] = 0
    for i in range(t-2,-1,-1):
        for s in range(states):
            nxt = [emission[i+1,labels[s]]+backward[i+1,s]]
            if s+1<states: nxt.append(emission[i+1,labels[s+1]]+backward[i+1,s+1])
            if s+2<states and labels[s+2] != blank_id and labels[s+2] != labels[s]:
                nxt.append(emission[i+1,labels[s+2]]+backward[i+1,s+2])
            backward[i,s] = max(nxt)
    last_text_state = 2*(len(targets)-2)+1
    profile = []
    for i in range(t-1):
        s = last_text_state
        nxt = [emission[i+1,labels[s+1]]+backward[i+1,s+1]]
        if labels[s+2] != labels[s]: nxt.append(emission[i+1,labels[s+2]]+backward[i+1,s+2])
        profile.append(forward[i,s]+max(nxt))
    best = max(profile)
    return [float(v-best) if np.isfinite(v) else None for v in profile]


def inspect(audio, text, window):
    import torch
    import ctc_align as ctc
    started=time.monotonic()
    torch.set_num_threads(2)
    model, dictionary, blank = ctc._load_model()
    signal=pcm(audio,rate=ctc.SR).copy()
    a,b=window['start'],window['end']
    wav=torch.from_numpy(signal[int(a*ctc.SR):int(b*ctc.SR)]).unsqueeze(0)
    raw=ctc._emissions(model,wav,blank,ctc._star_delta(),append_star=False)
    nb=raw.clone(); nb[:,blank]=-float('inf')
    star=nb.max(dim=-1,keepdim=True).values-ctc._star_delta()
    em=torch.cat([raw,star],dim=-1)
    targets,_=ctc.build_targets([text],dictionary,model.config.vocab_size,word_sep_id=dictionary.get('|'))
    profile=last_token_profile(em.numpy(),targets,blank)
    inv={v:k for k,v in dictionary.items()}
    rows=[]
    for i,probs in enumerate(raw.exp()):
        vals,ids=torch.topk(probs,5)
        rows.append({'time':a+i*.02, 'blank_probability':float(probs[blank]),
            'last_character_probability':float(probs[targets[-2]]),
            'top_classes':[{'class': '<blank>' if int(k)==blank else inv.get(int(k),str(int(k))),
                            'probability':float(v)} for k,v in zip(ids,vals)],
            'last_character_exit_relative_log_score':profile[i] if i<len(profile) else None})
    return {'model':ctc.MODEL_ID,'revision':ctc.MODEL_REVISION,'audio_sha256':file_sha(audio),
        'window':window,'conditioning_text':text,'clock':'original_mix_decoded',
        'output_kind':'grapheme_ctc_probabilities_and_alternative_paths',
        'blank_is_silence':False,'phoneme_probabilities':False,
        'synthetic_star_is_learned_class':False,'star_delta':ctc._star_delta(),
        'best_last_character_end':a+(int(np.nanargmax([v if v is not None else -np.inf for v in profile]))+1)*.02,
        'frames':rows,'latency_seconds':round(time.monotonic()-started,3)}
