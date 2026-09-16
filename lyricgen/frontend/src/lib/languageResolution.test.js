import { expect, it, vi } from 'vitest';
import { resolveSavedLanguageReview } from './languageResolution';
it('posts only the saved revision and never fetches a newer document', async () => {
 const request=vi.fn().mockResolvedValue({ok:true,json:async()=>({revision:38})});
 expect(await resolveSavedLanguageReview(request,'song',38)).toEqual({ok:true,revision:38});
 expect(request).toHaveBeenCalledTimes(1);
 expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({base_revision:38});
});
it('does not resolve an unsaved snapshot', async () => {
 const request=vi.fn();
 expect((await resolveSavedLanguageReview(request,'song',undefined)).ok).toBe(false);
 expect(request).not.toHaveBeenCalled();
});
it('reports stale revisions and network failures without falsely clearing the gate', async () => {
 expect(await resolveSavedLanguageReview(vi.fn().mockResolvedValue({ok:false,status:409,json:async()=>({})}),'song',38)).toEqual({ok:false,reason:'stale_revision'});
 expect(await resolveSavedLanguageReview(vi.fn().mockRejectedValue(new Error('offline')),'song',38)).toEqual({ok:false,reason:'network'});
});
