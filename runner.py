"""Single-worker Hercules bridge. No changes to the installed Hercules package."""
import asyncio
from contextlib import asynccontextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

ROOT = Path(os.environ.get('RUNNER_DATA_DIR', Path(__file__).parent / 'data')).resolve()
HERCULES = Path(os.environ.get('HERCULES_HOME', '~/projects/hercules')).expanduser().resolve()
TOKEN = os.environ.get('HERCULES_RUNNER_TOKEN', '')
TERMINAL = {'passed', 'failed', 'error', 'timed_out'}


class Case(BaseModel):
    model_config = ConfigDict(extra='forbid')
    caseId: int
    caseCode: str = Field(default='', max_length=100)
    name: str = Field(max_length=300)
    precondition: str = Field(default='', max_length=10000)
    steps: str = Field(min_length=1, max_length=20000)
    expectedResult: str = Field(default='', max_length=10000)


class Submission(BaseModel):
    model_config = ConfigDict(extra='forbid')
    schemaVersion: str = '1'
    requestId: str = Field(min_length=1, max_length=100)
    targetUrl: str = Field(max_length=500)
    loginUsername: str = Field(default='', max_length=100)
    loginPassword: SecretStr = SecretStr('')
    timeoutSeconds: int = Field(default=600, ge=10, le=1800)
    cases: list[Case] = Field(min_length=1, max_length=10)

    @model_validator(mode='after')
    def validate_input(self):
        from urllib.parse import urlsplit
        u = urlsplit(self.targetUrl)
        if self.schemaVersion != '1' or u.scheme not in ('http', 'https') or not u.hostname or u.username or u.password:
            raise ValueError('Unsupported schema or target URL')
        if bool(self.loginUsername) != bool(self.loginPassword.get_secret_value()):
            raise ValueError('Supply both target username and password')
        if len({c.caseId for c in self.cases}) != len(self.cases):
            raise ValueError('Duplicate case IDs')
        return self


def authorize(authorization: str = Header(default='')):
    if not TOKEN or not hmac.compare_digest(authorization, 'Bearer ' + TOKEN):
        raise HTTPException(401, 'Runner authentication required')


def feature(case: Case, url: str, credentials: bool) -> str:
    # JSON quoting prevents user text from injecting additional Gherkin scenarios.
    q = lambda text: json.dumps(text, ensure_ascii=False)
    return '\n'.join([
        'Feature: Platform API execution', f'  Scenario: Case {case.caseId}',
        f'    Given the target system entry URL is {q(url)}',
        '    And I must stay within the target system and use only disposable test data',
        ('    And I read credentials.json from the test data directory and log in to the target system without disclosing credentials'
         if credentials else '    And no login credentials were supplied; if authentication is required I report an incomplete test'),
        f'    And I establish these preconditions: {q(case.precondition)}',
        '    And I discover missing IDs and API routes from the target UI or API; I never invent values',
        '    And if a destructive action has no uniquely identifiable disposable test record I stop and report the missing prerequisite',
        f'    When I execute the following API test steps using the API helper and the authenticated session: {q(case.steps)}',
        '    And I execute each tested write request only once and retain the original API response as evidence',
        f'    Then I verify the expected outcome: {q(case.expectedResult)}',
        '    And I report failed assertions accurately; missing evidence or prerequisites mean incomplete, never passed',
        '',
    ])


