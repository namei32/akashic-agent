"""One isolated memory-access ablation trial with reusable historical vectors."""
from __future__ import annotations
import argparse,ast,asyncio,hashlib,json,sqlite3,struct,time
from pathlib import Path

from agent.plugin_composition import CHAT_MODELS,EMBEDDINGS,ModelRequest
from agent.plugins.snapshot import lease_runtime_snapshot
from eval.longmemeval.runtime import create_runtime,close_runtime
from eval.longmemeval.ingest import ingest_instance
from session.embedding_store import MessageEmbeddings
from session.message import ContentPart,Input,Output


def message_text(message):
    if not isinstance(message.body,(Input,Output)):return ''
    return ''.join(p.value for p in message.body.parts if isinstance(p,ContentPart) and p.kind=='text' and isinstance(p.value,str))


def seed_history_embeddings(rt, source: Path, *, model: str, dimension: int):
    """Read a frozen cache; publish matching original-history vectors through their owner API."""
    vectors=MessageEmbeddings(rt.core.message_log).bind(message_text)
    digest=hashlib.sha256();count=0
    with sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True) as db:
        rows=db.execute("SELECT e.message_id,e.content_hash,e.model,e.embedding,e.dim,m.session_key "
            "FROM message_embeddings e JOIN messages m ON m.id=e.message_id "
            "WHERE m.session_key NOT LIKE 'programmatic:eval:%' ORDER BY e.message_id,e.model")
        for identity,content_hash,space,blob,dim,session in rows:
            if space!=model or dim!=dimension:raise ValueError('Historical cache embedding space mismatch')
            message=rt.core.message_log.reader(session).get(identity)
            if message is None:raise ValueError('Cached vector has no imported historical message')
            if hashlib.sha256(message_text(message).encode()).hexdigest()!=content_hash:
                raise ValueError('Cached vector content does not match the imported history')
            if len(blob)!=4*dim:raise ValueError('Malformed cached embedding')
            vectors.save(message,model=space,embedding=struct.unpack(f'{dim}f',blob))
            digest.update(identity.encode());digest.update(space.encode());digest.update(content_hash.encode());digest.update(blob)
            count+=1
    return {'rows':count,'sha256':digest.hexdigest(),'source':str(source),'scope':'original history only; no prior QA, prompts, answers or model calls copied'}


async def trial(args):
    task=json.loads(args.task.read_text())
    if task['benchmark']=='longmemeval':
        from eval.longmemeval.dataset import load_dataset
        from eval.longmemeval.qa_runner import run_qa_instance
        instance=load_dataset(task['input'])[0]
    else:
        from eval.personamem.dataset import load_dataset
        from eval.personamem.qa_runner import run_qa_instance
        instance=load_dataset(task['input'],args.contexts)[0]
    assert instance.question_id==task['id']
    started=time.monotonic();rt=None
    result={'task':task,'mode':args.mode,'error':None,'correct':False,'timings':{}}
    try:
        t=time.monotonic();rt=await create_runtime(args.config,args.workspace)
        result['timings']['runtime_start_s']=time.monotonic()-t
        async with lease_runtime_snapshot(rt.core.plugin_manager.snapshot_store) as snapshot:
            descriptor=snapshot.composition_root.context.require(EMBEDDINGS).describe()
        t=time.monotonic();result['history_messages']=await ingest_instance(rt,instance)
        result['embedding_cache']=seed_history_embeddings(rt,args.cache,model=descriptor.identity,dimension=descriptor.dimensions)
        result['timings']['history_import_and_seed_s']=time.monotonic()-t
        t=time.monotonic();await rt.start();result['timings']['start_consumers_s']=time.monotonic()-t
        row=await run_qa_instance(rt,instance,timeout_s=300)
        result['qa']=row;result['timings']['qa_s']=row['elapsed_s']
        result['error']=row['error']
        if not result['error']:
            if task['benchmark']=='personamem':
                from eval.personamem.metrics import extract_option_label
                label=extract_option_label(row['predicted_answer'],row['all_options'])
                result['predicted_label']=label;result['correct']=label==row['gold_answer']
            else:
                tree=ast.parse(args.judge.read_text())
                fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_anscheck_prompt')
                namespace={};exec(compile(ast.Module(body=[fn],type_ignores=[]),str(args.judge),'exec'),namespace)
                prompt=namespace['get_anscheck_prompt'](instance.question_type,instance.question,instance.answer,row['predicted_answer'],abstention=instance.question_id.endswith('_abs'))
                t=time.monotonic()
                async with lease_runtime_snapshot(rt.core.plugin_manager.snapshot_store) as snapshot:
                    async with snapshot.composition_root.context.require(CHAT_MODELS).execution() as execution:
                        reply=await asyncio.wait_for(execution.chat('default').complete(ModelRequest(messages=({'role':'user','content':prompt},),max_output_tokens=10)),60)
                result['timings']['judge_s']=time.monotonic()-t
                verdict=(reply.content or '').strip().lower().strip('.')
                result['judge_verdict']=verdict
                if verdict not in {'yes','no'}:result['error']='invalid_judge_verdict'
                result['correct']=verdict=='yes'
    except Exception as exc:
        import traceback
        traceback.print_exc();result['error']=f'{type(exc).__name__}: {exc}'
    finally:
        if rt is not None:await close_runtime(rt)
    result['wall_s']=time.monotonic()-started
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'id':task['id'],'mode':args.mode,'correct':result['correct'],'error':result['error'],'wall_s':result['wall_s']}),flush=True)


def main():
    p=argparse.ArgumentParser()
    for name in ['task','config','workspace','output','cache','contexts','judge']:p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--mode',choices=['none','dense','full'],required=True)
    asyncio.run(trial(p.parse_args()))

if __name__=='__main__':main()
