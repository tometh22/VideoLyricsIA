"""API helper safety: exact approved scope and lossless generation payloads."""
import importlib.util
import json
from pathlib import Path
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'apply_campaign_preset.py'
spec = importlib.util.spec_from_file_location('campaign_preset_helper', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class CampaignPresetTests(unittest.TestCase):
    def test_selection_excludes_unapproved_discarded_and_missing_jobs(self):
        rows = [{'id':'approved','status':'lyrics_approved','job_id':'a'},
                {'id':'unreviewed','status':'transcribed_pending','job_id':'b'},
                {'id':'published','status':'done','job_id':'c'},
                {'id':'discarded','status':'lyrics_approved','job_id':'d','discarded':True},
                {'id':'missing','status':'lyrics_approved','job_id':None}]
        self.assertEqual([r['id'] for r in helper.ready_items({'items':rows})], ['approved'])

    def test_photo_preset_never_selects_ai_animation_or_video_effect(self):
        preset = json.loads(helper.DEFAULT_PRESET.read_text())
        groups = helper.groups_for(preset)
        self.assertAlmostEqual(sum(g['weight'] for g in groups),100)
        self.assertEqual({g['settings']['effect'] for g in groups},
                         {'bokeh','light','dust','embers','confetti','film','bass_pulse'})
        for group in groups:
            self.assertEqual(group['requirement'],'photo_effect')
            self.assertEqual(group['settings']['scene_source'],'lyrics')
            self.assertEqual(group['settings']['movement_style'],'foto-parallax')
            self.assertFalse(group['settings']['animate_image'])
            self.assertFalse(group['settings']['enable_scenes'])

    def test_generation_preserves_exact_approved_segments_and_revision(self):
        segments = [{'start':1.25,'end':2.4,'text':'Canción','locked':True,'words':[{'word':'Canción','start':1.25,'end':2.4}]}]
        item = {'job_id':'job1','title':'Título','artist':'Artista','assignment':{'revision':5},
                'settings':{'animate_image':False,'enable_scenes':False,'match_lyrics':True,'background_id':None,'effect':'film'}}
        job = {'segments_json':segments,'segments_revision':87}
        form = helper.generation_form(item,job)
        self.assertEqual(json.loads(form['segments_json']),segments)
        self.assertEqual(form['base_revision'],'87')
        self.assertEqual(form['campaign_creative_revision'],'5')
        self.assertEqual(form['animate_image'],'false')
        self.assertEqual(form['match_lyrics'],'true')
        self.assertNotIn('background_id',form)

    def test_generation_rejects_empty_segments(self):
        with self.assertRaises(ValueError):
            helper.generation_form({'job_id':'job1'}, {'segments_json':[]})

    def test_duplicate_effect_ids_rejected(self):
        with self.assertRaises(ValueError):
            helper.groups_for({'settings':{},'effects':[{'id':'same'},{'id':'same'}]})


if __name__ == '__main__':
    unittest.main()