def inspect_evidence(work: Path, case: Case, target_url: str = '') -> tuple[str, dict]:
    """Verify explicit API assertions against original CLI API logs; never replay HTTP."""
    files = list((work / 'output').rglob('*.xml'))
    if len(files) != 1:
        return 'error', {'reason': 'Expected exactly one Hercules XML report'}
    cases = list(ET.parse(files[0]).getroot().iter('testcase'))
    if len(cases) != 1:
        return 'error', {'reason': 'Unexpected scenario count'}
    node = cases[0]
    props = {p.get('name'): p.get('value', '').lower() for p in node.findall('./properties/property')}
    if node.find('error') is not None or node.find('skipped') is not None:
        return 'error', {'reason': 'Hercules reported an execution error or skipped scenario'}
    if node.find('failure') is not None:
        return 'failed', {'reason': 'Hercules reported an assertion failure'}
    # This installed Hercules version defaults is_completed=False even for its
    # successful assertion node. Read the final structured planner assertion too;
    # absence of XML failures alone is never sufficient.
    terminal_assertion = None
    for history_file in work.rglob('agent_inner_thoughts.json'):
        history = json.loads(history_file.read_text())
        for messages in history.values():
            if isinstance(messages, list) and messages:
                final = messages[-1].get('content')
                if isinstance(final, str):
                    try:
                        final = json.loads(final)
                    except ValueError:
                        continue
                if isinstance(final, dict) and final.get('terminate') == 'yes' and final.get('is_assert') is True:
                    terminal_assertion = final.get('is_passed')
    if terminal_assertion is False:
        return 'failed', {'reason': 'Hercules final assertion failed'}
    if props.get('is_completed') != 'true' and terminal_assertion is not True:
        return 'error', {'reason': 'Hercules did not explicitly complete the scenario or return a final assertion'}
    requests = re.findall(r'^\s*(?:请求|request|接口)\s*[:：]\s*(GET|POST|PUT|DELETE|PATCH)\s+(\S+)\s*$', case.steps, re.M | re.I)
    statuses = re.findall(r'^\s*(?:期望状态码|预期状态码|expectedStatus)\s*[:：]\s*(\d{3})\s*$', case.steps, re.M | re.I)
    assertions = re.findall(r'^\s*(?:断言|assert)\s*[:：]\s*(\$[^=]*?)\s*=\s*(.+?)\s*$', case.steps, re.M | re.I)
    if len(requests) != 1 or len(statuses) != 1:
        return 'error', {'reason': 'Verification requires one explicit HTTP request and expected status per case'}
    from urllib.parse import urlsplit
    method, template = requests[0]
    expected_url = urlsplit(template if template.startswith(('http://', 'https://')) else target_url)
    path = urlsplit(template).path
    pattern = re.escape(path)
    pattern = re.sub(r'\\\{\\\{.*?\\\}\\\}', '[^/]+', pattern)
    matches = []
    for logfile in work.rglob('api_logs.log'):
        pending = None
        for line in logfile.read_text(errors='replace').splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if 'request_data' in event:
                pending = event['request_data']
            elif 'response_data' in event and pending:
                actual_url = urlsplit(pending.get('url', ''))
                origin_matches = not expected_url.netloc or (actual_url.scheme, actual_url.netloc) == (expected_url.scheme, expected_url.netloc)
                if origin_matches and pending.get('method', '').upper() == method.upper() and re.fullmatch(pattern, actual_url.path):
                    matches.append(event['response_data'])
                pending = None
    if len(matches) != 1:
        return 'error', {'reason': 'Original response missing or ambiguous; no requests were replayed'}
    response = matches[0]
    checks = [{'assertion': 'HTTP status', 'passed': response.get('status_code') == int(statuses[0])}]
    try:
        body = json.loads(response.get('body', '')) if assertions else None
        for index, (selector, expected) in enumerate(assertions):
            if not re.fullmatch(r'\$(?:\.[\w-]+|\[\d+\])+', selector):
                raise ValueError('Unsupported JSON path')
            actual = body
            for key, number in re.findall(r'\.([\w-]+)|\[(\d+)\]', selector):
                actual = actual[key] if key else actual[int(number)]
            try:
                expected_value = json.loads(expected)
            except ValueError:
                expected_value = expected
            checks.append({'assertion': f'JSON assertion {index + 1}', 'passed': type(actual) is type(expected_value) and actual == expected_value})
    except (ValueError, KeyError, TypeError, IndexError):
        return 'error', {'reason': 'Could not evaluate a declared assertion from captured response'}
    evidence = {'reason': 'Original API response checked; broader expected outcome evaluated by Hercules',
                'checks': checks, 'statusCode': response.get('status_code'),
                'responseSha256': hashlib.sha256(response.get('body', '').encode()).hexdigest()}
    return ('passed' if all(c['passed'] for c in checks) else 'failed'), evidence


