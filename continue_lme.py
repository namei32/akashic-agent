"""Complete the interrupted LongMemEval cohort after its weekly quota reset."""
from pathlib import Path
from datetime import datetime,UTC
import hashlib,json,signal,subprocess,time,os
ROOT=Path(__file__).resolve().parent;P=str(ROOT/'venv-not-used')
P=(ROOT/'python-path.txt').read_text().strip();SOURCE='/Users/namei/.codex/worktrees/personal-memory-validation/akashic';CONFIG='/Users/namei/.local/share/akashic-eval/config.toml'
child=None;state={'status':'running','phase':'initializing','started_at':datetime.now(UTC).isoformat(),'scope':'LongMemEval 120 frozen questions; preserve 3 prior quota failures; generate only 717 missing attempts'}
def update(phase=None,**extra):
 if phase:state['phase']=phase
 p=ROOT/'answer-resume-status.json'
 if p.exists():
  try:state['answer_checkpoint']=json.loads(p.read_text())
  except json.JSONDecodeError:pass
 state.update(extra,updated_at=datetime.now(UTC).isoformat());tmp=ROOT/'continue-status.json.tmp';tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2));tmp.replace(ROOT/'continue-status.json')
 a=jsonl(ROOT/'main-answers-complete/answers.jsonl');j=jsonl(ROOT/'main-judgments-complete/judgments.jsonl');state['counts']={'answers':len(a),'answer_errors':sum(bool(x.get('error')) for x in a),'judgments':len(j)}
 tmp=ROOT/'continue-status.json.tmp';tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2));tmp.replace(ROOT/'continue-status.json')
 rows=state['counts'];
 (ROOT/'STATUS.md').write_text('\n'.join(['# LongMemEval 新题续跑','',f"状态：{state['status']}；阶段：{state['phase']}。",'',f"更新时间 UTC：{state['updated_at']}",'',f"已写入：回答 {rows['answers']}/720（执行错误 {rows['answer_errors']}），评分 {rows['judgments']}/720。",'', '固定 120 道新题；每组 3 次回答。原先 3 条 quota failure 保留；只生成缺失记录。', '连续 3 次基础设施失败会停止，并保留逐条记录。', '']),encoding='utf-8')
def jsonl(path):
 if not Path(path).exists():return []
 out=[]
 for line in Path(path).read_text().splitlines():
  try:out.append(json.loads(line))
  except json.JSONDecodeError:break
 return out
def stop():
 if child and child.poll() is None:
  os.killpg(child.pid,signal.SIGINT)
  try:child.wait(timeout=15)
  except subprocess.TimeoutExpired:os.killpg(child.pid,signal.SIGKILL);child.wait()
def run(name,cmd,accepted=(0,)):
 global child
 update(name,child_pid=None)
 with (ROOT/(name+'.log')).open('x') as f:
  child=subprocess.Popen(list(map(str,cmd)),cwd=SOURCE,stdout=f,stderr=subprocess.STDOUT,start_new_session=True);update(child_pid=child.pid)
  while child.poll() is None:update();time.sleep(5)
  code=child.returncode
 child=None
 if code not in accepted:raise RuntimeError(f'{name} exited {code}; partial records retained')
 return code
def interrupted(sig,frame):raise KeyboardInterrupt(f'signal {sig}')
signal.signal(signal.SIGTERM,interrupted)
try:
 manifest=json.loads((ROOT/'recovery-manifest.json').read_text())
 assert hashlib.sha256((ROOT/'stop_guard.py').read_bytes()).hexdigest()==manifest['guard_sha256']
 result=run('answer-resume',['/Users/namei/.local/share/akashic-eval/experiments/locomo-hybrid-full-sol-max-20260924-b2302paf/venv/bin/python',ROOT/'resume_answers_from_checkpoint.py'])
 answerdir=ROOT/'main-answers-complete';answer_receipt=json.loads((answerdir/'completion.json').read_text());assert answer_receipt['rows']==720
 common=['--suite',ROOT/'suite.json','--settings',ROOT/'main-settings.json'];judge=['judge',*common,'--answers',answerdir,'--lme-rubric',ROOT/'references/longmemeval_evaluate_qa.py','--lme-sha256','ecce9c4c79dc89d99534ac17b383a5cbb5b9f0c69ee98adaf0684742e3d95251','--config',CONFIG]
 jd=ROOT/'main-judgments-complete';code=run('main-judgments-complete',['/Users/namei/.local/share/akashic-eval/experiments/locomo-hybrid-full-sol-max-20260924-b2302paf/venv/bin/python',ROOT/'stage.py',*judge,'--out',jd,'--model-workspace',ROOT/'judge-resume-model'],accepted=(0,2))
 if code==2:
  run('main-judgments-complete-retry',['/Users/namei/.local/share/akashic-eval/experiments/locomo-hybrid-full-sol-max-20260924-b2302paf/venv/bin/python',ROOT/'stage.py',*judge,'--out',ROOT/'main-judgments-complete-retry','--retry-from',jd,'--model-workspace',ROOT/'judge-resume-retry-model'])
 jdir=ROOT/'main-judgments-complete-retry' if (ROOT/'main-judgments-complete-retry').exists() else jd
 run('main-report-complete',['/Users/namei/.local/share/akashic-eval/experiments/locomo-hybrid-full-sol-max-20260924-b2302paf/venv/bin/python',ROOT/'stage.py','report',*common,'--retrieval',ROOT/'main-retrieval','--answers',answerdir,'--judgments',jdir,'--out',ROOT/'main-report-complete'])
 run('finalize',['/Users/namei/.local/share/akashic-eval/experiments/locomo-hybrid-full-sol-max-20260924-b2302paf/venv/bin/python',ROOT/'finalize_recovery.py'])
 update('completed',status='completed',child_pid=None)
 original=Path((ROOT/'parent-experiment.txt').read_text());
 with (original/'STATUS.md').open('a') as f:f.write('\nLongMemEval 新题正式评测已完成：[续跑报告]('+str(ROOT/'results.zh.md')+')。PersonaMem 三轮中断记录仍保留。\n')
except BaseException as e:
 stop();update(status='stopped',error=f'{type(e).__name__}: {e}',child_pid=None);raise
