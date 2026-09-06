"""Sequence anchors propose correspondences, never certify recognition."""
from difflib import SequenceMatcher
from reviewer_shadow import tokens


def correspond(segment, request, window):
    if request.get('tool_status') != 'ok':
        return {'status': 'recognition_tool_failure', 'certified': False}
    words = request.get('response', {}).get('words', [])
    flat, owners = [], []
    for i, word in enumerate(words):
        ts = tokens(word.get('word', ''))
        flat += ts
        owners += [i] * len(ts)
    target = tokens(segment['text'])
    candidates = []
    for a in range(len(flat)):
        for length in range(max(1, len(target)-3), len(target)+4):
            b = a + length
            if b > len(flat):
                continue
            first, last = words[owners[a]], words[owners[b-1]]
            start, end = window['start'] + first['start'], window['start'] + last['end']
            if not max(start, segment['start']) < min(end, segment['end']):
                continue
            matcher = SequenceMatcher(None, target, flat[a:b], autojunk=False)
            anchors = [dict(baseline_start=m.a, heard_start=a+m.b, length=m.size)
                       for m in matcher.get_matching_blocks() if m.size >= 1]
            if sum(m['length'] for m in anchors) < 2:
                continue
            ops=list(matcher.get_opcodes())
            edit_cost=sum(max(y-x,v-u) for tag,x,y,u,v in ops if tag!='equal')
            candidates.append({'heard_tokens': flat[a:b], 'start': start, 'end': end,
                'edit_cost':edit_cost,'length_difference':abs(length-len(target)),
                'discontiguous_anchors_only':max(m['length'] for m in anchors)<2,
                'similarity': matcher.ratio(), 'anchors': anchors,
                'operations': [list(op) for op in matcher.get_opcodes()],
                'window_contains_baseline': window['start'] <= segment['start'] and window['end'] >= segment['end'],
                'formatting_only': target == flat[a:b], 'certified': False})
    candidates.sort(key=lambda c: (c['edit_cost'],c['length_difference'], -c['similarity'], abs(c['start']-segment['start'])))
    return {'status': 'anchored_correspondence_hypothesis' if candidates else 'no_sequence_anchor',
            'candidates': candidates[:3], 'certified': False}
