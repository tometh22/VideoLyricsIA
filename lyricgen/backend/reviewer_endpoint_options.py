"""Review-only endpoint alternatives; never evidence for automatic adoption.

Two fixed experiments: alternative CTC path peaks, and the largest learned
activity fall with a CTC exit inside its interval. No spectral heuristic, pitch
threshold, universal hold, human target, or cross-signal timestamp transfer.
"""
import math


def options(ctc, activity, baseline, occurrence_limit):
    if ctc['clock'] != 'original_mix_decoded' or activity['clock'] != ctc['clock']:
        raise ValueError('unverified_clock')
    if ctc['audio_sha256'] != activity['audio_sha256']:
        raise ValueError('different_audio')
    limit = min(occurrence_limit, ctc['window']['end'], activity['window']['end'])
    frames = ctc['frames']
    def score(f):
        v=f.get('last_character_exit_relative_log_score')
        return v if v is not None and math.isfinite(v) else -math.inf
    eligible = [f for f in frames if baseline < f['time']+.02 <= limit and score(f)>-math.inf]
    peaks = [f for i,f in enumerate(frames) if 0<i<len(frames)-1
        and f in eligible and score(f)>=score(frames[i-1]) and score(f)>score(frames[i+1])]
    peaks.sort(key=score,reverse=True)
    a=[{'end':round(f['time']+.02,6),'relative_path_log_score':score(f)} for f in peaks[:3]]
    # Span is fixed by the checkpoint's receptive field, not fitted to endings.
    radius=activity['receptive_field_seconds']/2
    af=activity['frames']
    drops=[]
    for i,f in enumerate(af):
        if not baseline < f['time'] < limit:
            continue
        left=[v['probability'] for v in af[max(0,i-57):i]]
        right=[v['probability'] for v in af[i:min(len(af),i+57)]]
        if len(left)<57 or len(right)<57:
            continue
        drops.append((sum(left)/len(left)-sum(right)/len(right),f['time']))
    b=[]
    if drops:
        drop,t=max(drops)
        interval=[max(baseline,t-radius),min(limit,t+radius)]
        within=[f for f in eligible if interval[0]<=f['time']+.02<=interval[1]]
        if drop>0 and within:
            best=max(within,key=score)
            b=[{'end':round(best['time']+.02,6),'activity_fall_interval':interval,
                'activity_mean_drop':drop,'relative_path_log_score':score(best)}]
    return {'baseline_alternative':baseline,'occurrence_limit':occurrence_limit,
        'examined_limit':limit,'A_ctc_path_peaks':a,'B_activity_fall_ctc':b,
        'selector_decision':'keep_baseline_pending_human_review',
        'automatic_apply_allowed':False,'same_word_continuation_verified':False,
        'uncertainties':['CTC path score is not perceptual-end confidence',
            'Singing activity can include chorus or reverb',
            'Activity receptive field blurs transitions; acoustic latency uncalibrated'],
        'abstentions':{'A':None if a else 'no_later_local_path_peak',
            'B':None if b else 'no_observable_activity_fall_with_ctc_exit'}}
