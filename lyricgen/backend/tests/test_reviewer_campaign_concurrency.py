from dataclasses import asdict
from pathlib import Path
import threading

import pytest

from reviewer_shadow import ShadowPolicy
from scripts.run_reviewer_campaign import execute_request_batches


class Ledger:
    def __init__(self, denied=None):
        self.owner=threading.get_ident()
        self.reservations=[]
        self.finished=[]
        self.denied=denied or {}

    def reserve(self, identity, provider, duration):
        assert threading.get_ident()==self.owner
        self.reservations.append(identity)
        return (False,self.denied[identity]) if identity in self.denied else (True,None)

    def finish(self, identity, status, directory, *, request):
        assert threading.get_ident()==self.owner
        self.finished.append((identity,status,directory))


def specs(n):
    return [{'identity':str(i),'provider':'google' if i%2 else 'openai',
             'window':{'start':i*10.,'end':i*10.+24,'offset_seconds':i*10.},
             'clip':Path(str(i)), 'folder':Path('private'), 'policy':ShadowPolicy()}
            for i in range(n)]


@pytest.mark.parametrize('concurrency',[2,4,8])
def test_bounded_calls_all_reserved_before_start_owner_only_ledger(concurrency):
    count=concurrency+2
    ledger=Ledger()
    barriers={0:threading.Barrier(concurrency),1:threading.Barrier(2)}
    lock=threading.Lock()
    active=0
    peak=0
    observed=[]
    source={'immutable':'binding'}

    class Listener:
        def __init__(self,directory,*,policy):
            assert directory==Path('private/requests')
            assert asdict(policy)==asdict(ShadowPolicy())

        def listen(self,clip,**kwargs):
            nonlocal active,peak
            i=int(str(clip));group=i//concurrency
            assert len(ledger.reservations)==min((group+1)*concurrency,count)
            assert kwargs['source']==source
            assert kwargs['view']=='mix'
            assert kwargs['window']==specs(count)[i]['window']
            assert threading.get_ident()!=ledger.owner
            with lock:
                active+=1;peak=max(peak,active)
                observed.append(i)
            barriers[group].wait(timeout=5)
            with lock:active-=1
            return {'tool_status':'ok'}

    errors=[]
    execute_request_batches(iter(specs(count)),ledger,source,errors,
                            concurrency=concurrency,listener_factory=Listener)
    assert peak==concurrency
    assert sorted(observed)==list(range(count))
    assert len(ledger.finished)==count
    assert errors==[]


def test_budget_denials_never_launch_and_known_invalid_retries_once():
    ledger=Ledger({'0':'budget_authorization_required','1':'invalid_response'})
    directories=[]

    class Listener:
        def __init__(self,directory,*,policy):
            directories.append(directory)
            assert directory==Path('private/retry-1/requests')
            assert policy.max_calls_per_song==1

        def listen(self,*args,**kwargs):
            return {'tool_status':'ok'}

    errors=[]
    execute_request_batches(specs(2),ledger,{},errors,listener_factory=Listener)
    assert errors==['budget_authorization_required']
    assert len(ledger.reservations)==3
    assert len(ledger.finished)==1
    assert len(directories)==1


def test_worker_exception_preserves_unknown_reservation_settles_others():
    ledger=Ledger()

    class Listener:
        def __init__(self,*args,**kwargs):pass

        def listen(self,clip,**kwargs):
            if str(clip)=='0':raise RuntimeError('private provider message')
            return {'tool_status':'ok'}

    errors=[]
    execute_request_batches(specs(2),ledger,{},errors,listener_factory=Listener)
    assert errors==['audio_worker_unknown_completion']
    assert len(ledger.reservations)==2
    assert [r[0] for r in ledger.finished]==['1']


@pytest.mark.parametrize('invalid',[0,1,3,16,True,4.0])
def test_concurrency_validated_before_reservations(invalid):
    ledger=Ledger()
    with pytest.raises(ValueError,match='unsupported_provider_concurrency'):
        execute_request_batches(specs(2),ledger,{},[],concurrency=invalid)
    assert ledger.reservations==[]