class Engine:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.lockfile = (self.root / 'worker.lock').open('w')
        fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / 'jobs.sqlite', check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, request_id TEXT UNIQUE, fingerprint TEXT, result TEXT)')
        self.db.commit()
        self.queue = queue.Queue(maxsize=4)
        self.stopping = threading.Event()
        self.process = None
        # Credentials are intentionally not persisted: interrupted jobs cannot be resumed.
        for (raw,) in self.db.execute('SELECT result FROM jobs').fetchall():
            job = json.loads(raw)
            if job['state'] not in TERMINAL:
                job.update(state='error', finishedAt=time.time(), error='Runner restarted; submit a new request')
                self.save(job)
        for work in self.root.glob('work-*'):
            marker = work / 'process.json'
            if marker.exists():
                try:
                    recorded = json.loads(marker.read_text())
                    current = Path(f"/proc/{recorded['pid']}/stat").read_text().rsplit(')', 1)[1].split()[19]
                    if current == recorded['start']:
                        os.killpg(recorded['pid'], signal.SIGKILL)
                except (FileNotFoundError, ProcessLookupError):
                    pass
            # Only remove our private scratch dirs, never Hercules installation artifacts.
            shutil.rmtree(work)
        self.worker = threading.Thread(target=self.loop, daemon=True)
        self.worker.start()

    def save(self, job):
        with self.lock:
            self.db.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(job), job['runId']))
            self.db.commit()

    def get(self, run_id):
        with self.lock:
            row = self.db.execute('SELECT result FROM jobs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Unknown run')
        return json.loads(row[0])

    def submit(self, request):
        safe = request.model_dump(exclude={'loginPassword', 'loginUsername', 'requestId'})
        fingerprint = hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()
        with self.lock:
            row = self.db.execute('SELECT id,fingerprint FROM jobs WHERE request_id=?', (request.requestId,)).fetchone()
            if row:
                if row[1] != fingerprint:
                    raise HTTPException(409, 'requestId already used for a different test')
                return self.get(row[0])
            if self.queue.full() or self.stopping.is_set():
                raise HTTPException(429, 'Runner queue full or shutting down')
            job = {'schemaVersion': '1', 'runId': str(uuid.uuid4()), 'state': 'queued', 'createdAt': time.time(), 'caseResults': []}
            self.db.execute('INSERT INTO jobs VALUES (?,?,?,?)', (job['runId'], request.requestId, fingerprint, json.dumps(job)))
            self.db.commit()
            self.queue.put_nowait((job['runId'], request))
            return job

    def stop_process(self):
        process = self.process
        if process:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def close(self):
        with self.lock:
            self.stopping.set()
            self.stop_process()
        self.worker.join(timeout=10)
        # Let startup reconcile queued jobs whose credentials were memory-only.
        self.db.close()
        self.lockfile.close()

    def loop(self):
        while not self.stopping.is_set():
            try:
                run_id, request = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            job = self.get(run_id)
            job.update(state='running', startedAt=time.time())
            self.save(job)
            deadline = time.monotonic() + request.timeoutSeconds
            try:
                for case in request.cases:
                    started = time.monotonic()
                    state, evidence = self.execute(run_id, case, request, deadline)
                    job['caseResults'].append({'caseId': case.caseId, 'state': state, 'durationMs': int((time.monotonic()-started)*1000), 'evidence': evidence})
                    self.save(job)
                states = {r['state'] for r in job['caseResults']}
                job['state'] = next((s for s in ('timed_out', 'error', 'failed') if s in states), 'passed')
            except Exception:
                # Never emit exceptions containing commands, model output, or target credentials.
                job.update(state='error', error='Runner internal error; inspect configuration and dependencies')
            finally:
                job['finishedAt'] = time.time()
                self.save(job)
                self.queue.task_done()
                del request

    def execute(self, run_id, case, request, deadline):
        work = self.root / f'work-{run_id}-{case.caseId}'
        work.mkdir(mode=0o700)
        try:
            for folder in ('input', 'output', 'test_data'):
                (work / folder).mkdir(mode=0o700)
            (work / 'input/test.feature').write_text(feature(case, request.targetUrl, bool(request.loginUsername)))
            if request.loginUsername:
                credential_path = work / 'test_data/credentials.json'
                credential_path.write_text(json.dumps({'username': request.loginUsername, 'password': request.loginPassword.get_secret_value()}))
                credential_path.chmod(0o600)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self.stopping.is_set():
                return 'timed_out', {'reason': 'Job deadline exceeded or runner shutting down'}
            env = dict(os.environ, ENABLE_TELEMETRY='0', AUTO_MODE='1', ENABLE_UBLOCK_EXTENSION='false', PYTHONDONTWRITEBYTECODE='1')
            env.pop('HERCULES_RUNNER_TOKEN', None)
            env['AGENTS_LLM_CONFIG_FILE'] = str(HERCULES / 'agents_llm_config.json')
            env['AGENTS_LLM_CONFIG_FILE_REF_KEY'] = os.environ.get('HERCULES_MODEL_PROFILE', 'ollama')
            command = [str(HERCULES / '.venv/bin/testzeus-hercules'), '--project-base', str(work), '--input-file', str(work/'input/test.feature'), '--output-path', str(work/'output'), '--test-data-path', str(work/'test_data')]
            # Raw stdout, screenshots and API logs can contain secrets. Never serve or persist them after completion.
            with (work / 'cli.log').open('w') as log:
                with self.lock:
                    if self.stopping.is_set():
                        return 'error', {'reason': 'Runner shutting down'}
                    self.process = subprocess.Popen(command, cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    process_start = Path(f'/proc/{self.process.pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
                    (work / 'process.json').write_text(json.dumps({'pid': self.process.pid, 'start': process_start}))
                try:
                    code = self.process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    return 'timed_out', {'reason': 'Hercules execution deadline exceeded'}
                finally:
                    self.stop_process()
                    self.process = None
            if code != 0:
                return 'error', {'reason': f'Hercules exited with code {code}'}
            return inspect_evidence(work, case, request.targetUrl)
        finally:
            shutil.rmtree(work)


@asynccontextmanager
async def lifespan(app):
    if not TOKEN:
        raise RuntimeError('Set HERCULES_RUNNER_TOKEN before starting the runner')
    if not (HERCULES / '.venv/bin/testzeus-hercules').is_file() or not (HERCULES / 'agents_llm_config.json').is_file():
        raise RuntimeError('Hercules CLI or model configuration is missing')
    app.state.engine = Engine()
    yield
    app.state.engine.close()


app = FastAPI(title='Hercules runner', lifespan=lifespan, docs_url=None, redoc_url=None)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    # Pydantic's default response can echo passwords from invalid request bodies.
    return JSONResponse(status_code=422, content={'detail': 'Invalid runner request; check schema and required fields'})


@app.get('/health')
def health():
    alive = app.state.engine.worker.is_alive()
    return JSONResponse(status_code=200 if alive else 503, content={'status': 'UP' if alive else 'DOWN', 'workerAlive': alive})


@app.post('/runs', status_code=202, dependencies=[Depends(authorize)])
def submit(request: Submission):
    return app.state.engine.submit(request)


@app.get('/runs/{run_id}', dependencies=[Depends(authorize)])
def result(run_id: str):
    return app.state.engine.get(run_id)
